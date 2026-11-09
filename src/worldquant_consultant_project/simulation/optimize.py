"""Parameter optimisation: sweep a search space over the factors worth improving.

A factor qualifies when both of its best in-sample values for the chosen delay beat the
thresholds in ``settings_for_choosing_alphas_to_optimize.json``; the parameter grid comes
from ``optimization_simulation_settings.json``. Every (factor, settings) pair runs once —
a pair that already has a stored run is skipped, so an interrupted sweep can be resumed.

Usage:
    uv run python -m worldquant_consultant_project.simulation.optimize --delay delay1
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..acquisition.config import CredentialsError, load_credentials
from . import storage
from .batch import ClientPool, outcome_bucket
from .client import BrainSimulationError
from .runner import DEFAULT_DB_PATH as DEFAULT_ALPHA_DB
from .runner import run_simulation
from .settings import (
    CAMEL_TO_FIELD,
    DEFAULT_CONCURRENCY_PATH,
    SUPPORTED_SIMULATION_TYPE,
    SettingsFileError,
    SimulationSettings,
    load_max_concurrency,
)

logger = logging.getLogger("simulation.optimize")

DEFAULT_LOG_DIR = DEFAULT_ALPHA_DB.parent / "logs"
DEFAULT_SELECTION_PATH = Path(__file__).with_name(
    "settings_for_choosing_alphas_to_optimize.json"
)
DEFAULT_SEARCH_SPACE_PATH = Path(__file__).with_name(
    "optimization_simulation_settings.json"
)

SUPPORTED_DELAYS = (1, 0)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PARTIAL = 3


@dataclass(frozen=True)
class OptimizeAlpha:
    alpha_uid: str
    expression: str


@dataclass
class OptimizeStats:
    planned: int = 0
    ok: int = 0
    # The platform evaluated the factor with these parameters and rejected it.
    failed: int = 0
    # Platform-side failures: nothing is stored, so the pair can be retried later.
    platform_failed: int = 0
    # Submitted but still computing: kept as a placeholder for reconciliation.
    pending: int = 0
    # A run with the same expression and parameters already exists.
    skipped: int = 0
    duration_seconds: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
    last_platform_error: str | None = None
    abort_reason: str | None = None


def parse_delay(value: str | int) -> int:
    """Accept ``delay1`` / ``delay0`` (the CLI spelling) or a bare ``1`` / ``0``."""
    text = str(value).strip().lower().removeprefix("delay")
    try:
        delay = int(text)
    except ValueError as exc:
        raise ValueError(f"delay must be delay0 or delay1, got {value!r}") from exc
    if delay not in SUPPORTED_DELAYS:
        raise ValueError(f"delay must be 0 or 1, got {delay}")
    return delay


def _first_number(value: Any, *, field_name: str) -> float:
    """Read a threshold, tolerating ``1.25``, ``"1.25"`` and ``["1.25"]``."""
    candidate = value[0] if isinstance(value, list) else value
    try:
        return float(candidate)
    except (TypeError, ValueError) as exc:
        raise SettingsFileError(f"{field_name} must be a number, got {value!r}") from exc


def load_thresholds(
    path: Path | str | None = None, *, delay: int
) -> tuple[float, float]:
    """``(sharpe_lower_limit, fitness_lower_limit)`` for the chosen delay."""
    selection_path = Path(path) if path else DEFAULT_SELECTION_PATH
    if not selection_path.is_file():
        raise SettingsFileError(f"selection settings not found: {selection_path}")
    try:
        payload = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsFileError(f"cannot read {selection_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SettingsFileError(f"{selection_path} must contain a JSON object")

    block = payload.get(f"delay{delay}")
    if not isinstance(block, dict):
        raise SettingsFileError(
            f"{selection_path} has no 'delay{delay}' object (found: {', '.join(payload)})"
        )
    missing = [
        name
        for name in ("is_sharpe_lower_limit", "is_fitness_lower_limit")
        if name not in block
    ]
    if missing:
        raise SettingsFileError(
            f"{selection_path} delay{delay} is missing {', '.join(missing)}"
        )
    return (
        _first_number(block["is_sharpe_lower_limit"], field_name="is_sharpe_lower_limit"),
        _first_number(block["is_fitness_lower_limit"], field_name="is_fitness_lower_limit"),
    )


def load_search_space(
    path: Path | str | None = None, *, delay: int
) -> list[SimulationSettings]:
    """Every parameter combination of the search space, in file order."""
    space_path = Path(path) if path else DEFAULT_SEARCH_SPACE_PATH
    if not space_path.is_file():
        raise SettingsFileError(f"optimisation settings not found: {space_path}")
    try:
        payload = json.loads(space_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsFileError(f"cannot read {space_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SettingsFileError(f"{space_path} must contain a JSON object")

    sim_type = str(payload.get("type") or SUPPORTED_SIMULATION_TYPE).upper()
    if sim_type != SUPPORTED_SIMULATION_TYPE:
        raise SettingsFileError(
            f"only {SUPPORTED_SIMULATION_TYPE} simulations are supported, got '{sim_type}'"
        )
    block = payload.get("search_space")
    if not isinstance(block, dict) or not block:
        raise SettingsFileError(f"{space_path} must contain a non-empty 'search_space' object")

    axes: dict[str, list[Any]] = {}
    for key, values in block.items():
        if not isinstance(values, list) or not values:
            raise SettingsFileError(f"search_space.{key} must be a non-empty list")
        axes[CAMEL_TO_FIELD.get(key, key)] = values
    if "delay" in axes:
        logger.warning("search_space.delay is ignored: the sweep uses --delay (delay%d)", delay)

    combos: list[SimulationSettings] = []
    seen: set[str] = set()
    for values in itertools.product(*axes.values()):
        overrides = dict(zip(axes, values))
        overrides.pop("delay", None)
        try:
            settings = SimulationSettings.from_dict({**overrides, "delay": delay})
        except ValueError as exc:
            raise SettingsFileError(f"invalid combination in {space_path}: {exc}") from exc
        key = storage.settings_json(settings.stored)
        if key in seen:
            continue
        seen.add(key)
        combos.append(settings)
    if not combos:
        raise SettingsFileError(f"{space_path} produced no parameter combination")
    return combos


def select_alphas(
    alpha_db: Path | str,
    *,
    delay: int,
    sharpe_min: float,
    fitness_min: float,
    limit: int | None = None,
) -> list[OptimizeAlpha]:
    """Factors whose best sharpe and fitness for ``delay`` both clear the thresholds."""
    db_path = Path(alpha_db)
    if not db_path.is_file():
        raise FileNotFoundError(f"alpha database not found: {db_path}")
    sharpe_column = f"max_is_sharpe_delay{delay}"
    fitness_column = f"max_is_fitness_delay{delay}"
    connection = sqlite3.connect(str(db_path), timeout=60.0)
    connection.row_factory = sqlite3.Row
    try:
        sql = (
            f"SELECT alpha_uid, expression FROM alphas "
            f"WHERE {sharpe_column} >= ? AND {fitness_column} >= ? "
            "ORDER BY alpha_uid"
        )
        params: list[object] = [sharpe_min, fitness_min]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            OptimizeAlpha(row["alpha_uid"], row["expression"])
            for row in connection.execute(sql, params)
        ]
    finally:
        connection.close()


def existing_pairs(alpha_db: Path | str) -> set[tuple[str, str]]:
    """Every ``(expression, settings_json)`` that already has a stored run."""
    connection = sqlite3.connect(str(alpha_db), timeout=60.0)
    try:
        return {
            (row[0], row[1])
            for row in connection.execute(
                f"SELECT expression, settings_json FROM {storage.SIMULATIONS_TABLE}"
            )
        }
    finally:
        connection.close()


def _pair_key(alpha: OptimizeAlpha, settings: SimulationSettings) -> tuple[str, str]:
    return alpha.expression, storage.settings_json(settings.stored)


def plan_runs(
    alphas: Sequence[OptimizeAlpha],
    grid: Sequence[SimulationSettings],
    *,
    alpha_db: Path | str,
) -> tuple[int, int]:
    """``(runs_to_do, runs_already_stored)`` for the whole sweep, without touching the API."""
    done = existing_pairs(alpha_db)
    already = sum(1 for alpha in alphas for settings in grid if _pair_key(alpha, settings) in done)
    return len(alphas) * len(grid) - already, already


def start_pool(
    credentials: Any, *, timeout: float
) -> ClientPool:
    return ClientPool(credentials, poll_timeout=timeout, max_submit_retries=60)


def run_optimization(
    alphas: Sequence[OptimizeAlpha],
    grid: Sequence[SimulationSettings],
    *,
    alpha_db: Path | str = DEFAULT_ALPHA_DB,
    workers: int = 1,
    timeout: float = 900.0,
    credentials_path: Path | str | None = None,
    progress_every: int = 10,
    pool: Any | None = None,
) -> OptimizeStats:
    """Backtest every (factor, settings) pair that has no stored run yet."""
    stats = OptimizeStats(planned=len(alphas) * len(grid))
    if not alphas or not grid:
        return stats

    storage.connect(alpha_db).close()
    owned_pool = pool is None
    if owned_pool:
        pool = start_pool(load_credentials(credentials_path), timeout=timeout)
    started = time.monotonic()
    completed = 0
    consecutive_platform = 0
    # A run of platform failures means the account (quota, session, outage) is the problem.
    platform_streak_limit = max(5, workers * 2)

    def worker(alpha: OptimizeAlpha, settings: SimulationSettings) -> tuple[str, str | None]:
        """``("skipped", None)`` when the pair is already stored, else (bucket, error)."""
        connection = storage.connect(alpha_db, initialize=False)
        try:
            if storage.find_sim_uid(
                connection, alpha.expression, storage.settings_json(settings.stored)
            ):
                return "skipped", None
        finally:
            connection.close()
        outcome = run_simulation(
            alpha.expression,
            settings,
            db_path=alpha_db,
            timeout=timeout,
            client=pool.client,
            initialize_db=False,
        )
        return outcome_bucket(outcome), outcome.error

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(worker, alpha, settings): (alpha, settings)
                for alpha in alphas
                for settings in grid
            }
            for future in as_completed(futures):
                alpha, settings = futures[future]
                try:
                    bucket, error = future.result()
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
                    logger.exception("optimisation run crashed for %s", alpha.alpha_uid)
                    stats.failed += 1
                    stats.failures.append((alpha.alpha_uid, str(exc)))
                    bucket, error = "factor", str(exc)

                if bucket == "skipped":
                    stats.skipped += 1
                elif bucket == "ok":
                    stats.ok += 1
                    consecutive_platform = 0
                elif bucket == "pending":
                    stats.pending += 1
                    consecutive_platform = 0
                elif bucket == "factor":
                    stats.failed += 1
                    stats.failures.append((alpha.alpha_uid, error or "failed"))
                    consecutive_platform = 0
                else:
                    stats.platform_failed += 1
                    stats.failures.append((alpha.alpha_uid, error or "platform error"))
                    stats.last_platform_error = error or "platform error"
                    consecutive_platform += 1
                completed += 1

                if stats.abort_reason is None and consecutive_platform >= platform_streak_limit:
                    stats.abort_reason = (
                        f"{consecutive_platform} consecutive platform failures "
                        f"(last: {stats.last_platform_error})"
                    )
                if stats.abort_reason:
                    logger.error(
                        "stopping the sweep: %s; the remaining pairs stay unrun", stats.abort_reason
                    )
                    for pending_future in futures:
                        pending_future.cancel()
                    break

                if progress_every and completed % progress_every == 0:
                    elapsed = time.monotonic() - started
                    rate = completed / elapsed if elapsed else 0.0
                    remaining = stats.planned - completed
                    logger.info(
                        "progress %d/%d  ok=%d failed=%d platform=%d pending=%d skipped=%d  "
                        "%.2f/min  eta %.0f min",
                        completed,
                        stats.planned,
                        stats.ok,
                        stats.failed,
                        stats.platform_failed,
                        stats.pending,
                        stats.skipped,
                        rate * 60,
                        (remaining / rate / 60) if rate else 0.0,
                    )
    finally:
        if owned_pool:
            pool.close()

    stats.duration_seconds = round(time.monotonic() - started, 1)
    return stats


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"optimize-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)
    try:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError as exc:
        logger.warning("cannot open log file %s: %s", log_path, exc)
        return None
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.simulation.optimize",
        description=(
            "Re-backtest the best factors over a parameter grid "
            "(optimization_simulation_settings.json)."
        ),
    )
    parser.add_argument(
        "--delay",
        default="delay1",
        help="delay group to optimise: delay1 (default) or delay0",
    )
    parser.add_argument(
        "--alpha-db", type=Path, default=DEFAULT_ALPHA_DB, help="factor database"
    )
    parser.add_argument(
        "--selection",
        type=Path,
        default=DEFAULT_SELECTION_PATH,
        help="settings_for_choosing_alphas_to_optimize.json",
    )
    parser.add_argument(
        "--search-space",
        type=Path,
        default=DEFAULT_SEARCH_SPACE_PATH,
        help="optimization_simulation_settings.json",
    )
    parser.add_argument(
        "--concurrency-file",
        type=Path,
        default=DEFAULT_CONCURRENCY_PATH,
        help="file holding the account's max simultaneous simulations",
    )
    parser.add_argument("--workers", type=int, help="override the concurrency file")
    parser.add_argument("--limit", type=int, help="optimise at most N factors")
    parser.add_argument("--timeout", type=float, default=900.0, help="per-simulation polling timeout")
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the selected factors and the runs still to do, then exit",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        delay = parse_delay(args.delay)
        sharpe_min, fitness_min = load_thresholds(args.selection, delay=delay)
        grid = load_search_space(args.search_space, delay=delay)
    except (ValueError, SettingsFileError) as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    try:
        workers = args.workers or load_max_concurrency(args.concurrency_file)
    except SettingsFileError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    if workers < 1:
        logger.error("workers must be >= 1")
        return EXIT_CONFIG

    try:
        alphas = select_alphas(
            args.alpha_db,
            delay=delay,
            sharpe_min=sharpe_min,
            fitness_min=fitness_min,
            limit=args.limit,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    if not alphas:
        print(
            f"no factor clears delay{delay} thresholds "
            f"(max_is_sharpe_delay{delay} >= {sharpe_min:g} and "
            f"max_is_fitness_delay{delay} >= {fitness_min:g})"
        )
        return EXIT_OK

    to_do, already = plan_runs(alphas, grid, alpha_db=args.alpha_db)
    logger.info(
        "delay%d | sharpe>=%g fitness>=%g | factors=%d | combinations=%d | runs=%d (already stored=%d) "
        "| workers=%d | db=%s",
        delay,
        sharpe_min,
        fitness_min,
        len(alphas),
        len(grid),
        to_do,
        already,
        workers,
        args.alpha_db,
    )

    if args.dry_run:
        print(f"delay{delay}: {len(alphas)} factor(s), {len(grid)} combination(s)")
        print(f"runs to do: {to_do} (already stored: {already})")
        for alpha in alphas[:5]:
            print(f"  {alpha.alpha_uid}  {alpha.expression}")
        if len(alphas) > 5:
            print(f"  ... and {len(alphas) - 5} more")
        return EXIT_OK

    try:
        stats = run_optimization(
            alphas,
            grid,
            alpha_db=args.alpha_db,
            workers=workers,
            timeout=args.timeout,
            credentials_path=args.credentials,
            progress_every=args.progress_every,
        )
    except (CredentialsError, BrainSimulationError) as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    print(
        f"optimised {len(alphas)} factor(s) over {len(grid)} combination(s) in "
        f"{stats.duration_seconds:.0f}s (ok={stats.ok}, failed={stats.failed}, "
        f"platform_failed={stats.platform_failed}, pending={stats.pending}, "
        f"skipped={stats.skipped})"
    )
    if stats.abort_reason:
        print(f"sweep stopped early: {stats.abort_reason}")
        print("  the remaining pairs can be resumed later; stored runs are skipped")
    for alpha_uid, reason in stats.failures[:10]:
        print(f"  failed {alpha_uid}: {reason[:120]}")
    if len(stats.failures) > 10:
        print(f"  ... and {len(stats.failures) - 10} more failures")

    return EXIT_OK if stats.failed == 0 else EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

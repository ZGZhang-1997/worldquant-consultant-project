"""Batch backtests: run every un-simulated alpha from ``alpha.db`` with a thread pool.

Concurrency comes from ``maximum_concurrent_number`` (the account's simultaneous
simulation allowance) and the parameters come from ``simulation_setting.json``.
Results land in ``simulations.db``; re-running the same expression with the same
settings overwrites the previous row instead of adding a new one.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from ..acquisition.config import CredentialsError, load_credentials
from . import storage
from .client import BrainSimulationClient, BrainSimulationError
from .runner import DEFAULT_DB_PATH as DEFAULT_SIMULATIONS_DB
from .runner import SimulationOutcome, run_simulation
from .settings import (
    DEFAULT_CONCURRENCY_PATH,
    DEFAULT_SETTINGS_PATH,
    SettingsFileError,
    SimulationSettings,
    load_max_concurrency,
    load_settings_file,
)

logger = logging.getLogger("simulation.batch")

DEFAULT_ALPHA_DB = DEFAULT_SIMULATIONS_DB.parent / "alpha.db"
DEFAULT_LOG_DIR = DEFAULT_SIMULATIONS_DB.parent / "logs"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PARTIAL = 3


@dataclass(frozen=True)
class PendingAlpha:
    alpha_uid: str
    expression: str


@dataclass
class BatchStats:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    duration_seconds: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)


def load_pending_alphas(
    alpha_db: Path | str, *, limit: int | None = None
) -> list[PendingAlpha]:
    """Alphas that have never been backtested, in insertion order."""
    db_path = Path(alpha_db)
    if not db_path.is_file():
        raise FileNotFoundError(f"alpha database not found: {db_path}")
    connection = sqlite3.connect(str(db_path), timeout=60.0)
    connection.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT alpha_uid, expression FROM alphas "
            "WHERE simulated IS NULL ORDER BY alpha_uid"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [PendingAlpha(row["alpha_uid"], row["expression"]) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def mark_simulated(alpha_db: Path | str, alpha_uids: Iterable[str], when: str | None = None) -> int:
    """Flag alphas as backtested (any attempt, successful or not)."""
    uids = list(alpha_uids)
    if not uids:
        return 0
    connection = sqlite3.connect(str(alpha_db), timeout=60.0)
    try:
        connection.execute("PRAGMA busy_timeout = 60000")
        updated = 0
        timestamp = when or storage.utc_now()
        for start in range(0, len(uids), 200):
            batch = uids[start : start + 200]
            cursor = connection.execute(
                f"UPDATE alphas SET simulated = 1, simulated_at = ? "
                f"WHERE alpha_uid IN ({','.join('?' * len(batch))}) AND simulated IS NULL",
                [timestamp, *batch],
            )
            updated += cursor.rowcount
        connection.commit()
        return updated
    finally:
        connection.close()


class _ClientPool:
    """One authenticated client per worker thread (requests sessions are not thread safe)."""

    def __init__(self, credentials, *, poll_timeout: float, max_submit_retries: int) -> None:
        self._credentials = credentials
        self._poll_timeout = poll_timeout
        self._max_submit_retries = max_submit_retries
        self._local = threading.local()
        self._all: list[BrainSimulationClient] = []
        self._lock = threading.Lock()

    @property
    def client(self) -> BrainSimulationClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = BrainSimulationClient(
                self._credentials,
                poll_timeout=self._poll_timeout,
                max_submit_retries=self._max_submit_retries,
            )
            self._local.client = client
            with self._lock:
                self._all.append(client)
        return client

    def close(self) -> None:
        with self._lock:
            clients, self._all = self._all, []
        for client in clients:
            client.close()


def run_batch(
    alphas: Sequence[PendingAlpha],
    *,
    settings: SimulationSettings,
    workers: int,
    simulations_db: Path | str = DEFAULT_SIMULATIONS_DB,
    alpha_db: Path | str = DEFAULT_ALPHA_DB,
    timeout: float = 900.0,
    credentials_path: Path | str | None = None,
    progress_every: int = 10,
) -> BatchStats:
    """Backtest ``alphas`` with at most ``workers`` concurrent simulations."""
    stats = BatchStats(requested=len(alphas))
    if not alphas:
        return stats

    # Initialise the schema once; workers then open cheap connections.
    storage.connect(simulations_db).close()
    credentials = load_credentials(credentials_path)
    pool = _ClientPool(credentials, poll_timeout=timeout, max_submit_retries=60)
    started = time.monotonic()
    completed = 0

    def worker(alpha: PendingAlpha) -> SimulationOutcome:
        return run_simulation(
            alpha.expression,
            settings,
            db_path=simulations_db,
            timeout=timeout,
            client=pool.client,
            initialize_db=False,
        )

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(worker, alpha): alpha for alpha in alphas}
            for future in as_completed(futures):
                alpha = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
                    logger.exception("backtest crashed for %s", alpha.alpha_uid)
                    stats.failed += 1
                    stats.failures.append((alpha.alpha_uid, str(exc)))
                    outcome = None

                if outcome is not None:
                    if outcome.ok:
                        stats.ok += 1
                    else:
                        stats.failed += 1
                        stats.failures.append((alpha.alpha_uid, outcome.error or "failed"))
                    # A platform-level outcome counts as "backtested" even when it failed,
                    # so the alpha is not retried forever.
                    mark_simulated(alpha_db, [alpha.alpha_uid])
                # A crash before reaching the platform leaves the alpha unsimulated for a retry.
                completed += 1
                if progress_every and completed % progress_every == 0:
                    elapsed = time.monotonic() - started
                    rate = completed / elapsed if elapsed else 0.0
                    remaining = len(alphas) - completed
                    eta = remaining / rate if rate else 0.0
                    logger.info(
                        "progress %d/%d  ok=%d failed=%d  %.2f/min  eta %.0f min",
                        completed,
                        len(alphas),
                        stats.ok,
                        stats.failed,
                        rate * 60,
                        eta / 60,
                    )
    finally:
        pool.close()

    stats.duration_seconds = round(time.monotonic() - started, 1)
    return stats


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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
        prog="worldquant_consultant_project.simulation.batch",
        description="Backtest every un-simulated alpha from alpha.db with a thread pool.",
    )
    parser.add_argument("--alpha-db", type=Path, default=DEFAULT_ALPHA_DB, help="alpha database")
    parser.add_argument("--db", type=Path, default=DEFAULT_SIMULATIONS_DB, help="results database")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH, help="simulation_setting.json")
    parser.add_argument(
        "--concurrency-file",
        type=Path,
        default=DEFAULT_CONCURRENCY_PATH,
        help="file holding the account's max simultaneous simulations",
    )
    parser.add_argument("--workers", type=int, help="override the concurrency file")
    parser.add_argument("--limit", type=int, help="backtest at most N alphas")
    parser.add_argument("--timeout", type=float, default=900.0, help="per-simulation polling timeout")
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="report the pending count and exit")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        _, settings = load_settings_file(args.settings)
        workers = args.workers or load_max_concurrency(args.concurrency_file)
    except SettingsFileError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    if workers < 1:
        logger.error("workers must be >= 1")
        return EXIT_CONFIG

    try:
        pending = load_pending_alphas(args.alpha_db, limit=args.limit)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    logger.info(
        "settings: %s | workers=%d | pending=%d | db=%s",
        settings.payload,
        workers,
        len(pending),
        args.db,
    )
    if args.dry_run:
        print(f"pending alphas: {len(pending)}")
        for alpha in pending[:5]:
            print(f"  {alpha.alpha_uid}  {alpha.expression}")
        if len(pending) > 5:
            print(f"  ... and {len(pending) - 5} more")
        return EXIT_OK

    if not pending:
        print("nothing to backtest")
        return EXIT_OK

    try:
        stats = run_batch(
            pending,
            settings=settings,
            workers=workers,
            simulations_db=args.db,
            alpha_db=args.alpha_db,
            timeout=args.timeout,
            credentials_path=args.credentials,
            progress_every=args.progress_every,
        )
    except CredentialsError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except BrainSimulationError as exc:
        logger.error("BRAIN connection failed: %s", exc)
        return EXIT_CONFIG

    print(
        f"backtested {stats.requested} alphas in {stats.duration_seconds:.0f}s "
        f"(ok={stats.ok}, failed={stats.failed})"
    )
    for alpha_uid, reason in stats.failures[:10]:
        print(f"  failed {alpha_uid}: {reason[:120]}")
    if len(stats.failures) > 10:
        print(f"  ... and {len(stats.failures) - 10} more failures")

    connection = storage.connect(args.db, initialize=False)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM simulations WHERE completed = 0"
        ).fetchone()[0]
        print(f"rows in {args.db.name}: incomplete={remaining}")
    finally:
        connection.close()

    return EXIT_OK if stats.failed == 0 else EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

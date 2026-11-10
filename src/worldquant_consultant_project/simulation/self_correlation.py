"""Measure the self-correlation of runs that look submittable.

BRAIN leaves ``SELF_CORRELATION`` at ``PENDING`` after a simulation and only computes it
on demand (``GET /alphas/{id}/correlations/self`` answers an empty body plus
``Retry-After`` until the regression against the account's alphas finishes). This pass
asks for the real number and records the verdict:

``submittable``                 PASS when the max self-correlation is below the limit
``self_correlation``            the measured value
``self_correlation_checked_at`` when it was measured

The runner does this for every fresh run whose ``tentative_submittable`` came out 1; this
module backfills the rows that were written before the columns existed.

Usage:
    uv run python -m worldquant_consultant_project.simulation.self_correlation
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..acquisition.config import CredentialsError, load_credentials
from . import storage
from .batch import ClientPool
from .client import BrainSimulationError
from .runner import DEFAULT_DB_PATH as DEFAULT_ALPHA_DB
from .settings import DEFAULT_CONCURRENCY_PATH, SettingsFileError, load_max_concurrency

logger = logging.getLogger("simulation.self_correlation")

DEFAULT_LOG_DIR = DEFAULT_ALPHA_DB.parent / "logs"
#: The platform's quota for the correlation endpoint is tight: stop instead of hammering it.
MAX_CONSECUTIVE_RATE_LIMITS = 5

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PARTIAL = 3


@dataclass(frozen=True)
class PendingCorrelation:
    sim_uid: str
    braint_alpha_id: str


@dataclass
class CorrelationStats:
    checked: int = 0
    #: Set when the platform's quota for this endpoint stopped the pass.
    aborted: str | None = None
    measured: int = 0
    passed: int = 0
    failed: int = 0
    #: The platform had not finished computing the regression for these.
    unavailable: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0


def load_pending(
    alpha_db: Path | str, *, limit: int | None = None
) -> list[PendingCorrelation]:
    """Runs that passed every other check but are still missing the correlation verdict."""
    db_path = Path(alpha_db)
    if not db_path.is_file():
        raise FileNotFoundError(f"alpha database not found: {db_path}")
    connection = storage.connect(db_path)
    try:
        sql = (
            "SELECT s.sim_uid, d.braint_alpha_id "
            f"FROM {storage.SIMULATIONS_TABLE} AS s "
            f"JOIN {storage.DETAILS_TABLE} AS d ON d.sim_uid = s.sim_uid "
            f"WHERE s.{storage.TENTATIVE_COLUMN} = 1 "
            "AND s.submittable IS NULL "
            "AND s.completed = 1 "
            "AND d.braint_alpha_id IS NOT NULL "
            "ORDER BY s.simulated_at DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            PendingCorrelation(row["sim_uid"], row["braint_alpha_id"])
            for row in connection.execute(sql, params)
        ]
    finally:
        connection.close()


def record(
    connection: sqlite3.Connection,
    sim_uid: str,
    value: float,
    *,
    limit: float = storage.SELF_CORRELATION_LIMIT,
) -> str:
    """Store the measured correlation and its verdict; returns the verdict."""
    verdict = storage.submittable_from(value, limit=limit) or storage.SUBMITTABLE_FAIL
    connection.execute(
        f"UPDATE {storage.SIMULATIONS_TABLE} SET submittable = ?, self_correlation = ?, "
        "self_correlation_checked_at = ? WHERE sim_uid = ?",
        (verdict, value, storage.utc_now(), sim_uid),
    )
    connection.commit()
    return verdict


def measure_pending(
    pending: Sequence[PendingCorrelation],
    *,
    alpha_db: Path | str = DEFAULT_ALPHA_DB,
    workers: int = 2,
    timeout: float = 60.0,
    limit: float = storage.SELF_CORRELATION_LIMIT,
    credentials_path: Path | str | None = None,
    pool: Any | None = None,
    pause: float = 1.0,
) -> CorrelationStats:
    """Ask the platform for each pending correlation and write the verdict."""
    stats = CorrelationStats()
    if not pending:
        return stats

    owned_pool = pool is None
    if owned_pool:
        pool = ClientPool(
            load_credentials(credentials_path),
            poll_timeout=timeout,
            max_submit_retries=1,
        )

    def worker(item: PendingCorrelation) -> tuple[PendingCorrelation, float | None, str | None]:
        try:
            value = pool.client.get_self_correlation(
                item.braint_alpha_id, timeout=timeout
            )
            return item, value, None
        except BrainSimulationError as exc:
            return item, None, str(exc)
        finally:
            if pause > 0:
                time.sleep(pause)

    completed = 0
    rate_limited = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(worker, item) for item in pending]
            for future in as_completed(futures):
                item, value, error = future.result()
                stats.checked += 1
                completed += 1
                if error:
                    stats.failures.append((item.sim_uid, error))
                    if "429" in error or "rate limit" in error.lower():
                        rate_limited += 1
                        if rate_limited >= MAX_CONSECUTIVE_RATE_LIMITS:
                            stats.aborted = (
                                f"{rate_limited} alphas in a row were rate limited by the "
                                "platform; the rest keeps its NULL verdict for a later pass"
                            )
                            logger.error("stopping: %s", stats.aborted)
                            for pending_future in futures:
                                pending_future.cancel()
                            break
                    else:
                        rate_limited = 0
                elif value is None:
                    stats.unavailable += 1
                    rate_limited = 0
                else:
                    rate_limited = 0
                    connection = storage.connect(alpha_db, initialize=False)
                    try:
                        verdict = record(connection, item.sim_uid, value, limit=limit)
                    finally:
                        connection.close()
                    stats.measured += 1
                    if verdict == storage.SUBMITTABLE_PASS:
                        stats.passed += 1
                    else:
                        stats.failed += 1
                if completed % 10 == 0:
                    logger.info(
                        "progress %d/%d measured=%d pass=%d fail=%d unavailable=%d",
                        completed,
                        len(pending),
                        stats.measured,
                        stats.passed,
                        stats.failed,
                        stats.unavailable,
                    )
    finally:
        if owned_pool:
            pool.close()
    return stats


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"self-correlation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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
        prog="worldquant_consultant_project.simulation.self_correlation",
        description=(
            "Measure the self-correlation of submittable runs and record PASS/FAIL."
        ),
    )
    parser.add_argument("--alpha-db", type=Path, default=DEFAULT_ALPHA_DB)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="measure at most N runs",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=storage.SELF_CORRELATION_LIMIT,
        help=f"self-correlation below this passes (default {storage.SELF_CORRELATION_LIMIT})",
    )
    parser.add_argument(
        "--concurrency-file",
        type=Path,
        default=DEFAULT_CONCURRENCY_PATH,
        help="file holding the account's max simultaneous requests",
    )
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-alpha wait for the platform")
    parser.add_argument(
        "--pause", type=float, default=1.0, help="seconds to wait between two alphas"
    )
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_dir)

    try:
        pending = load_pending(args.alpha_db, limit=args.limit)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    print(f"runs waiting for a self-correlation verdict: {len(pending)}")
    if args.dry_run or not pending:
        for item in pending[:10]:
            print(f"  {item.sim_uid[:8]}  {item.braint_alpha_id}")
        if len(pending) > 10:
            print(f"  ... and {len(pending) - 10} more")
        return EXIT_OK

    try:
        workers = args.workers or load_max_concurrency(args.concurrency_file)
    except SettingsFileError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    try:
        stats = measure_pending(
            pending,
            alpha_db=args.alpha_db,
            workers=workers,
            timeout=args.timeout,
            limit=args.threshold,
            credentials_path=args.credentials,
            pause=args.pause,
        )
    except CredentialsError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    print(
        f"checked {stats.checked}: measured={stats.measured} "
        f"(PASS={stats.passed}, FAIL={stats.failed}), unavailable={stats.unavailable}, "
        f"errors={len(stats.failures)}"
    )
    for sim_uid, reason in stats.failures[:5]:
        print(f"  failed {sim_uid[:8]}: {reason[:120]}")
    return EXIT_OK if not stats.failures else EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Batch backtests: run every alpha ``alpha.db`` has no verdict for yet, with a thread pool.

Concurrency comes from ``maximum_concurrent_number`` (the account's simultaneous
simulation allowance) and the parameters come from ``batch_simulation_settings.json``.
Results land in ``alpha.db`` next to the factors themselves, keyed by
``alpha_uid``; re-running the same expression with the same settings overwrites
the previous row instead of adding a new one.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..acquisition.config import CredentialsError, load_credentials
from . import storage
from .client import BrainSimulationClient, BrainSimulationError
from .runner import DEFAULT_DB_PATH as DEFAULT_ALPHA_DB
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

DEFAULT_LOG_DIR = DEFAULT_ALPHA_DB.parent / "logs"

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
    # The platform evaluated the factor and rejected it (counts as backtested).
    failed: int = 0
    # Platform-side failures: the factor was never evaluated, so it stays pending.
    platform_failed: int = 0
    # Submitted but still computing when we stopped waiting; left for reconciliation.
    pending: int = 0
    duration_seconds: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
    last_platform_error: str | None = None
    abort_reason: str | None = None


@dataclass
class ReconcileStats:
    """Outcome of finalising rows left behind by an interrupted run."""

    checked: int = 0
    recovered: int = 0
    failed: int = 0
    still_running: int = 0
    missing: int = 0
    # Rows the platform kept computing past the abandon window.
    abandoned: int = 0
    # Factors the platform judged (finished, or rejected the expression itself).
    resolved_alpha_uids: list[str] = field(default_factory=list)
    # Give-up rows (still computing, gone from the platform): the factor stays retryable.
    interrupted_alpha_uids: list[str] = field(default_factory=list)


def load_pending_alphas(
    alpha_db: Path | str, *, limit: int | None = None
) -> list[PendingAlpha]:
    """Alphas the platform has not judged yet, in insertion order."""
    db_path = Path(alpha_db)
    if not db_path.is_file():
        raise FileNotFoundError(f"alpha database not found: {db_path}")
    connection = sqlite3.connect(str(db_path), timeout=60.0)
    connection.row_factory = sqlite3.Row
    try:
        # Repeats are excluded through the runs themselves: a factor counts as backtested
        # only once one of its simulations produced results or was rejected as an expression.
        sql = (
            "SELECT alpha_uid, expression FROM alphas "
            f"WHERE NOT {storage.BACKTESTED_SQL} ORDER BY alpha_uid"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [PendingAlpha(row["alpha_uid"], row["expression"]) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _age_seconds(simulated_at: str | None) -> float:
    """Seconds since the row's start timestamp; 0 when it cannot be parsed."""
    if not simulated_at:
        return 0.0
    try:
        started = datetime.fromisoformat(simulated_at)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - started).total_seconds(), 0.0)


def purge_platform_errors(*, alpha_db: Path | str) -> tuple[int, int]:
    """Delete platform-caused failures, which also makes those factors retryable again.

    Returns ``(deleted_runs, refreshed_alphas)``.
    """
    connection = storage.connect(alpha_db)
    try:
        rows = storage.platform_error_rows(connection)
        if not rows:
            return 0, 0
        alpha_uids = {row["alpha_uid"] for row in rows}
        deleted = storage.delete_simulations(connection, [row["sim_uid"] for row in rows])
        refreshed = storage.refresh_alpha_metrics(connection, alpha_uids)
    finally:
        connection.close()

    logger.info(
        "purged %d platform-caused run(s); refreshed %d alpha(s)", deleted, refreshed
    )
    return deleted, refreshed


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


def reconcile_interrupted(
    connection: sqlite3.Connection,
    client: BrainSimulationClient,
    *,
    stale_after_seconds: float = 1800.0,
    abandon_after_seconds: float = 6 * 3600.0,
) -> ReconcileStats:
    """Finalise rows created at submit time whose result was never recorded.

    Such a row appears when the process stops while the platform is still computing, or
    when a live run hits its polling timeout. The platform keeps the simulation, so the id
    is polled once here: finished runs get their results (or error) written, ids the
    platform no longer knows are flagged as interrupted, and runs still in flight are left
    for the next pass until they exceed ``abandon_after_seconds`` (platform still
    computing) or ``stale_after_seconds`` (no Retry-After, progress only).
    """
    stats = ReconcileStats()
    rows = storage.pending_placeholders(connection)
    if not rows:
        return stats

    logger.info("reconciling %d row(s) left by an interrupted run", len(rows))
    for row in rows:
        stats.checked += 1
        sim_uid = row["sim_uid"]
        sim_id = row["braint_sim_id"]
        settings = json.loads(row["settings_json"])
        base = {
            "sim_uid": sim_uid,
            "alpha_uid": row["alpha_uid"],
            "expression": row["expression"],
            "settings": settings,
            "simulated_at": row["simulated_at"],
            "braint_sim_id": sim_id,
        }
        try:
            body = client.simulation_snapshot(sim_id)
        except BrainSimulationError as exc:
            logger.warning("cannot check %s (%s): %s", sim_id, sim_uid, exc)
            continue

        if body is None:
            stats.missing += 1
            stats.interrupted_alpha_uids.append(row["alpha_uid"])
            storage.write_outcome(
                connection,
                **base,
                completed=False,
                error="interrupted: simulation no longer exists on the platform",
                finished_at=storage.utc_now(),
            )
            logger.info("%s: id unknown to the platform, marked interrupted", sim_uid)
            continue

        status = str(body.get("status") or "").upper()
        if status in ("ERROR", "FAIL", "FAILED"):
            message = str(body.get("message") or status)[:1000]
            stats.failed += 1
            # Same rule as a live failure: only a factor-caused message means the factor
            # was judged; a platform-side one leaves it retryable.
            if storage.classify_error_text(message) == storage.ERROR_KIND_PLATFORM:
                stats.interrupted_alpha_uids.append(row["alpha_uid"])
            else:
                stats.resolved_alpha_uids.append(row["alpha_uid"])
            storage.write_outcome(
                connection,
                **base,
                completed=False,
                error=message,
                simulation_body=body,
                finished_at=storage.utc_now(),
            )
            logger.info("%s: platform reported %s", sim_uid, status)
            continue

        if not body.get("finished"):
            age = _age_seconds(row["simulated_at"])
            waiting = bool(body.get("waiting"))
            # A platform that keeps answering "Retry-After" is still computing, so it gets
            # the longer abandon window; a bare progress reply is judged by the stale window.
            limit = abandon_after_seconds if waiting else stale_after_seconds
            if age < limit:
                stats.still_running += 1
                logger.info(
                    "%s: still running on the platform (age %.0f min, limit %.0f min), "
                    "left for the next pass",
                    sim_uid,
                    age / 60,
                    limit / 60,
                )
                continue

            stats.abandoned += 1
            stats.interrupted_alpha_uids.append(row["alpha_uid"])
            progress = body.get("progress")
            detail = f"progress={progress}" if progress is not None else "no progress reported"
            reason = "platform still computing" if waiting else "stalled"
            storage.write_outcome(
                connection,
                **base,
                completed=False,
                error=f"interrupted: {reason} after {age / 3600:.1f}h ({detail})",
                simulation_body=body,
                finished_at=storage.utc_now(),
            )
            logger.info("%s: %s for %.1fh, marked interrupted", sim_uid, reason, age / 3600)
            continue

        alpha_id = body.get("alpha")
        if not alpha_id:
            stats.missing += 1
            stats.interrupted_alpha_uids.append(row["alpha_uid"])
            storage.write_outcome(
                connection,
                **base,
                completed=False,
                error="interrupted: simulation finished without an alpha id",
                simulation_body=body,
                finished_at=storage.utc_now(),
            )
            continue

        alpha_body = client.get_alpha(str(alpha_id))
        storage.write_outcome(
            connection,
            **base,
            completed=True,
            braint_alpha_id=str(alpha_id),
            finished_at=storage.utc_now(),
            simulation_body=body,
            alpha_body=alpha_body,
        )
        stats.recovered += 1
        stats.resolved_alpha_uids.append(row["alpha_uid"])
        logger.info("%s: recovered result for alpha %s", sim_uid, alpha_id)

    logger.info(
        "reconcile done: checked=%d recovered=%d failed=%d missing=%d running=%d abandoned=%d",
        stats.checked,
        stats.recovered,
        stats.failed,
        stats.missing,
        stats.still_running,
        stats.abandoned,
    )
    return stats


def outcome_bucket(outcome: SimulationOutcome) -> str:
    """Classify a finished run: ``ok`` / ``pending`` / ``factor`` / ``platform``.

    Only ``ok`` and ``factor`` mean the alpha should be marked as backtested; ``pending``
    is still computing and ``platform`` never evaluated the factor.
    """
    if outcome.ok:
        return "ok"
    if outcome.pending:
        return "pending"
    if outcome.factor_caused:
        return "factor"
    return "platform"


def run_batch(
    alphas: Sequence[PendingAlpha],
    *,
    settings: SimulationSettings,
    workers: int,
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
    storage.connect(alpha_db).close()
    credentials = load_credentials(credentials_path)
    pool = _ClientPool(credentials, poll_timeout=timeout, max_submit_retries=60)
    started = time.monotonic()
    completed = 0
    consecutive_platform = 0
    # A run of platform failures means the account (quota, session, outage) is the problem,
    # so the batch stops instead of burning through the whole list.
    platform_streak_limit = max(5, workers * 2)

    def should_abort() -> bool:
        if stats.abort_reason:
            return True
        if consecutive_platform >= platform_streak_limit:
            stats.abort_reason = (
                f"{consecutive_platform} consecutive platform failures "
                f"(last: {stats.last_platform_error})"
            )
            return True
        return False

    def worker(alpha: PendingAlpha) -> SimulationOutcome:
        return run_simulation(
            alpha.expression,
            settings,
            db_path=alpha_db,
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
                    bucket = outcome_bucket(outcome)
                    if bucket == "ok":
                        # run_simulation already flags the factor as backtested.
                        stats.ok += 1
                        consecutive_platform = 0
                    elif bucket == "pending":
                        # Still computing upstream; the row stays a placeholder and the
                        # alpha is not marked, so the next pass can finish the job.
                        stats.pending += 1
                        consecutive_platform = 0
                    elif bucket == "factor":
                        # The platform judged the factor and rejected it: retrying the same
                        # expression would fail again, so it counts as backtested.
                        stats.failed += 1
                        stats.failures.append((alpha.alpha_uid, outcome.error or "failed"))
                        consecutive_platform = 0
                    else:
                        # Platform-side problem (quota, session, server): the factor was
                        # never evaluated, so leave it pending for the next run.
                        stats.platform_failed += 1
                        stats.failures.append((alpha.alpha_uid, outcome.error or "platform error"))
                        stats.last_platform_error = outcome.error
                        consecutive_platform += 1
                        if outcome.limit_scope == "daily":
                            # No further submission can succeed today.
                            stats.abort_reason = "daily simulation limit reached"
                # A crash before reaching the platform leaves the alpha without a verdict.
                completed += 1

                if should_abort():
                    logger.error(
                        "stopping the batch: %s (%d consecutive platform failures, "
                        "%d total); the remaining alphas stay pending",
                        stats.abort_reason,
                        consecutive_platform,
                        stats.platform_failed,
                    )
                    for pending_future in futures:
                        pending_future.cancel()
                    break

                if progress_every and completed % progress_every == 0:
                    elapsed = time.monotonic() - started
                    rate = completed / elapsed if elapsed else 0.0
                    remaining = len(alphas) - completed
                    eta = remaining / rate if rate else 0.0
                    logger.info(
                        "progress %d/%d  ok=%d factor_failed=%d platform_failed=%d pending=%d  "
                        "%.2f/min  eta %.0f min",
                        completed,
                        len(alphas),
                        stats.ok,
                        stats.failed,
                        stats.platform_failed,
                        stats.pending,
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
        description="Backtest every alpha without a verdict yet, using a thread pool.",
    )
    parser.add_argument(
        "--alpha-db",
        type=Path,
        default=DEFAULT_ALPHA_DB,
        help="factor database that also stores the backtests",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="deprecated alias for --alpha-db (kept so old commands keep working)",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="batch_simulation_settings.json",
    )
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
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="only finalise rows left by an interrupted run, then exit",
    )
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help="skip finalising rows left by an interrupted run",
    )
    parser.add_argument(
        "--stale-minutes",
        type=float,
        default=30.0,
        help="age after which a simulation reporting only progress counts as interrupted (default 30)",
    )
    parser.add_argument(
        "--abandon-after-hours",
        type=float,
        default=6.0,
        help="age after which a simulation the platform still reports as computing is abandoned (default 6)",
    )
    parser.add_argument(
        "--purge-platform-errors",
        action="store_true",
        help="delete runs that failed for platform reasons so their alphas can be retried",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def _run_reconcile(args: argparse.Namespace) -> ReconcileStats | None:
    """Open the factor database and finalise rows left by an interrupted run."""
    try:
        credentials = load_credentials(args.credentials)
    except CredentialsError as exc:
        logger.error("%s", exc)
        return None

    connection = storage.connect(args.alpha_db)
    client = BrainSimulationClient(credentials)
    try:
        stats = reconcile_interrupted(
            connection,
            client,
            stale_after_seconds=max(args.stale_minutes, 0) * 60,
            abandon_after_seconds=max(args.abandon_after_hours, 0) * 3600,
        )
        # Each recovered or failed run refreshed its factor through write_outcome.
        connection.commit()
    except BrainSimulationError as exc:
        logger.error("cannot reconcile interrupted rows: %s", exc)
        return None
    finally:
        client.close()
        connection.close()

    if stats.resolved_alpha_uids:
        logger.info(
            "%d alpha(s) now have a verdict from the platform after reconciliation",
            len(stats.resolved_alpha_uids),
        )
    if stats.interrupted_alpha_uids:
        logger.info(
            "%d alpha(s) stay retryable (no verdict from the platform)",
            len(stats.interrupted_alpha_uids),
        )
    return stats


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db is not None:
        logger.warning("--db is deprecated (backtests live in alpha.db); use --alpha-db")
        args.alpha_db = args.db
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
        args.alpha_db,
    )

    if args.dry_run:
        print(f"pending alphas: {len(pending)}")
        for alpha in pending[:5]:
            print(f"  {alpha.alpha_uid}  {alpha.expression}")
        if len(pending) > 5:
            print(f"  ... and {len(pending) - 5} more")
        return EXIT_OK

    if args.purge_platform_errors:
        deleted, refreshed = purge_platform_errors(alpha_db=args.alpha_db)
        print(f"deleted {deleted} platform-caused run(s); refreshed {refreshed} alpha(s)")
        return EXIT_OK

    reconcile = not args.no_reconcile
    if reconcile or args.reconcile_only:
        stats_reconcile = _run_reconcile(args)
        if stats_reconcile is None:
            return EXIT_CONFIG
        print(
            f"reconciled interrupted rows: checked={stats_reconcile.checked} "
            f"recovered={stats_reconcile.recovered} failed={stats_reconcile.failed} "
            f"missing={stats_reconcile.missing} running={stats_reconcile.still_running} "
            f"abandoned={stats_reconcile.abandoned}"
        )
        if args.reconcile_only:
            return EXIT_OK
        # Results recovered above may complete alphas that are still marked pending.
        try:
            pending = load_pending_alphas(args.alpha_db, limit=args.limit)
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG

    if not pending:
        print("nothing to backtest")
        return EXIT_OK

    try:
        stats = run_batch(
            pending,
            settings=settings,
            workers=workers,
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
        f"(ok={stats.ok}, factor_failed={stats.failed}, "
        f"platform_failed={stats.platform_failed}, pending={stats.pending})"
    )
    if stats.abort_reason:
        print(f"batch stopped early: {stats.abort_reason}")
        print("  remaining alphas were left pending and can be resumed later")
    for alpha_uid, reason in stats.failures[:10]:
        print(f"  failed {alpha_uid}: {reason[:120]}")
    if len(stats.failures) > 10:
        print(f"  ... and {len(stats.failures) - 10} more failures")

    connection = storage.connect(args.alpha_db, initialize=False)
    try:
        remaining = connection.execute(
            f"SELECT COUNT(*) FROM {storage.SIMULATIONS_TABLE} WHERE completed = 0"
        ).fetchone()[0]
        print(f"rows in {args.alpha_db.name}: incomplete={remaining}")
    finally:
        connection.close()

    return EXIT_OK if stats.failed == 0 else EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

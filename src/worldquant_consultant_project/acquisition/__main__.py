"""Command line entry point for the BRAIN data acquisition stage.

Usage:
    uv run python -m worldquant_consultant_project.acquisition --help
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import db
from .client import BrainAuthError, BrainClient
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_DELAY,
    DEFAULT_INSTRUMENT_TYPE,
    DEFAULT_LOG_DIR,
    DEFAULT_REGION,
    UNIVERSE_CANDIDATES,
    CredentialsError,
    load_credentials,
)
from .crawl import CrawlConfig, run_crawl, dry_run

logger = logging.getLogger("acquisition")

EXIT_OK = 0
EXIT_AUTH = 2
EXIT_PARTIAL = 3


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.acquisition",
        description="Download every BRAIN data set and data field into SQLite.",
    )
    scope = parser.add_argument_group("scope")
    scope.add_argument("--instrument-type", default=DEFAULT_INSTRUMENT_TYPE)
    scope.add_argument("--region", default=DEFAULT_REGION)
    scope.add_argument("--delay", type=int, default=DEFAULT_DELAY)
    scope.add_argument(
        "--universes",
        help="comma separated list; default probes " + ", ".join(UNIVERSE_CANDIDATES),
    )
    scope.add_argument("--datasets", help="comma separated data set ids (debug / smoke runs)")
    scope.add_argument("--exclude-types", help="comma separated field types to skip, e.g. VECTOR")

    run = parser.add_argument_group("run")
    run.add_argument("--max-fields", type=int, help="stop each data set after N fields (smoke runs)")
    run.add_argument("--force", action="store_true", help="re-crawl data sets already marked done")
    run.add_argument("--dry-run", action="store_true", help="probe the API without writing to the database")
    run.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite path (default {DEFAULT_DB_PATH})")
    run.add_argument("--credentials", type=Path, help="credentials JSON path (default <project>/credentials.json)")
    run.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    run.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"crawl-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
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


def print_summary(connection: sqlite3.Connection) -> None:
    rows = db.summary(connection)
    if not rows:
        print("no data stored yet")
        return
    print(f"{'universe':<22}{'datasets':>10}{'fields':>10}")
    for row in rows:
        print(f"{row['universe']:<22}{row['datasets']:>10}{row['fields']:>10}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        credentials = load_credentials(args.credentials)
    except CredentialsError as exc:
        logger.error("%s", exc)
        return EXIT_AUTH

    universes = _csv(args.universes) or list(UNIVERSE_CANDIDATES)
    config = CrawlConfig(
        instrument_type=args.instrument_type,
        region=args.region,
        delay=args.delay,
        universes=universes,
        max_fields=args.max_fields,
        force=args.force,
        dataset_filter=_csv(args.datasets),
        exclude_types=frozenset(t.upper() for t in (_csv(args.exclude_types) or [])),
    )
    logger.info(
        "scope instrument=%s region=%s delay=%s universes=%s max_fields=%s db=%s",
        config.instrument_type,
        config.region,
        config.delay,
        ",".join(config.universes),
        config.max_fields,
        args.db,
    )

    try:
        with BrainClient(credentials) as client:
            if args.dry_run:
                report = dry_run(client, config)
                for key, value in report.items():
                    logger.info("dry-run %s: %s", key, value)
                return EXIT_OK

            connection = db.connect(args.db)
            try:
                stats = run_crawl(client, connection, config)
                print_summary(connection)
            finally:
                connection.close()
    except BrainAuthError as exc:
        logger.error("authentication failed: %s", exc)
        return EXIT_AUTH

    logger.info(
        "done: universes valid=%d invalid=%d, data sets done=%d failed=%d, fields stored=%d",
        stats.universes_valid,
        stats.universes_invalid,
        stats.datasets_done,
        len(stats.datasets_failed),
        stats.fields_written,
    )
    if stats.datasets_failed:
        for universe, dataset_id in stats.datasets_failed:
            logger.warning("failed: %s/%s (re-run with --resume to retry)", universe, dataset_id)
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

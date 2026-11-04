"""CLI: backtest one Fast Expression factor and store the result in SQLite.

Usage:
    uv run python -m worldquant_consultant_project.simulation --expression "rank(close)"
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from ..acquisition.config import CredentialsError, load_credentials
from . import storage
from .client import BrainSimulationClient, BrainSimulationError
from .runner import DEFAULT_DB_PATH, run_simulation
from .settings import SimulationSettings

logger = logging.getLogger("simulation")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_SIMULATION_FAILED = 3

DEFAULT_LOG_DIR = DEFAULT_DB_PATH.parent / "logs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.simulation",
        description="Backtest a WorldQuant BRAIN Fast Expression factor and store it in simulations.db.",
    )
    parser.add_argument("--expression", required=True, help="Fast Expression factor, e.g. \"rank(ts_delta(close, 5))\"")
    parser.add_argument("--region", default=None, help="default USA")
    parser.add_argument("--universe", default=None, help="default TOP3000")
    parser.add_argument("--delay", type=int, default=None, help="default 1")
    parser.add_argument("--decay", type=int, default=None, help="default 0")
    parser.add_argument("--neutralization", default=None, help="default SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=None, help="default 0.08")
    parser.add_argument("--instrument-type", default=None, help="default EQUITY")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite path (default {DEFAULT_DB_PATH})")
    parser.add_argument("--credentials", type=Path, default=None, help="credentials JSON path")
    parser.add_argument("--timeout", type=float, default=900.0, help="polling timeout in seconds (default 900)")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"simulation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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


def _settings_from_args(args: argparse.Namespace) -> SimulationSettings:
    overrides = {
        "region": args.region,
        "universe": args.universe,
        "delay": args.delay,
        "decay": args.decay,
        "neutralization": args.neutralization,
        "truncation": args.truncation,
        "instrument_type": args.instrument_type,
    }
    return SimulationSettings.from_dict({k: v for k, v in overrides.items() if v is not None})


def print_outcome(outcome) -> None:
    print(f"status      : {outcome.status}")
    print(f"sim_uid     : {outcome.sim_uid}")
    print(f"expression  : {outcome.expression}")
    if outcome.alpha_id:
        print(f"alpha_id    : {outcome.alpha_id}")
    if outcome.duration_seconds is not None:
        print(f"duration    : {outcome.duration_seconds:.1f}s")
    if outcome.error:
        print(f"error       : {outcome.error}")
    metrics = outcome.metrics.get("is") or {}
    if metrics:
        keys = ("sharpe", "fitness", "returns", "turnover", "drawdown", "margin")
        summary = ", ".join(f"{key}={metrics.get(key)}" for key in keys if key in metrics)
        print(f"in-sample   : {summary}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        settings = _settings_from_args(args)
    except ValueError as exc:
        logger.error("invalid setting: %s", exc)
        return EXIT_CONFIG

    try:
        credentials = load_credentials(args.credentials)
    except CredentialsError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    client = BrainSimulationClient(credentials, poll_timeout=args.timeout)
    try:
        client.authenticate()
        outcome = run_simulation(
            args.expression,
            settings,
            db_path=args.db,
            timeout=args.timeout,
            client=client,
        )
    except BrainSimulationError as exc:
        logger.error("authentication or connection failed: %s", exc)
        return EXIT_CONFIG
    finally:
        client.close()

    print_outcome(outcome)
    connection = storage.connect(args.db)
    try:
        rows = storage.recent(connection, limit=3)
        if rows:
            print("recent runs:")
            for row in rows:
                print(
                    f"  {row['submitted_at']}  {row['status']:<9} "
                    f"sharpe={row['is_sharpe']}  fitness={row['is_fitness']}  "
                    f"{row['universe']}/d{row['delay']}/decay{row['decay']}"
                )
    finally:
        connection.close()

    return EXIT_OK if outcome.ok else EXIT_SIMULATION_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

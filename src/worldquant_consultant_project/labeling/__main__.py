"""CLI entry point for field labeling.

Usage:
    uv run python -m worldquant_consultant_project.labeling --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import storage
from .client import DeepSeekClient
from .config import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_DIR,
    DEFAULT_MODEL,
    DEFAULT_PROMPT_PATH,
    LABEL_COLUMNS,
    LabelingConfigError,
    load_api_key,
    load_system_prompt,
    prompt_fingerprint,
)
from .label import LabelConfig, preview_prompt, run_labeling

logger = logging.getLogger("labeling")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PARTIAL = 3


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.labeling",
        description="Label BRAIN data fields with a DeepSeek model and store the labels in brain.db.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"SQLite path (default {DEFAULT_DB_PATH})")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH, help="system prompt file")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH, help="credentials JSON path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model name (default {DEFAULT_MODEL})")
    parser.add_argument("--dataset", help="comma separated dataset ids to restrict labeling")
    parser.add_argument("--limit", type=int, help="label at most N fields (smoke runs)")
    parser.add_argument("--workers", type=int, default=8, help="concurrent requests (default 8)")
    parser.add_argument("--thinking", action="store_true", help="enable the model's thinking mode")
    parser.add_argument("--force", action="store_true", help="re-label fields that already succeeded")
    parser.add_argument("--dry-run", action="store_true", help="show the prompt and pending count without calling the API")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"label-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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


def print_summary(connection) -> None:
    rows = list(
        connection.execute(
            """
            SELECT COUNT(*) AS fields,
                   SUM(CASE WHEN deepseek_category IS NOT NULL THEN 1 ELSE 0 END) AS labeled
            FROM (SELECT DISTINCT dataset_id, field_id, deepseek_category FROM data_fields)
            """
        )
    )
    if not rows:
        return
    row = rows[0]
    print(f"labeled fields: {row['labeled']} / {row['fields']}")
    for column in LABEL_COLUMNS.values():
        print(f"  {column}")
    print("distribution:")
    for dimension, column in LABEL_COLUMNS.items():
        counts = connection.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM ("
            f" SELECT DISTINCT dataset_id, field_id, {column} FROM data_fields)"
            f" WHERE {column} IS NOT NULL GROUP BY 1 ORDER BY n DESC"
        ).fetchall()
        if counts:
            summary = ", ".join(f"{r['value']}={r['n']}" for r in counts)
            print(f"  {dimension:<15} {summary}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        system_prompt = load_system_prompt(args.prompt)
    except LabelingConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    prompt_sha = prompt_fingerprint(system_prompt)

    try:
        connection = storage.connect(args.db)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    try:
        added = storage.ensure_schema(connection)
        if added:
            logger.info("added columns: %s", ", ".join(added))

        config = LabelConfig(
            model=args.model,
            prompt_sha=prompt_sha,
            workers=args.workers,
            thinking=args.thinking,
        )
        fields = storage.load_fields(
            connection,
            model=args.model,
            prompt_sha=prompt_sha,
            dataset_filter=_csv(args.dataset),
            limit=args.limit,
            force=args.force,
        )
        logger.info(
            "model=%s workers=%d thinking=%s pending=%d prompt_sha=%s",
            args.model,
            args.workers,
            args.thinking,
            len(fields),
            prompt_sha[:12],
        )

        if args.dry_run:
            preview = preview_prompt(fields)
            if not preview:
                print("nothing pending")
                return EXIT_OK
            print(f"field: {preview['field']}")
            print(f"pending fields: {preview['pending']}")
            print("--- user prompt ---")
            print(preview["user_prompt"], end="")
            return EXIT_OK

        if not fields:
            print("nothing pending")
            print_summary(connection)
            return EXIT_OK

        try:
            api_key = load_api_key(args.credentials)
        except LabelingConfigError as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG

        with DeepSeekClient(api_key) as client:
            stats = run_labeling(
                client,
                connection,
                fields,
                system_prompt=system_prompt,
                config=config,
                show_progress=sys.stderr.isatty(),
            )
        if args.limit is None:
            print_summary(connection)
    finally:
        connection.close()

    logger.info(
        "done: ok=%d invalid=%d error=%d tokens_in=%d tokens_out=%d",
        stats.ok,
        stats.invalid,
        stats.error,
        stats.prompt_tokens,
        stats.completion_tokens,
    )

    if stats.invalid + stats.error:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

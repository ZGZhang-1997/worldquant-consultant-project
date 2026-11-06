"""CLI: generate Fast Expression alphas from templates and label-filtered fields.

Usage:
    uv run python -m worldquant_consultant_project.Template_based_alpha_generate --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from ..acquisition.config import DEFAULT_DB_PATH as DEFAULT_BRAIN_DB
from . import storage
from .generator import GenerationError, generate_alphas
from .providers import LabelSourceError, resolve_label_source
from .templates import (
    DEFAULT_TEMPLATE_PATH,
    TemplateError,
    load_templates,
    select_templates,
)

logger = logging.getLogger("alpha_generate")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_EMPTY = 3

DEFAULT_LOG_DIR = DEFAULT_BRAIN_DB.parent / "logs"


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.Template_based_alpha_generate",
        description="Fill alpha templates with label-filtered data fields (cartesian per slot).",
    )
    parser.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATE_PATH, help="template JSON path")
    parser.add_argument("--brain-db", type=Path, default=DEFAULT_BRAIN_DB, help="catalogue database")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH, help="output alpha database")
    parser.add_argument(
        "--label-source",
        default="union",
        help="union (default), intersection, or a provider name such as deepseek / gemini",
    )
    parser.add_argument("--template", help="comma separated template ids to generate (default: all)")
    parser.add_argument("--limit-per-template", type=int, help="generate at most N alphas per template")
    parser.add_argument("--dry-run", action="store_true", help="report candidate and combination counts only")
    parser.add_argument("--clear", action="store_true", help="delete existing alphas before inserting")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"alpha-generate-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    try:
        templates = select_templates(load_templates(args.templates), _csv(args.template))
    except TemplateError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    if not args.brain_db.is_file():
        logger.error("catalogue database not found: %s", args.brain_db)
        return EXIT_CONFIG
    brain = sqlite3.connect(str(args.brain_db))
    brain.row_factory = sqlite3.Row
    try:
        source = resolve_label_source(brain, args.label_source)
        logger.info("label source: %s", source.name)
        generated, pools, duplicates = generate_alphas(
            brain,
            templates,
            source,
            limit_per_template=args.limit_per_template,
        )
    except (LabelSourceError, GenerationError) as exc:
        logger.error("%s", exc)
        return EXIT_EMPTY
    finally:
        brain.close()

    for template in templates:
        pool = pools.get(template.template_id)
        if not pool:
            continue
        slots = ", ".join(f"{name}={len(items)}" for name, items in pool.slots.items())
        print(f"{template.template_id:<38} {slots}  combos={pool.combination_count}")

    print(f"generated: {len(generated)} unique expressions (duplicates skipped: {duplicates})")

    if args.dry_run:
        return EXIT_OK

    connection = storage.connect(args.db)
    try:
        if args.clear:
            storage.clear(connection)
            logger.info("cleared existing alphas")
        already = storage.exists(connection, [alpha.expression_hash for alpha in generated])
        fresh = [alpha for alpha in generated if alpha.expression_hash not in already]
        run_id = storage.new_run_id()
        inserted = storage.save(connection, fresh, label_source=source.name, run_id=run_id)
        logger.info(
            "run %s: inserted %d alphas, skipped %d already present",
            run_id,
            inserted,
            len(already),
        )
        print(f"inserted {inserted} alphas into {args.db} (run_id={run_id})")
        for row in storage.summary(connection):
            print(
                f"  {row['template_id']:<38} {row['alphas']:>6} alphas "
                f"({row['simulated']} simulated)"
            )
    finally:
        connection.close()

    return EXIT_OK if generated else EXIT_EMPTY


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

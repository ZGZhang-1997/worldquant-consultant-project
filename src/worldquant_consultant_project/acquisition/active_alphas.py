"""Mirror the account's submitted (ACTIVE) alphas into ``alpha.db``.

The platform exposes them through ``GET /users/self/alphas?status=ACTIVE``: each item
carries the Fast Expression, the full backtest settings, the in-sample metrics, the
submission checks, the grade and ``dateSubmitted``. Everything is written to
``active_alphas`` and linked to ``alphas`` (a factor missing locally is registered on the
fly), so a submitted factor is never picked up by a later batch again.

Usage:
    uv run python -m worldquant_consultant_project.acquisition.active_alphas
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .client import BrainApiError, BrainClient
from .config import (
    DEFAULT_LOG_DIR,
    Credentials,
    CredentialsError,
    load_credentials,
)
from ..simulation import storage

logger = logging.getLogger("acquisition.active_alphas")

#: Status the platform uses for a submitted (live) alpha. "SUBMITTED" is *not* a value.
ACTIVE_STATUS = "ACTIVE"
#: The list endpoint refuses to page past the first 1,000 alphas.
LIST_LIMIT = 1000

DEFAULT_ALPHA_DB = storage.DEFAULT_DB_PATH

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3


@dataclass
class ActiveSyncStats:
    """What one synchronisation did."""

    fetched: int = 0
    #: Factors that were not in ``alphas`` yet and got registered.
    created_alphas: int = 0
    upserted: int = 0
    #: Items without a Fast Expression, which cannot be linked to a factor.
    skipped: int = 0
    #: Rows in ``active_alphas`` after the sync.
    total_rows: int = 0
    #: False when the 1,000-alpha window cut the listing short.
    complete: bool = True


def fetch_active_alphas(
    client: BrainClient, *, page_size: int = 100
) -> tuple[list[dict[str, Any]], bool]:
    """Every ACTIVE alpha the listing endpoint will return, plus a completeness flag."""
    items: list[dict[str, Any]] = []
    offset = 0
    total = 0
    complete = True
    while offset < LIST_LIMIT:
        try:
            payload = client.get_json(
                "/users/self/alphas",
                {
                    "status": ACTIVE_STATUS,
                    "limit": page_size,
                    "offset": offset,
                    "order": "-dateSubmitted",
                },
            )
        except BrainApiError as exc:
            if exc.status_code != 400:
                raise
            # The platform stops paging here: only the newest 1,000 can be listed.
            logger.warning(
                "the platform refused offset %d (%s); only the first %d active alphas "
                "are visible, the rest needs a narrower filter",
                offset,
                exc.body or exc,
                offset,
            )
            complete = False
            break

        page = payload.get("results") or []
        total = int(payload.get("count") or 0)
        items.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return items, complete and offset >= total


def _expression(item: Mapping[str, Any]) -> str | None:
    regular = item.get("regular")
    if isinstance(regular, Mapping):
        code = regular.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def alpha_row(item: Mapping[str, Any], alpha_uid: str, *, synced_at: str) -> dict[str, Any]:
    """Map one platform payload onto the ``active_alphas`` columns."""
    is_block = item.get("is") if isinstance(item.get("is"), Mapping) else {}
    row: dict[str, Any] = {
        "braint_alpha_id": str(item.get("id") or ""),
        "alpha_uid": alpha_uid,
        "expression": _expression(item) or "",
        "status": item.get("status"),
        "grade": item.get("grade"),
        "submitted_at": item.get("dateSubmitted"),
        "created_at": item.get("dateCreated"),
        "settings_json": _dump(item.get("settings")),
        "self_correlation": is_block.get("selfCorrelation"),
        "prod_correlation": is_block.get("prodCorrelation"),
        "checks_json": _dump(is_block.get("checks")),
        "os_json": _dump(item.get("os")),
        "train_json": _dump(item.get("train")),
        "test_json": _dump(item.get("test")),
        "raw_alpha_json": _dump(dict(item)),
        "synced_at": synced_at,
    }
    for column, source, _ in storage.IS_METRICS:
        value = is_block.get(source)
        row[column] = value if isinstance(value, (int, float, str)) or value is None else None
    return row


def sync_active_alphas(
    *,
    alpha_db: Path | str | None = None,
    credentials_path: Path | str | None = None,
    client: BrainClient | None = None,
    credentials: Credentials | None = None,
    page_size: int = 100,
) -> ActiveSyncStats:
    """Read the ACTIVE alphas from the platform and store them in ``alpha.db``."""
    db_path = Path(alpha_db) if alpha_db else DEFAULT_ALPHA_DB
    owned_client = client is None
    if owned_client:
        client = BrainClient(credentials or load_credentials(credentials_path))
    try:
        items, complete = fetch_active_alphas(client, page_size=page_size)
    finally:
        if owned_client:
            client.close()

    stats = ActiveSyncStats(fetched=len(items), complete=complete)
    synced_at = storage.utc_now()
    connection = storage.connect(db_path)
    try:
        for item in items:
            expression = _expression(item)
            braint_alpha_id = str(item.get("id") or "")
            if not expression or not braint_alpha_id:
                stats.skipped += 1
                continue
            alpha_uid, created = storage.ensure_alpha(connection, expression)
            if created:
                stats.created_alphas += 1

            row = alpha_row(item, alpha_uid, synced_at=synced_at)
            columns = list(row)
            placeholders = ", ".join(f":{name}" for name in columns)
            updates = ", ".join(
                f"{name} = excluded.{name}"
                for name in columns
                if name != "braint_alpha_id"
            )
            connection.execute(
                f"INSERT INTO {storage.ACTIVE_ALPHAS_TABLE} ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (braint_alpha_id) DO UPDATE SET {updates}",
                row,
            )
            stats.upserted += 1
        connection.commit()
        stats.total_rows = connection.execute(
            f"SELECT COUNT(*) FROM {storage.ACTIVE_ALPHAS_TABLE}"
        ).fetchone()[0]
    finally:
        connection.close()

    logger.info(
        "active alphas synced: fetched=%d upserted=%d new factors=%d skipped=%d total=%d%s",
        stats.fetched,
        stats.upserted,
        stats.created_alphas,
        stats.skipped,
        stats.total_rows,
        "" if complete else " (listing truncated at 1,000)",
    )
    return stats


def setup_logging(level: str, log_dir: Path) -> Path | None:
    log_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    log_path = log_dir / f"active-alphas-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldquant_consultant_project.acquisition.active_alphas",
        description="Mirror the submitted (ACTIVE) alphas from BRAIN into alpha.db.",
    )
    parser.add_argument("--alpha-db", type=Path, default=DEFAULT_ALPHA_DB)
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what the platform reports without writing anything",
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.log_level, args.log_dir)
    if log_path:
        logger.info("logging to %s", log_path)

    if args.dry_run:
        try:
            client = BrainClient(load_credentials(args.credentials))
            try:
                items, complete = fetch_active_alphas(client, page_size=args.page_size)
            finally:
                client.close()
        except (CredentialsError, BrainApiError) as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG
        print(f"active alphas on the platform: {len(items)}" + ("" if complete else " (truncated at 1,000)"))
        for item in items[:10]:
            print(
                f"  {item.get('id')}  {item.get('dateSubmitted')}  "
                f"grade={item.get('grade')}  {(_expression(item) or '')[:70]}"
            )
        if len(items) > 10:
            print(f"  ... and {len(items) - 10} more")
        return EXIT_OK

    try:
        stats = sync_active_alphas(
            alpha_db=args.alpha_db,
            credentials_path=args.credentials,
            page_size=args.page_size,
        )
    except CredentialsError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except BrainApiError as exc:
        logger.error("BRAIN API failed: %s", exc)
        return EXIT_API

    print(
        f"active alphas: fetched={stats.fetched}, upserted={stats.upserted}, "
        f"new factors={stats.created_alphas}, skipped={stats.skipped}, "
        f"rows in {Path(args.alpha_db).name}={stats.total_rows}"
    )
    if not stats.complete:
        print("  note: the listing stopped at 1,000 alphas; older ones are not mirrored")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Orchestration: probe universes, list data sets and download data fields."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from tqdm import tqdm

from . import db
from .client import BrainApiError, BrainClient
from .config import PAGE_SIZE

logger = logging.getLogger(__name__)

DATASETS_PATH = "/data-sets"
FIELDS_PATH = "/data-fields"
# BRAIN documents the filter as ``dataset.id``; older deployments accept ``dataset``.
PRIMARY_PARAM_STYLE = "dataset.id"
FALLBACK_PARAM_STYLE = "dataset"


@dataclass
class CrawlConfig:
    instrument_type: str
    region: str
    delay: int
    universes: Sequence[str]
    page_size: int = PAGE_SIZE
    max_fields: int | None = None
    force: bool = False
    dataset_filter: list[str] | None = None
    exclude_types: frozenset[str] = frozenset()


@dataclass
class CrawlStats:
    universes_valid: int = 0
    universes_invalid: int = 0
    datasets_seen: int = 0
    fields_written: int = 0
    datasets_done: int = 0
    datasets_failed: list[tuple[str, str]] = field(default_factory=list)


def _scope(config: CrawlConfig, universe: str) -> dict[str, Any]:
    return {
        "instrumentType": config.instrument_type,
        "region": config.region,
        "delay": config.delay,
        "universe": universe,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _nested(item: Mapping[str, Any], key: str, sub: str) -> Any:
    value = item.get(key)
    if isinstance(value, Mapping):
        return value.get(sub)
    return None


def dataset_row(
    item: Mapping[str, Any], config: CrawlConfig, universe: str
) -> dict[str, Any]:
    dataset_id = item.get("id") or item.get("dataset")
    return {
        "instrument_type": config.instrument_type,
        "region": config.region,
        "delay": config.delay,
        "universe": universe,
        "dataset_id": dataset_id,
        "name": item.get("name"),
        "description": item.get("description"),
        "category_id": _nested(item, "category", "id"),
        "category_name": _nested(item, "category", "name"),
        "subcategory_id": _nested(item, "subcategory", "id"),
        "subcategory_name": _nested(item, "subcategory", "name"),
        "field_count": _int_or_none(item.get("fieldCount")),
        "alpha_count": _int_or_none(item.get("alphaCount")),
        "user_count": _int_or_none(item.get("userCount")),
        "coverage": _float_or_none(item.get("coverage")),
        "value_score": _float_or_none(item.get("valueScore")),
        "date_coverage": _float_or_none(item.get("dateCoverage")),
        "date_created": item.get("dateCreated"),
        "themes": _json_or_none(item.get("themes")),
        "raw_json": json.dumps(item, ensure_ascii=False),
        "fetched_at": db.utc_now(),
    }


def field_row(
    item: Mapping[str, Any], config: CrawlConfig, universe: str, dataset_id: str
) -> dict[str, Any] | None:
    field_id = item.get("id")
    if not field_id:
        return None
    embedded_dataset = item.get("dataset")
    if isinstance(embedded_dataset, Mapping):
        embedded_id = embedded_dataset.get("id")
    elif isinstance(embedded_dataset, str):
        embedded_id = embedded_dataset
    else:
        embedded_id = None
    return {
        "instrument_type": config.instrument_type,
        "region": config.region,
        "delay": config.delay,
        "universe": universe,
        "dataset_id": embedded_id or dataset_id,
        "field_id": field_id,
        "description": item.get("description"),
        "type": item.get("type"),
        "category_id": _nested(item, "category", "id"),
        "category_name": _nested(item, "category", "name"),
        "subcategory_id": _nested(item, "subcategory", "id"),
        "subcategory_name": _nested(item, "subcategory", "name"),
        "coverage": _float_or_none(item.get("coverage")),
        "date_coverage": _float_or_none(item.get("dateCoverage")),
        "date_created": item.get("dateCreated"),
        "user_count": _int_or_none(item.get("userCount")),
        "alpha_count": _int_or_none(item.get("alphaCount")),
        "themes": _json_or_none(item.get("themes")),
        "raw_json": json.dumps(item, ensure_ascii=False),
        "fetched_at": db.utc_now(),
    }


def probe_universes(
    client: BrainClient, connection: sqlite3.Connection, config: CrawlConfig, stats: CrawlStats
) -> list[str]:
    """Return the universes the API accepts for this instrument/region/delay."""
    valid: list[str] = []
    for universe in config.universes:
        params = _scope(config, universe)
        params.update(limit=1, offset=0)
        try:
            payload = client.get_json(DATASETS_PATH, params)
        except BrainApiError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                logger.warning("universe %s rejected by API (%s)", universe, exc.status_code)
                db.record_universe(
                    connection,
                    instrument_type=config.instrument_type,
                    region=config.region,
                    delay=config.delay,
                    universe=universe,
                    is_valid=False,
                    error=str(exc)[:500],
                )
                stats.universes_invalid += 1
                continue
            raise
        total = int(payload.get("count") or 0)
        db.record_universe(
            connection,
            instrument_type=config.instrument_type,
            region=config.region,
            delay=config.delay,
            universe=universe,
            is_valid=True,
        )
        stats.universes_valid += 1
        valid.append(universe)
        logger.info("universe %s valid (%d data sets)", universe, total)
    connection.commit()
    return valid


def sync_datasets(
    client: BrainClient,
    connection: sqlite3.Connection,
    config: CrawlConfig,
    universe: str,
    stats: CrawlStats,
) -> int:
    """Store metadata for every data set of ``universe`` and seed crawl state."""
    stored = 0
    for total, items in client.iter_pages(DATASETS_PATH, _scope(config, universe)):
        for item in items:
            row = dataset_row(item, config, universe)
            if not row["dataset_id"]:
                continue
            db.upsert_dataset(connection, row)
            db.ensure_state_pending(
                connection,
                instrument_type=config.instrument_type,
                region=config.region,
                delay=config.delay,
                universe=universe,
                dataset_id=str(row["dataset_id"]),
            )
            stored += 1
        connection.commit()
        logger.info("universe %s: stored %d/%d data sets", universe, stored, total)
    stats.datasets_seen += stored
    return stored


def crawl_dataset_fields(
    client: BrainClient,
    connection: sqlite3.Connection,
    config: CrawlConfig,
    universe: str,
    dataset_id: str,
    field_count_hint: int | None,
) -> tuple[int, str]:
    """Download every field of one data set. Returns (fields stored, param style)."""
    params = _scope(config, universe)
    param_style = PRIMARY_PARAM_STYLE
    offset = 0
    written = 0
    pages = 0
    total = 0

    while True:
        query = dict(params)
        query[param_style] = dataset_id
        query.update(limit=config.page_size, offset=offset)
        payload = client.get_json(FIELDS_PATH, query)
        items = payload.get("results") or []
        total = int(payload.get("count") or 0)

        if not items and offset == 0 and param_style == PRIMARY_PARAM_STYLE and (field_count_hint or 0) > 0:
            logger.info(
                "no fields returned for %s with '%s'; retrying with '%s'",
                dataset_id,
                PRIMARY_PARAM_STYLE,
                FALLBACK_PARAM_STYLE,
            )
            param_style = FALLBACK_PARAM_STYLE
            continue

        rows = []
        for item in items:
            row = field_row(item, config, universe, dataset_id)
            if row is None:
                continue
            if row["type"] and row["type"].upper() in config.exclude_types:
                continue
            rows.append(row)

        if config.max_fields is not None:
            rows = rows[: max(0, config.max_fields - written)]

        db.upsert_fields(connection, rows)
        written += len(rows)
        pages += 1
        db.upsert_state(
            connection,
            instrument_type=config.instrument_type,
            region=config.region,
            delay=config.delay,
            universe=universe,
            dataset_id=dataset_id,
            status="running",
            field_count=written,
            pages=pages,
            param_style=param_style,
        )
        connection.commit()
        logger.debug(
            "universe %s dataset %s: page %d, %d/%d fields, %d stored",
            universe,
            dataset_id,
            pages,
            offset + len(items),
            total,
            written,
        )

        offset += len(items)
        if config.max_fields is not None and written >= config.max_fields:
            logger.info("dataset %s: max-fields limit %d reached", dataset_id, config.max_fields)
            break
        if not items or offset >= total:
            break

    return written, param_style


def crawl_universe(
    client: BrainClient,
    connection: sqlite3.Connection,
    config: CrawlConfig,
    universe: str,
    stats: CrawlStats,
) -> None:
    sync_datasets(client, connection, config, universe, stats)
    pending = db.datasets_to_crawl(
        connection,
        instrument_type=config.instrument_type,
        region=config.region,
        delay=config.delay,
        universe=universe,
        force=config.force,
        dataset_filter=config.dataset_filter,
    )
    logger.info("universe %s: %d data sets to crawl", universe, len(pending))
    progress = tqdm(
        pending,
        desc=f"{universe}",
        unit="dataset",
        dynamic_ncols=True,
        disable=not sys.stderr.isatty(),
    )
    for row in progress:
        dataset_id = str(row["dataset_id"])
        progress.set_postfix_str(dataset_id, refresh=False)
        db.upsert_state(
            connection,
            instrument_type=config.instrument_type,
            region=config.region,
            delay=config.delay,
            universe=universe,
            dataset_id=dataset_id,
            status="running",
        )
        connection.commit()
        try:
            written, param_style = crawl_dataset_fields(
                client, connection, config, universe, dataset_id, row["field_count"]
            )
        except BrainApiError as exc:
            logger.error("dataset %s failed: %s", dataset_id, exc)
            db.upsert_state(
                connection,
                instrument_type=config.instrument_type,
                region=config.region,
                delay=config.delay,
                universe=universe,
                dataset_id=dataset_id,
                status="failed",
                last_error=str(exc)[:1000],
            )
            connection.commit()
            stats.datasets_failed.append((universe, dataset_id))
            continue

        db.upsert_state(
            connection,
            instrument_type=config.instrument_type,
            region=config.region,
            delay=config.delay,
            universe=universe,
            dataset_id=dataset_id,
            status="done",
            field_count=written,
            param_style=param_style,
        )
        connection.commit()
        stats.fields_written += written
        stats.datasets_done += 1
        logger.info("dataset %s/%s: %d fields stored", universe, dataset_id, written)


def run_crawl(
    client: BrainClient, connection: sqlite3.Connection, config: CrawlConfig
) -> CrawlStats:
    stats = CrawlStats()
    run_id = db.start_run(
        connection,
        instrument_type=config.instrument_type,
        region=config.region,
        delay=config.delay,
        universes=config.universes,
        note=f"max_fields={config.max_fields} force={config.force}",
    )
    status = "completed"
    try:
        valid_universes = probe_universes(client, connection, config, stats)
        for universe in valid_universes:
            if config.max_fields is not None:
                # Smoke runs only touch the first universe that accepts the request.
                crawl_universe(client, connection, config, universe, stats)
                break
            crawl_universe(client, connection, config, universe, stats)
    except Exception as exc:  # noqa: BLE001 - recorded in crawl_runs before re-raising
        status = "failed"
        db.finish_run(connection, run_id, status=status, note=str(exc)[:1000])
        raise
    if stats.datasets_failed:
        status = "partial"
    db.finish_run(connection, run_id, status=status)
    return stats


def dry_run(client: BrainClient, config: CrawlConfig) -> dict[str, Any]:
    """Fetch a single page of data without writing anything to the database."""
    report: dict[str, Any] = {}
    for universe in config.universes:
        params = _scope(config, universe)
        params.update(limit=1, offset=0)
        payload = client.get_json(DATASETS_PATH, params)
        report[universe] = {"datasets": int(payload.get("count") or 0)}
        break
    universe = config.universes[0]
    params = _scope(config, universe)
    params.update(limit=1, offset=0)
    datasets = client.get_json(DATASETS_PATH, dict(params, limit=1))
    items: Iterable[Mapping[str, Any]] = datasets.get("results") or []
    for item in items:
        dataset_id = item.get("id")
        query = dict(_scope(config, universe))
        query[PRIMARY_PARAM_STYLE] = dataset_id
        query.update(limit=1, offset=0)
        fields = client.get_json(FIELDS_PATH, query)
        report["sample_dataset"] = {
            "id": dataset_id,
            "name": item.get("name"),
            "fields": int(fields.get("count") or 0),
            "sample_field": (fields.get("results") or [{}])[0].get("id"),
        }
        break
    return report

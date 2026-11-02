"""SQLite persistence for the BRAIN data-set / data-field catalogue."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    instrument_type TEXT    NOT NULL,
    region          TEXT    NOT NULL,
    delay           INTEGER NOT NULL,
    universes       TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    note            TEXT
);

CREATE TABLE IF NOT EXISTS universes (
    instrument_type TEXT    NOT NULL,
    region          TEXT    NOT NULL,
    delay           INTEGER NOT NULL,
    universe        TEXT    NOT NULL,
    is_valid        INTEGER NOT NULL,
    error           TEXT,
    probed_at       TEXT    NOT NULL,
    PRIMARY KEY (instrument_type, region, delay, universe)
);

CREATE TABLE IF NOT EXISTS datasets (
    instrument_type  TEXT    NOT NULL,
    region           TEXT    NOT NULL,
    delay            INTEGER NOT NULL,
    universe         TEXT    NOT NULL,
    dataset_id       TEXT    NOT NULL,
    name             TEXT,
    description      TEXT,
    category_id      TEXT,
    category_name    TEXT,
    subcategory_id   TEXT,
    subcategory_name TEXT,
    field_count      INTEGER,
    alpha_count      INTEGER,
    user_count       INTEGER,
    coverage         REAL,
    value_score      REAL,
    date_coverage    REAL,
    date_created     TEXT,
    themes           TEXT,
    raw_json         TEXT    NOT NULL,
    fetched_at       TEXT    NOT NULL,
    PRIMARY KEY (instrument_type, region, delay, universe, dataset_id)
);

CREATE TABLE IF NOT EXISTS data_fields (
    instrument_type  TEXT    NOT NULL,
    region           TEXT    NOT NULL,
    delay            INTEGER NOT NULL,
    universe         TEXT    NOT NULL,
    dataset_id       TEXT    NOT NULL,
    field_id         TEXT    NOT NULL,
    description      TEXT,
    type             TEXT,
    category_id      TEXT,
    category_name    TEXT,
    subcategory_id   TEXT,
    subcategory_name TEXT,
    coverage         REAL,
    date_coverage    REAL,
    date_created     TEXT,
    user_count       INTEGER,
    alpha_count      INTEGER,
    themes           TEXT,
    raw_json         TEXT    NOT NULL,
    fetched_at       TEXT    NOT NULL,
    PRIMARY KEY (instrument_type, region, delay, universe, dataset_id, field_id),
    FOREIGN KEY (instrument_type, region, delay, universe, dataset_id)
        REFERENCES datasets (instrument_type, region, delay, universe, dataset_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_state (
    instrument_type TEXT    NOT NULL,
    region          TEXT    NOT NULL,
    delay           INTEGER NOT NULL,
    universe        TEXT    NOT NULL,
    dataset_id      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    field_count     INTEGER NOT NULL DEFAULT 0,
    pages           INTEGER NOT NULL DEFAULT 0,
    param_style     TEXT,
    last_error      TEXT,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (instrument_type, region, delay, universe, dataset_id),
    FOREIGN KEY (instrument_type, region, delay, universe, dataset_id)
        REFERENCES datasets (instrument_type, region, delay, universe, dataset_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fields_dataset
    ON data_fields (instrument_type, region, delay, universe, dataset_id);
CREATE INDEX IF NOT EXISTS idx_fields_field_id
    ON data_fields (field_id);
CREATE INDEX IF NOT EXISTS idx_fields_type
    ON data_fields (type);
CREATE INDEX IF NOT EXISTS idx_datasets_universe
    ON datasets (instrument_type, region, delay, universe);
CREATE INDEX IF NOT EXISTS idx_state_status
    ON crawl_state (instrument_type, region, delay, universe, status);

DROP VIEW IF EXISTS v_field_counts;
CREATE VIEW v_field_counts AS
SELECT instrument_type,
       region,
       delay,
       universe,
       dataset_id,
       COUNT(*)                                             AS field_count,
       SUM(CASE WHEN type = 'MATRIX' THEN 1 ELSE 0 END)      AS matrix_count,
       SUM(CASE WHEN type = 'VECTOR' THEN 1 ELSE 0 END)      AS vector_count,
       SUM(CASE WHEN type = 'GROUP'  THEN 1 ELSE 0 END)      AS group_count,
       MAX(fetched_at)                                       AS last_fetched_at
FROM data_fields
GROUP BY instrument_type, region, delay, universe, dataset_id;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and initialise) the catalogue database."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def start_run(
    connection: sqlite3.Connection,
    *,
    instrument_type: str,
    region: str,
    delay: int,
    universes: Iterable[str],
    note: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO crawl_runs (started_at, instrument_type, region, delay, universes, status, note)
        VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (utc_now(), instrument_type, region, delay, json.dumps(list(universes)), note),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    note: str | None = None,
) -> None:
    connection.execute(
        "UPDATE crawl_runs SET finished_at = ?, status = ?, note = COALESCE(?, note) WHERE run_id = ?",
        (utc_now(), status, note, run_id),
    )
    connection.commit()


def record_universe(
    connection: sqlite3.Connection,
    *,
    instrument_type: str,
    region: str,
    delay: int,
    universe: str,
    is_valid: bool,
    error: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO universes (instrument_type, region, delay, universe, is_valid, error, probed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (instrument_type, region, delay, universe) DO UPDATE SET
            is_valid  = excluded.is_valid,
            error     = excluded.error,
            probed_at = excluded.probed_at
        """,
        (instrument_type, region, delay, universe, int(is_valid), error, utc_now()),
    )
    connection.commit()


def upsert_dataset(connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO datasets (
            instrument_type, region, delay, universe, dataset_id, name, description,
            category_id, category_name, subcategory_id, subcategory_name,
            field_count, alpha_count, user_count, coverage, value_score,
            date_coverage, date_created, themes, raw_json, fetched_at
        ) VALUES (
            :instrument_type, :region, :delay, :universe, :dataset_id, :name, :description,
            :category_id, :category_name, :subcategory_id, :subcategory_name,
            :field_count, :alpha_count, :user_count, :coverage, :value_score,
            :date_coverage, :date_created, :themes, :raw_json, :fetched_at
        )
        ON CONFLICT (instrument_type, region, delay, universe, dataset_id) DO UPDATE SET
            name             = excluded.name,
            description      = excluded.description,
            category_id      = excluded.category_id,
            category_name    = excluded.category_name,
            subcategory_id   = excluded.subcategory_id,
            subcategory_name = excluded.subcategory_name,
            field_count      = excluded.field_count,
            alpha_count      = excluded.alpha_count,
            user_count       = excluded.user_count,
            coverage         = excluded.coverage,
            value_score      = excluded.value_score,
            date_coverage    = excluded.date_coverage,
            date_created     = excluded.date_created,
            themes           = excluded.themes,
            raw_json         = excluded.raw_json,
            fetched_at       = excluded.fetched_at
        """,
        dict(row),
    )


def upsert_fields(connection: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    connection.executemany(
        """
        INSERT INTO data_fields (
            instrument_type, region, delay, universe, dataset_id, field_id, description, type,
            category_id, category_name, subcategory_id, subcategory_name,
            coverage, date_coverage, date_created, user_count, alpha_count,
            themes, raw_json, fetched_at
        ) VALUES (
            :instrument_type, :region, :delay, :universe, :dataset_id, :field_id, :description, :type,
            :category_id, :category_name, :subcategory_id, :subcategory_name,
            :coverage, :date_coverage, :date_created, :user_count, :alpha_count,
            :themes, :raw_json, :fetched_at
        )
        ON CONFLICT (instrument_type, region, delay, universe, dataset_id, field_id) DO UPDATE SET
            description      = excluded.description,
            type             = excluded.type,
            category_id      = excluded.category_id,
            category_name    = excluded.category_name,
            subcategory_id   = excluded.subcategory_id,
            subcategory_name = excluded.subcategory_name,
            coverage         = excluded.coverage,
            date_coverage    = excluded.date_coverage,
            date_created     = excluded.date_created,
            user_count       = excluded.user_count,
            alpha_count      = excluded.alpha_count,
            themes           = excluded.themes,
            raw_json         = excluded.raw_json,
            fetched_at       = excluded.fetched_at
        """,
        [dict(row) for row in rows],
    )
    return len(rows)


def upsert_state(
    connection: sqlite3.Connection,
    *,
    instrument_type: str,
    region: str,
    delay: int,
    universe: str,
    dataset_id: str,
    status: str,
    field_count: int = 0,
    pages: int = 0,
    param_style: str | None = None,
    last_error: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_state (
            instrument_type, region, delay, universe, dataset_id,
            status, field_count, pages, param_style, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (instrument_type, region, delay, universe, dataset_id) DO UPDATE SET
            status      = excluded.status,
            field_count = excluded.field_count,
            pages       = excluded.pages,
            param_style = COALESCE(excluded.param_style, crawl_state.param_style),
            last_error  = excluded.last_error,
            updated_at  = excluded.updated_at
        """,
        (
            instrument_type,
            region,
            delay,
            universe,
            dataset_id,
            status,
            field_count,
            pages,
            param_style,
            last_error,
            utc_now(),
        ),
    )


def ensure_state_pending(
    connection: sqlite3.Connection,
    *,
    instrument_type: str,
    region: str,
    delay: int,
    universe: str,
    dataset_id: str,
) -> None:
    """Create a pending row unless one already exists (keeps resume progress)."""
    connection.execute(
        """
        INSERT OR IGNORE INTO crawl_state (
            instrument_type, region, delay, universe, dataset_id, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (instrument_type, region, delay, universe, dataset_id, utc_now()),
    )


def datasets_to_crawl(
    connection: sqlite3.Connection,
    *,
    instrument_type: str,
    region: str,
    delay: int,
    universe: str,
    force: bool = False,
    dataset_filter: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT d.dataset_id, d.name, d.field_count, s.status
        FROM datasets d
        JOIN crawl_state s
          ON  s.instrument_type = d.instrument_type
          AND s.region          = d.region
          AND s.delay           = d.delay
          AND s.universe        = d.universe
          AND s.dataset_id      = d.dataset_id
        WHERE d.instrument_type = ? AND d.region = ? AND d.delay = ? AND d.universe = ?
    """
    params: list[Any] = [instrument_type, region, delay, universe]
    wanted = [d for d in (dataset_filter or []) if d]
    if wanted:
        sql += f" AND d.dataset_id IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    if not force:
        sql += " AND s.status != 'done'"
    sql += " ORDER BY s.status = 'done', d.dataset_id"
    return list(connection.execute(sql, params))


def summary(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT universes.universe,
                   COUNT(DISTINCT datasets.dataset_id) AS datasets,
                   COUNT(data_fields.field_id)         AS fields
            FROM universes
            LEFT JOIN datasets
              ON  datasets.instrument_type = universes.instrument_type
              AND datasets.region          = universes.region
              AND datasets.delay           = universes.delay
              AND datasets.universe        = universes.universe
            LEFT JOIN data_fields
              ON  data_fields.instrument_type = datasets.instrument_type
              AND data_fields.region          = datasets.region
              AND data_fields.delay           = datasets.delay
              AND data_fields.universe        = datasets.universe
              AND data_fields.dataset_id      = datasets.dataset_id
            WHERE universes.is_valid = 1
            GROUP BY universes.universe
            ORDER BY fields DESC
            """
        )
    )

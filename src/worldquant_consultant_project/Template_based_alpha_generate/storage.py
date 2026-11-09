"""Persistence for generated alphas: ``data/alpha.db``.

``alphas``        the lean table: uid, expression and the columns that summarise the factor's
                  backtests (owned by ``simulation.storage``, reused here so both create the
                  same layout).
``alpha_details`` 1:1 side table with template, dedupe hash and batch metadata.
``alpha_slots``   one row per filled slot (field provenance).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..acquisition.config import DEFAULT_DATA_DIR
from ..simulation.storage import (
    ALPHA_EXPRESSIONS_VIEW_SQL,
    alphas_table_sql,
    ensure_alphas_layout,
)
from .generator import GeneratedAlpha

DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "alpha.db"


_DETAILS_TABLE = """
CREATE TABLE IF NOT EXISTS alpha_details (
    alpha_uid        TEXT PRIMARY KEY,
    template_id      TEXT NOT NULL,
    template_name    TEXT,
    expression_hash  TEXT NOT NULL UNIQUE,
    slot_count       INTEGER NOT NULL,
    formula_pattern  TEXT NOT NULL,
    label_source     TEXT NOT NULL,
    slot_fill_json   TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    generated_at     TEXT NOT NULL,
    FOREIGN KEY (alpha_uid) REFERENCES alphas (alpha_uid) ON DELETE CASCADE
);
"""

_SLOTS_TABLE = """
CREATE TABLE IF NOT EXISTS alpha_slots (
    alpha_uid         TEXT NOT NULL,
    slot_name         TEXT NOT NULL,
    slot_index        INTEGER NOT NULL,
    dataset_id        TEXT NOT NULL,
    field_id          TEXT NOT NULL,
    description       TEXT,
    field_type        TEXT,
    matched_providers TEXT NOT NULL,
    labels_json       TEXT NOT NULL,
    PRIMARY KEY (alpha_uid, slot_name),
    FOREIGN KEY (alpha_uid) REFERENCES alphas (alpha_uid) ON DELETE CASCADE
);
"""

_INDEXES_AND_VIEW = """
CREATE INDEX IF NOT EXISTS idx_alphas_expression  ON alphas (expression);
CREATE INDEX IF NOT EXISTS idx_details_template   ON alpha_details (template_id);
CREATE INDEX IF NOT EXISTS idx_details_run        ON alpha_details (run_id);
CREATE INDEX IF NOT EXISTS idx_details_created    ON alpha_details (generated_at);
CREATE INDEX IF NOT EXISTS idx_alpha_slots_field  ON alpha_slots (field_id);
""" + ALPHA_EXPRESSIONS_VIEW_SQL

#: ``alphas`` itself comes from ``simulation.storage`` so both modules agree on its layout.
SCHEMA = _DETAILS_TABLE + _SLOTS_TABLE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _needs_migration(connection: sqlite3.Connection) -> bool:
    """The first layout kept every template column inside ``alphas``."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(alphas)")}
    return bool(columns) and "template_id" in columns


def _migrate_wide_alphas(connection: sqlite3.Connection) -> bool:
    """Split the old wide ``alphas`` table into ``alphas`` + ``alpha_details``."""
    if not _needs_migration(connection):
        return False

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            "DROP VIEW IF EXISTS v_alpha_expressions;"
            "DROP TABLE IF EXISTS alphas_new;"
            "DROP TABLE IF EXISTS alpha_details;"
        )
        connection.executescript(alphas_table_sql("alphas_new"))
        connection.executescript(_DETAILS_TABLE)
        # Both markers stay NULL until the alpha is actually backtested.
        connection.execute(
            "INSERT INTO alphas_new (alpha_uid, expression) SELECT alpha_uid, expression FROM alphas"
        )
        connection.execute(
            """
            INSERT INTO alpha_details (
                alpha_uid, template_id, template_name, expression_hash, slot_count,
                formula_pattern, label_source, slot_fill_json, run_id, generated_at
            )
            SELECT alpha_uid, template_id, template_name, expression_hash, slot_count,
                   formula_pattern, label_source, slot_fill_json, run_id, generated_at
            FROM alphas
            """
        )
        connection.execute("DROP TABLE alphas")
        connection.execute("ALTER TABLE alphas_new RENAME TO alphas")
        connection.executescript(_INDEXES_AND_VIEW)
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    return True


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    if not _migrate_wide_alphas(connection):
        connection.executescript(SCHEMA)
        ensure_alphas_layout(connection)
        connection.executescript(_INDEXES_AND_VIEW)
        connection.commit()
    return connection


def new_run_id() -> str:
    return uuid.uuid4().hex


def exists(connection: sqlite3.Connection, hashes: Sequence[str]) -> set[str]:
    """Which of ``hashes`` are already stored."""
    if not hashes:
        return set()
    found: set[str] = set()
    chunk = 500
    for start in range(0, len(hashes), chunk):
        batch = hashes[start : start + chunk]
        rows = connection.execute(
            f"SELECT expression_hash FROM alpha_details "
            f"WHERE expression_hash IN ({','.join('?' * len(batch))})",
            batch,
        )
        found.update(row[0] for row in rows)
    return found


def save(
    connection: sqlite3.Connection,
    alphas: Iterable[GeneratedAlpha],
    *,
    label_source: str,
    run_id: str,
) -> int:
    """Insert alphas, their details and slot rows. Returns the number of alphas inserted."""
    generated_at = utc_now()
    core_rows: list[tuple] = []
    detail_rows: list[tuple] = []
    slot_rows: list[tuple] = []

    for alpha in alphas:
        alpha_uid = uuid.uuid4().hex
        core_rows.append((alpha_uid, alpha.expression, 0, 0, 0))
        detail_rows.append(
            (
                alpha_uid,
                alpha.template.template_id,
                alpha.template.template_name,
                alpha.expression_hash,
                alpha.template.slot_count,
                alpha.template.formula_pattern,
                label_source,
                json.dumps(alpha.slot_fill, ensure_ascii=False, sort_keys=True),
                run_id,
                generated_at,
            )
        )
        for index, (slot_name, candidate) in enumerate(alpha.slots):
            slot_rows.append(
                (
                    alpha_uid,
                    slot_name,
                    index,
                    candidate.dataset_id,
                    candidate.field_id,
                    candidate.description,
                    candidate.field_type,
                    json.dumps(list(candidate.matched_providers), ensure_ascii=False),
                    candidate.labels_json,
                )
            )

    if not core_rows:
        return 0

    try:
        connection.executemany(
            "INSERT INTO alphas (alpha_uid, expression, number_of_simulations, "
            "tentative_submittable, is_formatting_error) VALUES (?, ?, ?, ?, ?)",
            core_rows,
        )
        connection.executemany(
            """
            INSERT INTO alpha_details (
                alpha_uid, template_id, template_name, expression_hash, slot_count,
                formula_pattern, label_source, slot_fill_json, run_id, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            detail_rows,
        )
        connection.executemany(
            """
            INSERT INTO alpha_slots (
                alpha_uid, slot_name, slot_index, dataset_id, field_id,
                description, field_type, matched_providers, labels_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            slot_rows,
        )
    except sqlite3.IntegrityError:
        connection.rollback()
        raise
    connection.commit()
    return len(core_rows)


def clear(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM alpha_slots")
    connection.execute("DELETE FROM alpha_details")
    connection.execute("DELETE FROM alphas")
    connection.commit()


def summary(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT d.template_id,
                   COUNT(*) AS alphas,
                   SUM(CASE WHEN a.number_of_simulations > 0 THEN 1 ELSE 0 END) AS simulated,
                   COUNT(DISTINCT d.run_id) AS runs,
                   MIN(d.generated_at) AS first_generated
            FROM alphas AS a
            JOIN alpha_details AS d ON d.alpha_uid = a.alpha_uid
            GROUP BY d.template_id
            ORDER BY d.template_id
            """
        )
    )

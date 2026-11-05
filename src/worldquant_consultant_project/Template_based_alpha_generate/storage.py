"""Persistence for generated alphas: ``data/alpha.db`` with alphas + alpha_slots."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..acquisition.config import DEFAULT_DATA_DIR
from .generator import GeneratedAlpha

DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "alpha.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS alphas (
    alpha_uid        TEXT PRIMARY KEY,
    template_id      TEXT NOT NULL,
    template_name    TEXT,
    expression       TEXT NOT NULL,
    expression_hash  TEXT NOT NULL UNIQUE,
    slot_count       INTEGER NOT NULL,
    formula_pattern  TEXT NOT NULL,
    label_source     TEXT NOT NULL,
    slot_fill_json   TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    generated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alphas_template ON alphas (template_id);
CREATE INDEX IF NOT EXISTS idx_alphas_run      ON alphas (run_id);
CREATE INDEX IF NOT EXISTS idx_alphas_created  ON alphas (generated_at);

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

CREATE INDEX IF NOT EXISTS idx_alpha_slots_field ON alpha_slots (field_id);

DROP VIEW IF EXISTS v_alpha_expressions;
CREATE VIEW v_alpha_expressions AS
SELECT alpha_uid,
       template_id,
       template_name,
       expression,
       slot_count,
       label_source,
       run_id,
       generated_at
FROM alphas
ORDER BY template_id, alpha_uid;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
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
            f"SELECT expression_hash FROM alphas WHERE expression_hash IN ({','.join('?' * len(batch))})",
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
    """Insert alphas plus their slot rows. Returns the number of alphas inserted."""
    generated_at = utc_now()
    alpha_rows: list[tuple] = []
    slot_rows: list[tuple] = []

    for alpha in alphas:
        alpha_uid = uuid.uuid4().hex
        alpha_rows.append(
            (
                alpha_uid,
                alpha.template.template_id,
                alpha.template.template_name,
                alpha.expression,
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

    if not alpha_rows:
        return 0

    try:
        connection.executemany(
            """
            INSERT INTO alphas (
                alpha_uid, template_id, template_name, expression, expression_hash,
                slot_count, formula_pattern, label_source, slot_fill_json, run_id, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            alpha_rows,
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
    return len(alpha_rows)


def clear(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM alpha_slots")
    connection.execute("DELETE FROM alphas")
    connection.commit()


def summary(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT template_id,
                   COUNT(*) AS alphas,
                   COUNT(DISTINCT run_id) AS runs,
                   MIN(generated_at) AS first_generated
            FROM alphas
            GROUP BY template_id
            ORDER BY template_id
            """
        )
    )

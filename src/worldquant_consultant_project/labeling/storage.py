"""Schema migration and persistence helpers for field labels."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import LABEL_COLUMNS, LABEL_DIMENSIONS, FieldInput

AUDIT_TABLE = "deepseek_labels"

AUDIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    model                TEXT    NOT NULL,
    dataset_id           TEXT    NOT NULL,
    field_id             TEXT    NOT NULL,
    category             TEXT,
    math_dimension       TEXT,
    polarity             TEXT,
    domain_support       TEXT,
    frequency            TEXT,
    raw_json             TEXT,
    prompt_tokens        INTEGER,
    completion_tokens    INTEGER,
    attempts             INTEGER NOT NULL DEFAULT 0,
    status               TEXT    NOT NULL,
    error                TEXT,
    system_prompt_sha256 TEXT,
    labeled_at           TEXT    NOT NULL,
    PRIMARY KEY (model, dataset_id, field_id)
);

CREATE INDEX IF NOT EXISTS idx_deepseek_labels_status
    ON {AUDIT_TABLE} (model, status);
"""

LABEL_VIEW = "v_deepseek_field_labels"
LABEL_VIEW_SQL = f"""
DROP VIEW IF EXISTS {LABEL_VIEW};
CREATE VIEW {LABEL_VIEW} AS
SELECT DISTINCT
       dataset_id,
       field_id,
       description,
       type,
       category_name,
       subcategory_name,
       date_coverage,
       coverage,
       deepseek_category,
       deepseek_math_dimension,
       deepseek_polarity,
       deepseek_domain_support,
       deepseek_frequency
FROM data_fields
WHERE deepseek_category IS NOT NULL;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> list[str]:
    """Append the ``deepseek_*`` columns and create the audit table. Idempotent."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(data_fields)")}
    added: list[str] = []
    for column in LABEL_COLUMNS.values():
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE data_fields ADD COLUMN {column} TEXT")
        added.append(column)
    connection.executescript(AUDIT_SCHEMA)
    connection.executescript(LABEL_VIEW_SQL)
    connection.commit()
    return added


def _field_input(row: Mapping[str, Any]) -> FieldInput:
    return FieldInput(
        dataset_id=str(row["dataset_id"]),
        field_id=str(row["field_id"]),
        description=row["description"],
        dataset_name=row["dataset_name"],
        category_name=row["category_name"],
        subcategory_name=row["subcategory_name"],
        field_type=row["type"],
        date_coverage=row["date_coverage"],
        coverage=row["coverage"],
    )


def load_fields(
    connection: sqlite3.Connection,
    *,
    model: str,
    prompt_sha: str,
    dataset_filter: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[FieldInput]:
    """Distinct fields still needing labels for ``model`` with the current prompt."""
    sql = """
        SELECT df.dataset_id,
               df.field_id,
               df.description,
               df.category_name,
               df.subcategory_name,
               df.type,
               df.date_coverage,
               df.coverage,
               d.name AS dataset_name
        FROM (SELECT DISTINCT dataset_id, field_id, description, category_name,
                     subcategory_name, type, date_coverage, coverage
              FROM data_fields) AS df
        LEFT JOIN (SELECT dataset_id, MAX(name) AS name
                   FROM datasets GROUP BY dataset_id) AS d
          ON df.dataset_id = d.dataset_id
        LEFT JOIN deepseek_labels AS l
          ON l.model = ? AND l.dataset_id = df.dataset_id AND l.field_id = df.field_id
        WHERE 1 = 1
    """
    params: list[Any] = [model]
    if not force:
        sql += (
            " AND (l.status IS NULL"
            " OR l.status != 'ok'"
            " OR l.system_prompt_sha256 IS NOT ?)"
        )
        params.append(prompt_sha)
    wanted = [d for d in (dataset_filter or []) if d]
    if wanted:
        sql += f" AND df.dataset_id IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    sql += " ORDER BY df.dataset_id, df.field_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_field_input(row) for row in connection.execute(sql, params)]


def count_pending(
    connection: sqlite3.Connection, *, model: str, prompt_sha: str, force: bool = False
) -> int:
    return len(load_fields(connection, model=model, prompt_sha=prompt_sha, force=force))


def write_labels(
    connection: sqlite3.Connection,
    *,
    model: str,
    prompt_sha: str,
    results: Iterable[Mapping[str, Any]],
) -> int:
    """Write one batch of results to the audit table and to ``data_fields``."""
    batch = list(results)
    if not batch:
        return 0

    columns = list(LABEL_DIMENSIONS)
    audit_sql = f"""
        INSERT INTO {AUDIT_TABLE} (
            model, dataset_id, field_id,
            {', '.join(columns)},
            raw_json, prompt_tokens, completion_tokens,
            attempts, status, error, system_prompt_sha256, labeled_at
        ) VALUES (
            :model, :dataset_id, :field_id,
            {', '.join(':' + c for c in columns)},
            :raw_json, :prompt_tokens, :completion_tokens,
            :attempts, :status, :error, :system_prompt_sha256, :labeled_at
        )
        ON CONFLICT (model, dataset_id, field_id) DO UPDATE SET
            {', '.join(f'{c} = excluded.{c}' for c in columns)},
            raw_json             = excluded.raw_json,
            prompt_tokens        = excluded.prompt_tokens,
            completion_tokens    = excluded.completion_tokens,
            attempts             = excluded.attempts,
            status               = excluded.status,
            error                = excluded.error,
            system_prompt_sha256 = excluded.system_prompt_sha256,
            labeled_at           = excluded.labeled_at
    """
    rows = []
    for item in batch:
        row = dict(item)
        row.setdefault("model", model)
        row.setdefault("system_prompt_sha256", prompt_sha)
        row.setdefault("labeled_at", utc_now())
        rows.append(row)

    connection.executemany(audit_sql, rows)

    field_assignments = ", ".join(f"{LABEL_COLUMNS[c]} = ?" for c in columns)
    field_params = [
        (
            *[item.get(c) for c in columns],
            item["dataset_id"],
            item["field_id"],
        )
        for item in rows
        if item.get("status") == "ok"
    ]
    if field_params:
        connection.executemany(
            f"UPDATE data_fields SET {field_assignments} WHERE dataset_id = ? AND field_id = ?",
            field_params,
        )
    connection.commit()
    return len(rows)

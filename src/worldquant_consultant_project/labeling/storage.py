"""Schema migration and persistence helpers for field labels (provider aware)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import DEFAULT_PROVIDER, LABEL_DIMENSIONS, FieldInput, ProviderSpec, get_provider


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


def _audit_schema(spec: ProviderSpec) -> str:
    columns = "".join(f"    {dimension:<16} TEXT,\n" for dimension in LABEL_DIMENSIONS)
    return f"""
CREATE TABLE IF NOT EXISTS {spec.audit_table} (
    model                TEXT    NOT NULL,
    dataset_id           TEXT    NOT NULL,
    field_id             TEXT    NOT NULL,
{columns}    raw_json             TEXT,
    prompt_tokens        INTEGER,
    completion_tokens    INTEGER,
    attempts             INTEGER NOT NULL DEFAULT 0,
    status               TEXT    NOT NULL,
    error                TEXT,
    system_prompt_sha256 TEXT,
    labeled_at           TEXT    NOT NULL,
    PRIMARY KEY (model, dataset_id, field_id)
);

CREATE INDEX IF NOT EXISTS idx_{spec.audit_table}_status
    ON {spec.audit_table} (model, status);
"""


def _view_sql(spec: ProviderSpec) -> str:
    columns = spec.columns
    projected = ",\n       ".join(columns.values())
    return f"""
DROP VIEW IF EXISTS {spec.view_name};
CREATE VIEW {spec.view_name} AS
SELECT DISTINCT
       dataset_id,
       field_id,
       description,
       type,
       category_name,
       subcategory_name,
       date_coverage,
       coverage,
       {projected}
FROM data_fields
WHERE {columns["category"]} IS NOT NULL;
"""


def ensure_schema(
    connection: sqlite3.Connection, provider: str | ProviderSpec = DEFAULT_PROVIDER
) -> list[str]:
    """Append the provider's label columns and create its audit table/view. Idempotent."""
    spec = provider if isinstance(provider, ProviderSpec) else get_provider(provider)
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(data_fields)")}
    added: list[str] = []
    for column in spec.columns.values():
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE data_fields ADD COLUMN {column} TEXT")
        added.append(column)
    connection.executescript(_audit_schema(spec))
    connection.executescript(_view_sql(spec))
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
    provider: str | ProviderSpec = DEFAULT_PROVIDER,
    dataset_filter: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[FieldInput]:
    """Distinct fields still needing labels for ``model`` with the current prompt."""
    spec = provider if isinstance(provider, ProviderSpec) else get_provider(provider)
    sql = f"""
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
        LEFT JOIN {spec.audit_table} AS l
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


def write_labels(
    connection: sqlite3.Connection,
    *,
    model: str,
    prompt_sha: str,
    results: Iterable[Mapping[str, Any]],
    provider: str | ProviderSpec = DEFAULT_PROVIDER,
) -> int:
    """Write one batch of results to the audit table and to ``data_fields``."""
    spec = provider if isinstance(provider, ProviderSpec) else get_provider(provider)
    batch = list(results)
    if not batch:
        return 0

    dimensions = list(LABEL_DIMENSIONS)
    columns = spec.columns
    audit_sql = f"""
        INSERT INTO {spec.audit_table} (
            model, dataset_id, field_id,
            {', '.join(dimensions)},
            raw_json, prompt_tokens, completion_tokens,
            attempts, status, error, system_prompt_sha256, labeled_at
        ) VALUES (
            :model, :dataset_id, :field_id,
            {', '.join(':' + d for d in dimensions)},
            :raw_json, :prompt_tokens, :completion_tokens,
            :attempts, :status, :error, :system_prompt_sha256, :labeled_at
        )
        ON CONFLICT (model, dataset_id, field_id) DO UPDATE SET
            {', '.join(f'{d} = excluded.{d}' for d in dimensions)},
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

    set_clause = ", ".join(f"{columns[d]} = ?" for d in dimensions)
    updates = [
        (*[item.get(d) for d in dimensions], item["dataset_id"], item["field_id"])
        for item in rows
        if item.get("status") == "ok"
    ]
    if updates:
        connection.executemany(
            f"UPDATE data_fields SET {set_clause} WHERE dataset_id = ? AND field_id = ?",
            updates,
        )
    connection.commit()
    return len(rows)


def label_summary(
    connection: sqlite3.Connection, provider: str | ProviderSpec = DEFAULT_PROVIDER
) -> dict[str, Any]:
    """Coverage and per-dimension distribution for one provider."""
    spec = provider if isinstance(provider, ProviderSpec) else get_provider(provider)
    columns = spec.columns
    totals = connection.execute(
        f"""
        SELECT COUNT(*) AS fields,
               SUM(CASE WHEN {columns["category"]} IS NOT NULL THEN 1 ELSE 0 END) AS labeled
        FROM (SELECT DISTINCT dataset_id, field_id, {columns["category"]} FROM data_fields)
        """
    ).fetchone()
    distributions: dict[str, list[tuple[str, int]]] = {}
    for dimension, column in columns.items():
        rows = connection.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM ("
            f" SELECT DISTINCT dataset_id, field_id, {column} FROM data_fields)"
            f" WHERE {column} IS NOT NULL GROUP BY 1 ORDER BY n DESC"
        ).fetchall()
        distributions[dimension] = [(row["value"], row["n"]) for row in rows]
    return {
        "fields": totals["fields"] if totals else 0,
        "labeled": totals["labeled"] if totals else 0,
        "distributions": distributions,
    }

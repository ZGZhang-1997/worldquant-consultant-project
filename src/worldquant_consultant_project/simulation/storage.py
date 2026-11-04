"""SQLite persistence for simulation runs: one row per backtest."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# BRAIN sample blocks in the /alphas/{id} payload, in column-name order.
SAMPLES: tuple[str, ...] = ("is", "os", "train", "test", "prod")

# Numeric metric columns expanded per sample as <sample>_<column>.
NUMERIC_METRICS: tuple[tuple[str, str, str], ...] = (
    ("sharpe", "sharpe", "REAL"),
    ("fitness", "fitness", "REAL"),
    ("returns", "returns", "REAL"),
    ("turnover", "turnover", "REAL"),
    ("drawdown", "drawdown", "REAL"),
    ("margin", "margin", "REAL"),
    ("pnl", "pnl", "REAL"),
    ("book_size", "bookSize", "REAL"),
    ("long_count", "longCount", "INTEGER"),
    ("short_count", "shortCount", "INTEGER"),
    ("self_correlation", "selfCorrelation", "REAL"),
    ("prod_correlation", "prodCorrelation", "REAL"),
)

TEXT_METRICS: tuple[tuple[str, str], ...] = (
    ("start_date", "startDate"),
    ("drawdown_start_date", "drawdownStartDate"),
)

COUNT_COLUMNS: tuple[str, ...] = ("checks_passed", "checks_failed", "checks_pending")

# Sample keys consumed by explicit columns; everything else lands in <sample>_metrics_json.
RESERVED_SAMPLE_KEYS = frozenset(
    {source for _, source, _ in NUMERIC_METRICS}
    | {source for _, source in TEXT_METRICS}
    | {"checks"}
)

METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sim_uid", "TEXT PRIMARY KEY"),
    ("expression", "TEXT NOT NULL"),
    ("language", "TEXT NOT NULL DEFAULT 'FASTEXPR'"),
    ("instrument_type", "TEXT"),
    ("region", "TEXT"),
    ("universe", "TEXT"),
    ("delay", "INTEGER"),
    ("decay", "INTEGER"),
    ("neutralization", "TEXT"),
    ("truncation", "REAL"),
    ("pasteurization", "TEXT"),
    ("unit_handling", "TEXT"),
    ("nan_handling", "TEXT"),
    ("settings_json", "TEXT NOT NULL"),
    ("settings_hash", "TEXT NOT NULL"),
    ("braint_sim_id", "TEXT"),
    ("alpha_id", "TEXT"),
    ("status", "TEXT NOT NULL"),
    ("has_error", "INTEGER NOT NULL DEFAULT 0"),
    ("error", "TEXT"),
    ("submitted_at", "TEXT NOT NULL"),
    ("finished_at", "TEXT"),
    ("duration_seconds", "REAL"),
    ("raw_simulation_json", "TEXT"),
    ("raw_alpha_json", "TEXT"),
)

LEGACY_TABLES = (
    "simulation_results",
    "simulation_metrics",
    "simulation_checks",
)
LEGACY_VIEW = "v_simulation_summary"


def sample_columns() -> list[tuple[str, str]]:
    """``(column, type)`` pairs for every per-sample metric column."""
    columns: list[tuple[str, str]] = []
    for sample in SAMPLES:
        columns.extend((f"{sample}_{name}", kind) for name, _, kind in NUMERIC_METRICS)
        columns.extend((f"{sample}_{name}", "TEXT") for name, _ in TEXT_METRICS)
        columns.extend((f"{sample}_{name}", "INTEGER") for name in COUNT_COLUMNS)
        columns.append((f"{sample}_checks_json", "TEXT"))
        columns.append((f"{sample}_metrics_json", "TEXT"))
    return columns


TABLE_COLUMNS: tuple[tuple[str, str], ...] = METADATA_COLUMNS + tuple(sample_columns())
COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in TABLE_COLUMNS)


def _schema_sql() -> str:
    body = ",\n    ".join(f"{name} {kind}" for name, kind in TABLE_COLUMNS)
    return f"""
CREATE TABLE IF NOT EXISTS simulations (
    {body}
);

CREATE INDEX IF NOT EXISTS idx_simulations_alpha    ON simulations (alpha_id);
CREATE INDEX IF NOT EXISTS idx_simulations_status   ON simulations (status);
CREATE INDEX IF NOT EXISTS idx_simulations_settings ON simulations (settings_hash);
CREATE INDEX IF NOT EXISTS idx_simulations_created  ON simulations (submitted_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    migrate_legacy(connection)
    connection.executescript(_schema_sql())
    connection.commit()
    return connection


# --------------------------------------------------------------------- writes


def sample_values(alpha: Mapping[str, Any], sample: str) -> dict[str, Any]:
    """Map one BRAIN sample block onto its ``<sample>_*`` columns."""
    prefix = sample
    block = alpha.get(sample)
    values: dict[str, Any] = {f"{prefix}_{name}": None for name, _, _ in NUMERIC_METRICS}
    values.update({f"{prefix}_{name}": None for name, _ in TEXT_METRICS})
    values.update({f"{prefix}_{name}": None for name in COUNT_COLUMNS})
    values[f"{prefix}_checks_json"] = None
    values[f"{prefix}_metrics_json"] = None
    if not isinstance(block, Mapping):
        return values

    checks = [c for c in (block.get("checks") or []) if isinstance(c, Mapping)]
    results = [str(c.get("result") or "").upper() for c in checks]
    values[f"{prefix}_checks_passed"] = sum(1 for r in results if r == "PASS")
    values[f"{prefix}_checks_failed"] = sum(1 for r in results if r == "FAIL")
    values[f"{prefix}_checks_pending"] = sum(1 for r in results if r == "PENDING")
    values[f"{prefix}_checks_json"] = json.dumps(checks, ensure_ascii=False) if checks else None

    leftovers: dict[str, Any] = {}
    for key, value in block.items():
        if key in RESERVED_SAMPLE_KEYS:
            continue
        leftovers[key] = value
    values[f"{prefix}_metrics_json"] = json.dumps(leftovers, ensure_ascii=False) if leftovers else None

    for name, source, kind in NUMERIC_METRICS:
        raw = block.get(source)
        if isinstance(raw, bool) or raw is None:
            values[f"{prefix}_{name}"] = None if raw is None else int(raw)
        elif isinstance(raw, (int, float)):
            values[f"{prefix}_{name}"] = raw
        else:
            values[f"{prefix}_{name}"] = None

    for name, source in TEXT_METRICS:
        raw = block.get(source)
        values[f"{prefix}_{name}"] = raw if isinstance(raw, str) else None

    return values


def build_row(
    *,
    sim_uid: str,
    expression: str,
    settings: Mapping[str, Any],
    settings_hash: str,
    status: str,
    submitted_at: str,
    braint_sim_id: str | None = None,
    alpha_id: str | None = None,
    error: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    simulation_body: Mapping[str, Any] | None = None,
    alpha_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete single-table row for one backtest."""
    row: dict[str, Any] = {
        "sim_uid": sim_uid,
        "expression": expression,
        "language": str(settings.get("language", "FASTEXPR")),
        "instrument_type": settings.get("instrument_type"),
        "region": settings.get("region"),
        "universe": settings.get("universe"),
        "delay": _as_int(settings.get("delay")),
        "decay": _as_int(settings.get("decay")),
        "neutralization": settings.get("neutralization"),
        "truncation": _as_float(settings.get("truncation")),
        "pasteurization": settings.get("pasteurization"),
        "unit_handling": settings.get("unit_handling"),
        "nan_handling": settings.get("nan_handling"),
        "settings_json": json.dumps(settings, ensure_ascii=False),
        "settings_hash": settings_hash,
        "braint_sim_id": braint_sim_id,
        "alpha_id": alpha_id,
        "status": status,
        "has_error": 0 if status in ("completed", "running") else 1,
        "error": error,
        "submitted_at": submitted_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "raw_simulation_json": _dump(simulation_body),
        "raw_alpha_json": _dump(alpha_body),
    }
    for sample in SAMPLES:
        row.update(
            sample_values(alpha_body or {}, sample)
            if alpha_body
            else {name: None for name, _ in sample_columns() if name.startswith(f"{sample}_")}
        )
    return row


def write_outcome(connection: sqlite3.Connection, **kwargs: Any) -> None:
    """Insert or update the single row describing one backtest."""
    row = build_row(**kwargs)
    placeholders = ", ".join(f":{name}" for name in COLUMN_NAMES)
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in COLUMN_NAMES if name != "sim_uid"
    )
    connection.execute(
        f"INSERT INTO simulations ({', '.join(COLUMN_NAMES)}) VALUES ({placeholders})"
        f" ON CONFLICT (sim_uid) DO UPDATE SET {updates}",
        row,
    )
    connection.commit()


# ------------------------------------------------------------------ migration


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def migrate_legacy(connection: sqlite3.Connection) -> bool:
    """Fold the former four-table layout into ``simulations``.

    Returns True when a migration was performed.
    """
    legacy_present = any(_table_exists(connection, name) for name in LEGACY_TABLES)
    if not legacy_present:
        return False

    connection.executescript("DROP VIEW IF EXISTS " + LEGACY_VIEW)
    for name in ("simulations", *LEGACY_TABLES):
        if _table_exists(connection, name):
            connection.execute(f"ALTER TABLE {name} RENAME TO {name}_legacy")
    connection.executescript(_schema_sql())

    old_rows = list(connection.execute("SELECT * FROM simulations_legacy"))
    migrated = 0
    for old in old_rows:
        sim_uid = old["sim_uid"]
        row: dict[str, Any] = {name: None for name in COLUMN_NAMES}
        for name in ("sim_uid", "expression", "language", "instrument_type", "region", "universe",
                     "delay", "decay", "neutralization", "truncation", "pasteurization",
                     "unit_handling", "nan_handling", "settings_json", "settings_hash",
                     "braint_sim_id", "alpha_id", "status", "error", "submitted_at",
                     "finished_at", "duration_seconds", "raw_simulation_json", "raw_alpha_json"):
            row[name] = old[name]
        row["has_error"] = 0 if old["status"] == "completed" else 1

        for result in connection.execute(
            "SELECT * FROM simulation_results_legacy WHERE sim_uid = ?", (sim_uid,)
        ):
            sample = result["sample"]
            if sample not in SAMPLES:
                continue
            key = dict(result)
            for name, source, _ in NUMERIC_METRICS:
                row[f"{sample}_{name}"] = key.get(source if source in key else name)
            for name, _source in TEXT_METRICS:
                row[f"{sample}_{name}"] = key.get(name)
            for name in COUNT_COLUMNS:
                row[f"{sample}_{name}"] = key.get(name)

        for metric in connection.execute(
            "SELECT * FROM simulation_metrics_legacy WHERE sim_uid = ?", (sim_uid,)
        ):
            sample = metric["sample"]
            name = metric["metric"]
            if sample not in SAMPLES or name in RESERVED_SAMPLE_KEYS:
                continue
            if metric["value"] is not None:
                leftover = _load_json(row[f"{sample}_metrics_json"])
                leftover[name] = metric["value"]
                row[f"{sample}_metrics_json"] = json.dumps(leftover, ensure_ascii=False)
            elif metric["value_text"] is not None:
                text_columns = {source: column for column, source in TEXT_METRICS}
                column_name = text_columns.get(name)
                if column_name and row[f"{sample}_{column_name}"] is None:
                    row[f"{sample}_{column_name}"] = metric["value_text"]

        checks = list(
            connection.execute(
                "SELECT * FROM simulation_checks_legacy WHERE sim_uid = ?", (sim_uid,)
            )
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for check in checks:
            grouped.setdefault(check["sample"], []).append(
                {
                    "name": check["name"],
                    "result": check["result"],
                    "value": check["value"],
                    "limit": check["limit"],
                    "year": check["year"],
                    "message": check["message"],
                }
            )
        for sample, items in grouped.items():
            if sample in SAMPLES:
                row[f"{sample}_checks_json"] = json.dumps(items, ensure_ascii=False)

        placeholders = ", ".join(f":{name}" for name in COLUMN_NAMES)
        connection.execute(
            f"INSERT INTO simulations ({', '.join(COLUMN_NAMES)}) VALUES ({placeholders})", row
        )
        migrated += 1

    new_count = connection.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
    if new_count != len(old_rows):
        raise RuntimeError(
            f"legacy migration incomplete: expected {len(old_rows)} rows, found {new_count}"
        )

    for name in LEGACY_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {name}_legacy")
    connection.execute("DROP TABLE IF EXISTS simulations_legacy")
    connection.commit()
    return True


# -------------------------------------------------------------------- helpers


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


SUMMARY_COLUMNS = (
    "sim_uid",
    "expression",
    "universe",
    "delay",
    "decay",
    "status",
    "has_error",
    "is_sharpe",
    "is_fitness",
    "os_sharpe",
    "os_fitness",
    "alpha_id",
    "duration_seconds",
    "submitted_at",
)


def recent(connection: sqlite3.Connection, limit: int = 10) -> Iterable[sqlite3.Row]:
    columns = ", ".join(SUMMARY_COLUMNS)
    return connection.execute(
        f"SELECT {columns} FROM simulations ORDER BY submitted_at DESC LIMIT ?", (limit,)
    ).fetchall()

"""SQLite persistence for simulation runs.

Two tables only:
``simulations``        one row per backtest: id, expression, settings JSON, completion,
                       timing, every in-sample metric and one column per submission check.
``simulation_details`` the 1:1 side table with identifiers, errors, other samples and raw payloads.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# In-sample metric columns: ``<column>`` holds ``is.<source>``.
IS_METRICS: tuple[tuple[str, str, str], ...] = (
    ("is_sharpe", "sharpe", "REAL"),
    ("is_fitness", "fitness", "REAL"),
    ("is_returns", "returns", "REAL"),
    ("is_turnover", "turnover", "REAL"),
    ("is_drawdown", "drawdown", "REAL"),
    ("is_margin", "margin", "REAL"),
    ("is_pnl", "pnl", "REAL"),
    ("is_book_size", "bookSize", "REAL"),
    ("is_long_count", "longCount", "INTEGER"),
    ("is_short_count", "shortCount", "INTEGER"),
    ("is_start_date", "startDate", "TEXT"),
)

# Samples that are only kept as JSON in the side table.
OTHER_SAMPLES: tuple[str, ...] = ("os", "train", "test", "prod")

CHECK_COLUMN_PREFIX = "check_"
CHECK_RESULTS = ("PASS", "FAIL", "PENDING")

# Self-correlation is only computed at submission time, so it is excluded from the
# "would pass everything else" verdict.
EXCLUDED_FROM_TENTATIVE = ("check_self_correlation",)
TENTATIVE_COLUMN = "tentative_submittable"

MAIN_COLUMNS: tuple[str, ...] = (
    "sim_uid",
    "expression",
    TENTATIVE_COLUMN,
    "settings_json",
    "completed",
    "simulated_at",
    "duration_seconds",
) + tuple(column for column, _, _ in IS_METRICS)

DETAIL_COLUMNS: tuple[str, ...] = (
    "sim_uid",
    "alpha_id",
    "braint_sim_id",
    "error",
    "finished_at",
    "other_samples_json",
    "is_extra_json",
    "checks_json",
    "raw_simulation_json",
    "raw_alpha_json",
)

_SIMULATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS {table} (
    sim_uid          TEXT PRIMARY KEY,
    expression       TEXT NOT NULL,
    tentative_submittable INTEGER NOT NULL DEFAULT 0,
    settings_json    TEXT NOT NULL,
    completed        INTEGER NOT NULL DEFAULT 0,
    simulated_at     TEXT NOT NULL,
    duration_seconds REAL,
    {metric_columns}
);
"""

_DETAILS_TABLE = """
CREATE TABLE IF NOT EXISTS simulation_details (
    sim_uid             TEXT PRIMARY KEY,
    alpha_id            TEXT,
    braint_sim_id       TEXT,
    error               TEXT,
    finished_at         TEXT,
    other_samples_json  TEXT,
    is_extra_json       TEXT,
    checks_json         TEXT,
    raw_simulation_json TEXT,
    raw_alpha_json      TEXT,
    FOREIGN KEY (sim_uid) REFERENCES simulations (sim_uid) ON DELETE CASCADE
);
"""

_INDEXES_AND_VIEW = """
CREATE INDEX IF NOT EXISTS idx_simulations_simulated ON simulations (simulated_at);
CREATE INDEX IF NOT EXISTS idx_details_alpha ON simulation_details (alpha_id);

DROP VIEW IF EXISTS v_simulation_summary;
CREATE VIEW v_simulation_summary AS
SELECT s.sim_uid,
       s.expression,
       s.tentative_submittable,
       s.completed,
       s.simulated_at,
       s.duration_seconds,
       s.is_sharpe,
       s.is_fitness,
       s.is_turnover,
       d.alpha_id
FROM simulations AS s
LEFT JOIN simulation_details AS d ON d.sim_uid = s.sim_uid
ORDER BY s.simulated_at DESC;
"""


def _base_schema() -> str:
    return (
        _SIMULATIONS_TABLE.format(table="simulations", metric_columns=_metric_column_sql())
        + _DETAILS_TABLE
        + _INDEXES_AND_VIEW
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_column(name: str) -> str:
    """Map a BRAIN check name onto its column, e.g. LOW_SHARPE -> check_low_sharpe."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return f"{CHECK_COLUMN_PREFIX}{slug or 'unnamed'}"


def _metric_column_sql() -> str:
    return ",\n    ".join(f"{column} {kind}" for column, _, kind in IS_METRICS)


def _needs_rebuild(connection: sqlite3.Connection) -> bool:
    """The pre-simplification table had 122 columns and no ``completed`` flag."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(simulations)")}
    return bool(columns) and "completed" not in columns


def _rename_legacy_timestamp(connection: sqlite3.Connection) -> bool:
    """Rename ``submitted_at`` to ``simulated_at``: the timestamp marks the run start."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(simulations)")}
    if "submitted_at" not in columns or "simulated_at" in columns:
        return False
    connection.execute("DROP VIEW IF EXISTS v_simulation_summary")
    connection.execute("ALTER TABLE simulations RENAME COLUMN submitted_at TO simulated_at")
    connection.execute("DROP INDEX IF EXISTS idx_simulations_submitted")
    connection.commit()
    return True


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    if _needs_rebuild(connection):
        # The simplified layout starts empty: old wide tables are dropped on purpose.
        connection.executescript(
            "DROP VIEW IF EXISTS v_simulation_summary;"
            "DROP TABLE IF EXISTS simulation_details;"
            "DROP TABLE IF EXISTS simulations;"
            "DROP TABLE IF EXISTS simulation_results;"
            "DROP TABLE IF EXISTS simulation_metrics;"
            "DROP TABLE IF EXISTS simulation_checks;"
        )
    _rename_legacy_timestamp(connection)
    # The view references tentative_submittable, so any table rebuild must happen first.
    if not _insert_tentative_column(connection):
        connection.executescript(_base_schema())
    connection.commit()
    return connection


def _insert_tentative_column(connection: sqlite3.Connection) -> bool:
    """Rebuild the table so ``tentative_submittable`` sits right after ``expression``.

    SQLite can only append columns, and the wide-table layout already shipped without
    it, so the existing rows are copied into a fresh table with the wanted order.
    """
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(simulations)")]
    if not columns or TENTATIVE_COLUMN in columns:
        return False

    existing = set(columns)
    check_columns = [
        name
        for name in columns
        if name.startswith(CHECK_COLUMN_PREFIX) and name not in EXCLUDED_FROM_TENTATIVE
    ]
    all_check_columns = [name for name in columns if name.startswith(CHECK_COLUMN_PREFIX)]
    copy_columns = [
        name
        for name in MAIN_COLUMNS + tuple(all_check_columns)
        if name != TENTATIVE_COLUMN and name in existing
    ]
    if check_columns:
        condition = " AND ".join(f"COALESCE({name}, '') = 'PASS'" for name in check_columns)
        tentative_sql = f"CASE WHEN {condition} THEN 1 ELSE 0 END"
    else:
        tentative_sql = "0"

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            "DROP VIEW IF EXISTS v_simulation_summary;"
            "DROP TABLE IF EXISTS simulations_new;"
        )
        connection.executescript(
            _SIMULATIONS_TABLE.format(
                table="simulations_new", metric_columns=_metric_column_sql()
            )
        )
        ensure_check_columns(
            connection,
            [name[len(CHECK_COLUMN_PREFIX):] for name in columns if name.startswith(CHECK_COLUMN_PREFIX)],
            table="simulations_new",
        )
        connection.execute(
            f"INSERT INTO simulations_new ({', '.join(copy_columns + [TENTATIVE_COLUMN])}) "
            f"SELECT {', '.join(copy_columns)}, {tentative_sql} FROM simulations"
        )
        connection.execute("DROP TABLE simulations")
        connection.execute("ALTER TABLE simulations_new RENAME TO simulations")
        connection.executescript(_base_schema())
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    return True


def ensure_check_columns(
    connection: sqlite3.Connection, names: Iterable[str], *, table: str = "simulations"
) -> list[str]:
    """Add one column per newly seen submission check. Idempotent."""
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    added: list[str] = []
    for name in names:
        column = check_column(name)
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        existing.add(column)
        added.append(column)
    if added:
        connection.commit()
    return added


def known_check_columns(connection: sqlite3.Connection) -> list[str]:
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(simulations)")]
    return [column for column in columns if column.startswith(CHECK_COLUMN_PREFIX)]


# --------------------------------------------------------------------- writes


def _is_block(alpha: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not alpha:
        return {}
    block = alpha.get("is")
    return block if isinstance(block, Mapping) else {}


def _checks(alpha: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [c for c in (_is_block(alpha).get("checks") or []) if isinstance(c, Mapping)]


def check_values(alpha: Mapping[str, Any] | None) -> dict[str, str]:
    """``{check_column: PASS|FAIL|PENDING}`` for one alpha."""
    values: dict[str, str] = {}
    for check in _checks(alpha):
        result = str(check.get("result") or "").upper()
        values[check_column(str(check.get("name") or "unnamed"))] = (
            result if result in CHECK_RESULTS else "UNKNOWN"
        )
    return values


def tentative_submittable(checks: Mapping[str, str]) -> int:
    """1 when every check except self-correlation passed, else 0.

    Self-correlation is only evaluated at submission time, so it must not block this
    preliminary verdict; a missing or pending check still counts as "not passed".
    """
    required = {
        column: result
        for column, result in checks.items()
        if column not in EXCLUDED_FROM_TENTATIVE
    }
    if not required:
        return 0
    return 1 if all(result == "PASS" for result in required.values()) else 0


def derive_submittable(alpha: Mapping[str, Any] | None) -> tuple[int | None, list[dict[str, Any]]]:
    """Whether BRAIN would accept the alpha, plus the blocking checks.

    Kept as a helper for callers/reporting; the database derives it from the check columns.
    """
    checks = _checks(alpha)
    if not checks:
        return None, []
    blockers = [
        {
            "name": check.get("name"),
            "result": check.get("result"),
            "value": check.get("value"),
            "limit": check.get("limit"),
        }
        for check in checks
        if str(check.get("result") or "").upper() in ("FAIL", "PENDING")
    ]
    return (0 if blockers else 1), blockers


def build_rows(
    *,
    sim_uid: str,
    expression: str,
    settings: Mapping[str, Any],
    completed: bool,
    simulated_at: str,
    duration_seconds: float | None = None,
    alpha_id: str | None = None,
    braint_sim_id: str | None = None,
    error: str | None = None,
    finished_at: str | None = None,
    simulation_body: Mapping[str, Any] | None = None,
    alpha_body: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the main row and its side-table row for one backtest."""
    block = _is_block(alpha_body)
    checks = check_values(alpha_body)
    main: dict[str, Any] = {
        "sim_uid": sim_uid,
        "expression": expression,
        TENTATIVE_COLUMN: tentative_submittable(checks),
        "settings_json": json.dumps(settings, ensure_ascii=False, sort_keys=True),
        "completed": 1 if completed else 0,
        "simulated_at": simulated_at,
        "duration_seconds": duration_seconds,
    }
    for column, source, _ in IS_METRICS:
        value = block.get(source)
        if isinstance(value, bool):
            main[column] = int(value)
        elif isinstance(value, (int, float, str)) or value is None:
            main[column] = value
        else:
            main[column] = None
    main.update(checks)

    known_sources = {source for _, source, _ in IS_METRICS}
    extra_is = {k: v for k, v in block.items() if k not in known_sources and k != "checks"}
    others = {
        sample: alpha_body[sample]
        for sample in OTHER_SAMPLES
        if alpha_body and isinstance(alpha_body.get(sample), Mapping)
    }
    details: dict[str, Any] = {
        "sim_uid": sim_uid,
        "alpha_id": alpha_id,
        "braint_sim_id": braint_sim_id,
        "error": error,
        "finished_at": finished_at,
        "other_samples_json": json.dumps(others, ensure_ascii=False) if others else None,
        "is_extra_json": json.dumps(extra_is, ensure_ascii=False) if extra_is else None,
        "checks_json": (
            json.dumps(_checks(alpha_body), ensure_ascii=False) if alpha_body else None
        ),
        "raw_simulation_json": _dump(simulation_body),
        "raw_alpha_json": _dump(alpha_body),
    }
    return main, details


def write_outcome(connection: sqlite3.Connection, **kwargs: Any) -> None:
    """Insert or update one backtest (main row + side row), adding check columns as needed."""
    main, details = build_rows(**kwargs)
    new_checks = [column for column in main if column.startswith(CHECK_COLUMN_PREFIX)]
    ensure_check_columns(connection, [column[len(CHECK_COLUMN_PREFIX):] for column in new_checks])

    main_columns = list(main)
    placeholders = ", ".join(f":{name}" for name in main_columns)
    updates = ", ".join(f"{name} = excluded.{name}" for name in main_columns if name != "sim_uid")
    connection.execute(
        f"INSERT INTO simulations ({', '.join(main_columns)}) VALUES ({placeholders})"
        f" ON CONFLICT (sim_uid) DO UPDATE SET {updates}",
        main,
    )
    detail_placeholders = ", ".join(f":{name}" for name in DETAIL_COLUMNS)
    detail_updates = ", ".join(
        f"{name} = excluded.{name}" for name in DETAIL_COLUMNS if name != "sim_uid"
    )
    connection.execute(
        f"INSERT INTO simulation_details ({', '.join(DETAIL_COLUMNS)}) VALUES ({detail_placeholders})"
        f" ON CONFLICT (sim_uid) DO UPDATE SET {detail_updates}",
        details,
    )
    connection.commit()


# -------------------------------------------------------------------- reading


def summary_columns(connection: sqlite3.Connection) -> list[str]:
    return [
        "sim_uid",
        "expression",
        "completed",
        "simulated_at",
        "duration_seconds",
        "is_sharpe",
        "is_fitness",
        "is_turnover",
        "alpha_id",
    ]


def recent(connection: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM v_simulation_summary LIMIT ?", (limit,)
        )
    )


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

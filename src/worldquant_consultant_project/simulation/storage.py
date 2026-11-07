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
from typing import Any, Iterable, Mapping, Sequence

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

# Self-correlation is normally PENDING until submission, so only an explicit FAIL blocks
# the preliminary verdict; it is tolerated rather than required.
TOLERATED_CHECKS = ("check_self_correlation",)
TENTATIVE_COLUMN = "tentative_submittable"
# Bumped whenever the tentative rule changes so existing rows get recomputed.
TENTATIVE_RULE_VERSION = "2"

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
    "error_kind",
    "finished_at",
    "other_samples_json",
    "is_extra_json",
    "checks_json",
    "raw_simulation_json",
    "raw_alpha_json",
)

#: ``error_kind`` values. Only ``factor`` errors mean the alpha was really evaluated.
ERROR_KIND_FACTOR = "factor"
ERROR_KIND_PLATFORM = "platform"

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
    error_kind          TEXT,
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
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_simulations_simulated ON simulations (simulated_at);
CREATE INDEX IF NOT EXISTS idx_details_alpha ON simulation_details (alpha_id);

-- Same expression with the same settings is one logical run: a re-run overwrites it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_simulations_expression_settings
    ON simulations (expression, settings_json);

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


#: Substrings that identify a failure caused by the platform, not by the factor.
PLATFORM_ERROR_MARKERS: tuple[str, ...] = (
    "incorrect authentication credentials",
    "daily_simulation_limit",
    "concurrent_simulation_limit",
    "simulation did not finish",
    "connection aborted",
    "connection error",
    "max retries exceeded",
    "interrupted:",
    "no location header",
)


def classify_error_text(text: str | None) -> str | None:
    """Classify a stored error message: ``platform`` or ``factor``.

    Used for rows written before ``error_kind`` existed, and as a fallback whenever a
    caller only has the message.
    """
    if not text:
        return None
    lowered = text.lower()
    if re.search(r"http (401|403|404|408|425|429)\b", lowered) or re.search(
        r"http 5\d\d", lowered
    ):
        return ERROR_KIND_PLATFORM
    if any(marker in lowered for marker in PLATFORM_ERROR_MARKERS):
        return ERROR_KIND_PLATFORM
    return ERROR_KIND_FACTOR


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


def connect(path: Path | str, *, initialize: bool = True) -> sqlite3.Connection:
    """Open the simulations database.

    ``initialize=False`` skips the schema/migration work, which is what worker threads
    want: the batch runner initialises once up front, then each worker opens cheaply.
    """
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA foreign_keys = ON")
    if not initialize:
        return connection
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
    _ensure_error_kind(connection)
    _refresh_tentative(connection)
    connection.commit()
    return connection


def _ensure_error_kind(connection: sqlite3.Connection) -> int:
    """Add ``simulation_details.error_kind`` and classify rows written before it existed."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(simulation_details)")}
    if "error_kind" not in columns:
        connection.execute("ALTER TABLE simulation_details ADD COLUMN error_kind TEXT")
        connection.commit()

    unclassified = connection.execute(
        "SELECT sim_uid, error FROM simulation_details WHERE error IS NOT NULL AND error_kind IS NULL"
    ).fetchall()
    if not unclassified:
        return 0
    connection.executemany(
        "UPDATE simulation_details SET error_kind = ? WHERE sim_uid = ?",
        [(classify_error_text(row["error"]), row["sim_uid"]) for row in unclassified],
    )
    connection.commit()
    return len(unclassified)


def _refresh_tentative(connection: sqlite3.Connection) -> int:
    """Recompute ``tentative_submittable`` when the rule changed since the rows were written."""
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'tentative_rule'"
    ).fetchone()
    if row is not None and row[0] == TENTATIVE_RULE_VERSION:
        return 0

    columns = [item["name"] for item in connection.execute("PRAGMA table_info(simulations)")]
    if not columns:
        return 0
    updated = connection.execute(
        f"UPDATE simulations SET {TENTATIVE_COLUMN} = {_tentative_sql(columns)}"
    ).rowcount
    connection.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('tentative_rule', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (TENTATIVE_RULE_VERSION,),
    )
    connection.commit()
    return updated


def _insert_tentative_column(connection: sqlite3.Connection) -> bool:
    """Rebuild the table so ``tentative_submittable`` sits right after ``expression``.

    SQLite can only append columns, and the wide-table layout already shipped without
    it, so the existing rows are copied into a fresh table with the wanted order.
    """
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(simulations)")]
    if not columns or TENTATIVE_COLUMN in columns:
        return False

    existing = set(columns)
    all_check_columns = [name for name in columns if name.startswith(CHECK_COLUMN_PREFIX)]
    copy_columns = [
        name
        for name in MAIN_COLUMNS + tuple(all_check_columns)
        if name != TENTATIVE_COLUMN and name in existing
    ]
    tentative_sql = _tentative_sql(all_check_columns)

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
        try:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as exc:
            # Another worker may have added the same column between our check and the ALTER.
            if "duplicate column" not in str(exc).lower():
                raise
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
    """1 when the other checks all passed and self-correlation did not fail.

    Self-correlation usually stays ``PENDING`` until the alpha is submitted, so both
    ``PASS`` and ``PENDING`` (or missing) are acceptable; only an explicit ``FAIL`` blocks.
    Any other check must be ``PASS``.
    """
    if not checks:
        return 0
    for column, result in checks.items():
        if column in TOLERATED_CHECKS:
            if result == "FAIL":
                return 0
        elif result != "PASS":
            return 0
    return 1


def _tentative_sql(check_columns: Sequence[str]) -> str:
    """SQL equivalent of :func:`tentative_submittable` over table columns."""
    checks = [name for name in check_columns if name.startswith(CHECK_COLUMN_PREFIX)]
    if not checks:
        return "0"
    required = [name for name in checks if name not in TOLERATED_CHECKS]
    tolerated = [name for name in checks if name in TOLERATED_CHECKS]
    conditions = [f"COALESCE({name}, '') = 'PASS'" for name in required]
    conditions += [f"COALESCE({name}, '') != 'FAIL'" for name in tolerated]
    any_present = " OR ".join(f"{name} IS NOT NULL" for name in checks)
    return f"CASE WHEN ({' AND '.join(conditions)}) AND ({any_present}) THEN 1 ELSE 0 END"


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
    error_kind: str | None = None,
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
        "settings_json": settings_json(settings),
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
        "error_kind": error_kind or (classify_error_text(error) if error else None),
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


def settings_json(settings: Mapping[str, Any]) -> str:
    """Canonical settings text: identical settings always produce identical JSON."""
    return json.dumps(settings, ensure_ascii=False, sort_keys=True)


def find_sim_uid(
    connection: sqlite3.Connection, expression: str, settings_text: str
) -> str | None:
    """The uid of an existing run with the same expression and settings, if any."""
    row = connection.execute(
        "SELECT sim_uid FROM simulations WHERE expression = ? AND settings_json = ?",
        (expression, settings_text),
    ).fetchone()
    return str(row[0]) if row else None


def write_outcome(connection: sqlite3.Connection, **kwargs: Any) -> None:
    """Insert or update one backtest (main row + side row), adding check columns as needed.

    A row is identified by ``(expression, settings_json)``: re-running the same expression
    with the same settings overwrites the previous result but keeps its ``sim_uid``.
    """
    main, details = build_rows(**kwargs)
    existing = find_sim_uid(connection, main["expression"], main["settings_json"])
    if existing:
        main["sim_uid"] = existing
        details["sim_uid"] = existing

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


def platform_error_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Failed runs whose failure came from the platform, so the factor was never judged."""
    return list(
        connection.execute(
            """
            SELECT s.sim_uid, s.expression, d.error
            FROM simulations AS s
            JOIN simulation_details AS d ON d.sim_uid = s.sim_uid
            WHERE s.completed = 0
              AND d.error IS NOT NULL
              AND d.error_kind = ?
            ORDER BY s.simulated_at
            """,
            (ERROR_KIND_PLATFORM,),
        )
    )


def delete_simulations(connection: sqlite3.Connection, sim_uids: Sequence[str]) -> int:
    """Delete runs by uid (details cascade). Returns the number of rows removed."""
    uids = list(sim_uids)
    if not uids:
        return 0
    removed = 0
    for start in range(0, len(uids), 200):
        batch = uids[start : start + 200]
        removed += connection.execute(
            f"DELETE FROM simulations WHERE sim_uid IN ({','.join('?' * len(batch))})", batch
        ).rowcount
    connection.commit()
    return removed


def pending_placeholders(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows written at submit time whose final result was never stored.

    This happens when the process is stopped while the platform is still computing:
    the row keeps ``braint_sim_id`` but has no error, result or completion time.
    """
    return list(
        connection.execute(
            """
            SELECT s.sim_uid,
                   s.expression,
                   s.settings_json,
                   s.simulated_at,
                   d.braint_sim_id
            FROM simulations AS s
            JOIN simulation_details AS d ON d.sim_uid = s.sim_uid
            WHERE s.completed = 0
              AND d.error IS NULL
              AND d.finished_at IS NULL
              AND d.braint_sim_id IS NOT NULL
            ORDER BY s.simulated_at
            """
        )
    )


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

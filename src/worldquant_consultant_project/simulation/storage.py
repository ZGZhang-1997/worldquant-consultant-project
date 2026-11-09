"""Simulation persistence, attached to the factor database (``data/alpha.db``).

Each backtest hangs off the factor it belongs to, so one ``alpha_uid`` finds every run
made for that factor (possibly several, with different settings):

``alpha_simulations``          one row per backtest: settings, completion, timing,
                               every in-sample metric and one column per submission check.
``alpha_simulation_details``   1:1 side table: BRAIN ids, errors, other samples, raw payloads.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..acquisition.config import DEFAULT_DATA_DIR

DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "alpha.db"

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

SIMULATIONS_TABLE = "alpha_simulations"
DETAILS_TABLE = "alpha_simulation_details"

#: Aggregate columns on ``alphas``. They summarise every run of a factor and are
#: recomputed whenever the factor's runs change.
ALPHA_AGGREGATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tentative_submittable", "INTEGER"),
    ("max_is_sharpe", "REAL"),
    ("max_is_fitness", "REAL"),
    ("max_is_sharpe_delay1", "REAL"),
    ("max_is_fitness_delay1", "REAL"),
    ("max_is_sharpe_delay0", "REAL"),
    ("max_is_fitness_delay0", "REAL"),
)

#: Delays with their own max-metric columns.
AGGREGATE_DELAYS: tuple[int, ...] = (1, 0)

#: ``delay`` of a run, taken from ``settings_json``; NULL when the JSON has no delay.
_DELAY_SQL = (
    "CAST(json_extract("
    "CASE WHEN json_valid(s.settings_json) THEN s.settings_json ELSE '{}' END,"
    " '$.delay') AS INTEGER)"
)

MAIN_COLUMNS: tuple[str, ...] = (
    "sim_uid",
    "alpha_uid",
    "expression",
    TENTATIVE_COLUMN,
    "settings_json",
    "completed",
    "simulated_at",
    "duration_seconds",
) + tuple(column for column, _, _ in IS_METRICS)

DETAIL_COLUMNS: tuple[str, ...] = (
    "sim_uid",
    "alpha_uid",
    "braint_alpha_id",
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

_ALPHAS_TABLE = """
CREATE TABLE IF NOT EXISTS alphas (
    alpha_uid    TEXT PRIMARY KEY,
    expression   TEXT NOT NULL,
    simulated    INTEGER,
    simulated_at TEXT,
    tentative_submittable INTEGER,
    max_is_sharpe REAL,
    max_is_fitness REAL,
    max_is_sharpe_delay1 REAL,
    max_is_fitness_delay1 REAL,
    max_is_sharpe_delay0 REAL,
    max_is_fitness_delay0 REAL
);
"""


def _max_metric_sql(column: str, metric: str | None = None, delay: int | None = None) -> str:
    """``MAX(<metric>)`` over the runs of the factor, optionally only for one ``delay``."""
    name = metric or column.removeprefix("max_")
    for suffix in (*(f"_delay{d}" for d in AGGREGATE_DELAYS),):
        name = name.removesuffix(suffix)
    where = ["s.alpha_uid = alphas.alpha_uid"]
    if delay is not None:
        where.append(f"{_DELAY_SQL} = {delay}")
    return (
        f"(SELECT MAX(s.{name}) FROM {SIMULATIONS_TABLE} AS s "
        f"WHERE {' AND '.join(where)})"
    )


_ALPHA_AGGREGATE_PARTS: list[str] = [
    f"""tentative_submittable = COALESCE((
        SELECT MAX(s.tentative_submittable) FROM {SIMULATIONS_TABLE} AS s
        WHERE s.alpha_uid = alphas.alpha_uid), 0)""",
    f"max_is_sharpe = {_max_metric_sql('max_is_sharpe')}",
    f"max_is_fitness = {_max_metric_sql('max_is_fitness')}",
]
for _delay in AGGREGATE_DELAYS:
    for _metric in ("sharpe", "fitness"):
        _column = f"max_is_{_metric}_delay{_delay}"
        _ALPHA_AGGREGATE_PARTS.append(
            f"{_column} = {_max_metric_sql(_column, f'is_{_metric}', _delay)}"
        )

#: ``UPDATE alphas SET ?`` body that recomputes the aggregate columns of a factor.
_ALPHA_AGGREGATE_SET = "\n    " + ",\n    ".join(_ALPHA_AGGREGATE_PARTS) + "\n"
del _delay, _metric, _column

_SIMULATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS {table} (
    sim_uid          TEXT PRIMARY KEY,
    alpha_uid        TEXT NOT NULL,
    expression       TEXT NOT NULL,
    tentative_submittable INTEGER NOT NULL DEFAULT 0,
    settings_json    TEXT NOT NULL,
    completed        INTEGER NOT NULL DEFAULT 0,
    simulated_at     TEXT NOT NULL,
    duration_seconds REAL,
    {metric_columns},
    FOREIGN KEY (alpha_uid) REFERENCES alphas (alpha_uid) ON DELETE CASCADE
);
"""

_DETAILS_TABLE = """
CREATE TABLE IF NOT EXISTS {table} (
    sim_uid             TEXT PRIMARY KEY,
    alpha_uid           TEXT NOT NULL,
    braint_alpha_id     TEXT,
    braint_sim_id       TEXT,
    error               TEXT,
    error_kind          TEXT,
    finished_at         TEXT,
    other_samples_json  TEXT,
    is_extra_json       TEXT,
    checks_json         TEXT,
    raw_simulation_json TEXT,
    raw_alpha_json      TEXT,
    FOREIGN KEY (sim_uid) REFERENCES alpha_simulations (sim_uid) ON DELETE CASCADE,
    FOREIGN KEY (alpha_uid) REFERENCES alphas (alpha_uid) ON DELETE CASCADE
);
"""

_INDEXES_AND_VIEW = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Lookups by factor are the main access path.
CREATE INDEX IF NOT EXISTS idx_alphas_expression
    ON alphas (expression);
CREATE INDEX IF NOT EXISTS idx_alpha_simulations_alpha
    ON alpha_simulations (alpha_uid);
CREATE INDEX IF NOT EXISTS idx_alpha_simulations_simulated
    ON alpha_simulations (simulated_at);
CREATE INDEX IF NOT EXISTS idx_alpha_simulation_details_braint
    ON alpha_simulation_details (braint_alpha_id);

-- Same expression with the same settings is one logical run: a re-run overwrites it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alpha_simulations_expression_settings
    ON alpha_simulations (expression, settings_json);

DROP VIEW IF EXISTS v_alpha_simulation_summary;
CREATE VIEW v_alpha_simulation_summary AS
SELECT s.sim_uid,
       s.alpha_uid,
       s.expression,
       s.tentative_submittable,
       s.completed,
       s.simulated_at,
       s.duration_seconds,
       s.is_sharpe,
       s.is_fitness,
       s.is_turnover,
       det.braint_alpha_id
FROM alpha_simulations AS s
LEFT JOIN alpha_simulation_details AS det ON det.sim_uid = s.sim_uid;
"""

def _schema() -> str:
    return (
        _ALPHAS_TABLE
        + _SIMULATIONS_TABLE.format(
            table=SIMULATIONS_TABLE, metric_columns=_metric_column_sql()
        )
        + _DETAILS_TABLE.format(table=DETAILS_TABLE)
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


def connect(path: Path | str | None = None, *, initialize: bool = True) -> sqlite3.Connection:
    """Open the factor database and make sure the simulation tables exist.

    ``initialize=False`` skips the schema work, which is what worker threads want: the
    batch runner initialises once up front, then each worker opens cheaply.
    """
    db_path = Path(path) if path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA foreign_keys = ON")
    if not initialize:
        return connection

    connection.executescript(_schema())
    _ensure_error_kind(connection)
    _ensure_alpha_aggregates(connection)
    if _refresh_tentative(connection):
        refresh_all_alpha_metrics(connection)
        connection.commit()
    return connection


def _ensure_error_kind(connection: sqlite3.Connection) -> int:
    """Add ``error_kind`` if missing and classify rows that lack it."""
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({DETAILS_TABLE})")}
    if "error_kind" not in columns:
        connection.execute(f"ALTER TABLE {DETAILS_TABLE} ADD COLUMN error_kind TEXT")
        connection.commit()

    unclassified = connection.execute(
        f"SELECT sim_uid, error FROM {DETAILS_TABLE} "
        "WHERE error IS NOT NULL AND error_kind IS NULL"
    ).fetchall()
    if not unclassified:
        return 0
    connection.executemany(
        f"UPDATE {DETAILS_TABLE} SET error_kind = ? WHERE sim_uid = ?",
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

    columns = [
        item["name"] for item in connection.execute(f"PRAGMA table_info({SIMULATIONS_TABLE})")
    ]
    if not columns:
        return 0
    updated = connection.execute(
        f"UPDATE {SIMULATIONS_TABLE} SET {TENTATIVE_COLUMN} = {_tentative_sql(columns)}"
    ).rowcount
    connection.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('tentative_rule', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (TENTATIVE_RULE_VERSION,),
    )
    connection.commit()
    return updated


def _ensure_alpha_aggregates(connection: sqlite3.Connection) -> int:
    """Add the aggregate columns on ``alphas`` and backfill them from existing runs."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(alphas)")}
    missing = [(name, kind) for name, kind in ALPHA_AGGREGATE_COLUMNS if name not in columns]
    if not missing:
        return 0
    for name, kind in missing:
        connection.execute(f"ALTER TABLE alphas ADD COLUMN {name} {kind}")
    refresh_all_alpha_metrics(connection)
    connection.commit()
    return len(missing)


def refresh_alpha_metrics(
    connection: sqlite3.Connection, alpha_uids: Iterable[str] | str
) -> int:
    """Recompute the aggregate columns of the given factors from their runs.

    ``tentative_submittable`` is 1 as soon as one run allows submission, ``max_is_sharpe``
    and ``max_is_fitness`` are the best in-sample values over all runs (they may come from
    different runs).
    """
    uids = [alpha_uids] if isinstance(alpha_uids, str) else list(alpha_uids)
    uids = [uid for uid in dict.fromkeys(uids) if uid]
    if not uids:
        return 0
    updated = 0
    for start in range(0, len(uids), 200):
        batch = uids[start : start + 200]
        updated += connection.execute(
            f"UPDATE alphas SET {_ALPHA_AGGREGATE_SET} "
            f"WHERE alpha_uid IN ({','.join('?' * len(batch))})",
            batch,
        ).rowcount
    connection.commit()
    return updated


def refresh_all_alpha_metrics(connection: sqlite3.Connection) -> int:
    """Recompute the aggregate columns of every factor."""
    return connection.execute(f"UPDATE alphas SET {_ALPHA_AGGREGATE_SET}").rowcount


def ensure_alpha(connection: sqlite3.Connection, expression: str) -> tuple[str, bool]:
    """Return ``(alpha_uid, created)`` for an expression, registering it when unknown.

    Ad-hoc expressions run outside the template pipeline still get an ``alpha_uid`` so
    their backtests are reachable the same way as generated factors.
    """
    row = connection.execute(
        "SELECT alpha_uid FROM alphas WHERE expression = ? ORDER BY alpha_uid LIMIT 1",
        (expression,),
    ).fetchone()
    if row is not None:
        return str(row["alpha_uid"]), False
    alpha_uid = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO alphas (alpha_uid, expression, simulated, simulated_at) VALUES (?, ?, NULL, NULL)",
        (alpha_uid, expression),
    )
    connection.commit()
    return alpha_uid, True


def find_alpha_uid(connection: sqlite3.Connection, expression: str) -> str | None:
    row = connection.execute(
        "SELECT alpha_uid FROM alphas WHERE expression = ? ORDER BY alpha_uid LIMIT 1",
        (expression,),
    ).fetchone()
    return str(row["alpha_uid"]) if row else None


def mark_alpha_simulated(
    connection: sqlite3.Connection, alpha_uid: str, when: str | None = None
) -> int:
    """Flag the factor as backtested."""
    updated = connection.execute(
        "UPDATE alphas SET simulated = 1, simulated_at = ? WHERE alpha_uid = ?",
        (when or utc_now(), alpha_uid),
    ).rowcount
    connection.commit()
    return updated


def unmark_alpha_simulated(connection: sqlite3.Connection, alpha_uids: Iterable[str]) -> int:
    """Reset the backtested flag so the factor is picked up again."""
    uids = [uid for uid in dict.fromkeys(alpha_uids) if uid]
    if not uids:
        return 0
    updated = 0
    for start in range(0, len(uids), 200):
        batch = uids[start : start + 200]
        updated += connection.execute(
            f"UPDATE alphas SET simulated = NULL, simulated_at = NULL "
            f"WHERE alpha_uid IN ({','.join('?' * len(batch))})",
            batch,
        ).rowcount
    connection.commit()
    return updated


def ensure_check_columns(
    connection: sqlite3.Connection, names: Iterable[str], *, table: str = SIMULATIONS_TABLE
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
    columns = [
        row["name"] for row in connection.execute(f"PRAGMA table_info({SIMULATIONS_TABLE})")
    ]
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
    """Whether BRAIN would accept the alpha, plus the blocking checks."""
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
    alpha_uid: str,
    expression: str,
    settings: Mapping[str, Any],
    completed: bool,
    simulated_at: str,
    duration_seconds: float | None = None,
    braint_alpha_id: str | None = None,
    braint_sim_id: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
    finished_at: str | None = None,
    simulation_body: Mapping[str, Any] | None = None,
    alpha_body: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the simulation row and its side-table row for one backtest."""
    block = _is_block(alpha_body)
    checks = check_values(alpha_body)
    main: dict[str, Any] = {
        "sim_uid": sim_uid,
        "alpha_uid": alpha_uid,
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
        "alpha_uid": alpha_uid,
        "braint_alpha_id": braint_alpha_id,
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
        f"SELECT sim_uid FROM {SIMULATIONS_TABLE} WHERE expression = ? AND settings_json = ?",
        (expression, settings_text),
    ).fetchone()
    return str(row[0]) if row else None


def write_outcome(connection: sqlite3.Connection, **kwargs: Any) -> None:
    """Insert or update one backtest (row + side row), adding check columns as needed.

    A run is identified by ``(expression, settings_json)``: re-running the same expression
    with the same settings overwrites the previous result but keeps its ``sim_uid``. The
    same factor may hold several rows with different settings.
    """
    main, details = build_rows(**kwargs)
    existing = find_sim_uid(connection, main["expression"], main["settings_json"])
    if existing:
        main["sim_uid"] = existing
        details["sim_uid"] = existing

    new_checks = [column for column in main if column.startswith(CHECK_COLUMN_PREFIX)]
    ensure_check_columns(connection, [column[len(CHECK_COLUMN_PREFIX):] for column in new_checks])

    # An overwrite replaces the whole result, so checks the new run no longer reports must
    # be cleared instead of keeping the previous verdict (e.g. a re-run that failed).
    stale_checks = [
        column for column in known_check_columns(connection) if column not in main
    ]
    if stale_checks:
        main.update(dict.fromkeys(stale_checks))

    main_columns = list(main)
    placeholders = ", ".join(f":{name}" for name in main_columns)
    updates = ", ".join(f"{name} = excluded.{name}" for name in main_columns if name != "sim_uid")
    connection.execute(
        f"INSERT INTO {SIMULATIONS_TABLE} ({', '.join(main_columns)}) VALUES ({placeholders})"
        f" ON CONFLICT (sim_uid) DO UPDATE SET {updates}",
        main,
    )
    detail_placeholders = ", ".join(f":{name}" for name in DETAIL_COLUMNS)
    detail_updates = ", ".join(
        f"{name} = excluded.{name}" for name in DETAIL_COLUMNS if name != "sim_uid"
    )
    connection.execute(
        f"INSERT INTO {DETAILS_TABLE} ({', '.join(DETAIL_COLUMNS)}) "
        f"VALUES ({detail_placeholders}) ON CONFLICT (sim_uid) DO UPDATE SET {detail_updates}",
        details,
    )
    refresh_alpha_metrics(connection, main["alpha_uid"])
    connection.commit()


# -------------------------------------------------------------------- reading


def recent(connection: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Newest backtests across all factors."""
    return list(
        connection.execute(
            "SELECT * FROM v_alpha_simulation_summary "
            "ORDER BY simulated_at DESC LIMIT ?",
            (limit,),
        )
    )


def runs_for_alpha(connection: sqlite3.Connection, alpha_uid: str) -> list[sqlite3.Row]:
    """Every backtest recorded for one factor, newest first (all columns included)."""
    return list(
        connection.execute(
            f"""
            SELECT s.*,
                   d.braint_alpha_id,
                   d.braint_sim_id,
                   d.error,
                   d.error_kind,
                   d.finished_at
            FROM {SIMULATIONS_TABLE} AS s
            LEFT JOIN {DETAILS_TABLE} AS d USING (sim_uid)
            WHERE s.alpha_uid = ?
            ORDER BY s.simulated_at DESC
            """,
            (alpha_uid,),
        )
    )


def platform_error_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Failed runs whose failure came from the platform, so the factor was never judged."""
    return list(
        connection.execute(
            f"""
            SELECT s.sim_uid, s.alpha_uid, s.expression, d.error
            FROM {SIMULATIONS_TABLE} AS s
            JOIN {DETAILS_TABLE} AS d ON d.sim_uid = s.sim_uid
            WHERE s.completed = 0
              AND d.error IS NOT NULL
              AND d.error_kind = ?
            ORDER BY s.simulated_at
            """,
            (ERROR_KIND_PLATFORM,),
        )
    )


def pending_placeholders(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows written at submit time whose final result was never stored.

    This happens when the process is stopped while the platform is still computing:
    the row keeps ``braint_sim_id`` but has no error, result or completion time.
    """
    return list(
        connection.execute(
            f"""
            SELECT s.sim_uid,
                   s.alpha_uid,
                   s.expression,
                   s.settings_json,
                   s.simulated_at,
                   d.braint_sim_id
            FROM {SIMULATIONS_TABLE} AS s
            JOIN {DETAILS_TABLE} AS d ON d.sim_uid = s.sim_uid
            WHERE s.completed = 0
              AND d.error IS NULL
              AND d.finished_at IS NULL
              AND d.braint_sim_id IS NOT NULL
            ORDER BY s.simulated_at
            """
        )
    )


def delete_simulations(connection: sqlite3.Connection, sim_uids: Sequence[str]) -> int:
    """Delete runs by uid (details cascade). Returns the number of rows removed."""
    uids = list(sim_uids)
    if not uids:
        return 0
    affected: list[str] = []
    removed = 0
    for start in range(0, len(uids), 200):
        batch = uids[start : start + 200]
        placeholders = ",".join("?" * len(batch))
        affected += [
            row["alpha_uid"]
            for row in connection.execute(
                f"SELECT DISTINCT alpha_uid FROM {SIMULATIONS_TABLE} "
                f"WHERE sim_uid IN ({placeholders})",
                batch,
            )
        ]
        removed += connection.execute(
            f"DELETE FROM {SIMULATIONS_TABLE} WHERE sim_uid IN ({placeholders})",
            batch,
        ).rowcount
    refresh_alpha_metrics(connection, affected)
    connection.commit()
    return removed


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

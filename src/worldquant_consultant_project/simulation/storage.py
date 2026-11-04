"""SQLite persistence for simulation runs, metrics and checks."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS simulations (
    sim_uid             TEXT PRIMARY KEY,
    expression          TEXT NOT NULL,
    language            TEXT NOT NULL DEFAULT 'FASTEXPR',
    instrument_type     TEXT,
    region              TEXT,
    universe            TEXT,
    delay               INTEGER,
    decay               INTEGER,
    neutralization      TEXT,
    truncation          REAL,
    pasteurization      TEXT,
    unit_handling       TEXT,
    nan_handling        TEXT,
    settings_json       TEXT NOT NULL,
    settings_hash       TEXT NOT NULL,
    braint_sim_id       TEXT,
    alpha_id            TEXT,
    status              TEXT NOT NULL,
    error               TEXT,
    submitted_at        TEXT NOT NULL,
    finished_at         TEXT,
    duration_seconds    REAL,
    raw_simulation_json TEXT,
    raw_alpha_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_simulations_alpha    ON simulations (alpha_id);
CREATE INDEX IF NOT EXISTS idx_simulations_status   ON simulations (status);
CREATE INDEX IF NOT EXISTS idx_simulations_settings ON simulations (settings_hash);
CREATE INDEX IF NOT EXISTS idx_simulations_created  ON simulations (submitted_at);

CREATE TABLE IF NOT EXISTS simulation_results (
    sim_uid            TEXT NOT NULL,
    sample             TEXT NOT NULL,
    sharpe             REAL,
    fitness            REAL,
    returns            REAL,
    turnover           REAL,
    drawdown           REAL,
    margin             REAL,
    pnl                REAL,
    book_size          REAL,
    long_count         INTEGER,
    short_count        INTEGER,
    self_correlation   REAL,
    prod_correlation   REAL,
    start_date         TEXT,
    drawdown_start_date TEXT,
    checks_passed      INTEGER,
    checks_failed      INTEGER,
    checks_pending     INTEGER,
    PRIMARY KEY (sim_uid, sample),
    FOREIGN KEY (sim_uid) REFERENCES simulations (sim_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS simulation_metrics (
    sim_uid    TEXT NOT NULL,
    sample     TEXT NOT NULL,
    metric     TEXT NOT NULL,
    value      REAL,
    value_text TEXT,
    PRIMARY KEY (sim_uid, sample, metric),
    FOREIGN KEY (sim_uid) REFERENCES simulations (sim_uid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON simulation_metrics (sample, metric);

CREATE TABLE IF NOT EXISTS simulation_checks (
    sim_uid  TEXT NOT NULL,
    sample   TEXT NOT NULL,
    name     TEXT NOT NULL,
    result   TEXT,
    value    REAL,
    "limit"  REAL,
    year     INTEGER,
    message  TEXT,
    PRIMARY KEY (sim_uid, sample, name),
    FOREIGN KEY (sim_uid) REFERENCES simulations (sim_uid) ON DELETE CASCADE
);

DROP VIEW IF EXISTS v_simulation_summary;
CREATE VIEW v_simulation_summary AS
SELECT s.sim_uid,
       s.expression,
       s.region,
       s.universe,
       s.delay,
       s.decay,
       s.neutralization,
       s.truncation,
       s.status,
       is_res.sharpe    AS is_sharpe,
       is_res.fitness   AS is_fitness,
       is_res.returns   AS is_returns,
       is_res.turnover  AS is_turnover,
       is_res.drawdown  AS is_drawdown,
       is_res.margin    AS is_margin,
       os_res.sharpe    AS os_sharpe,
       os_res.fitness   AS os_fitness,
       s.alpha_id,
       s.submitted_at
FROM simulations AS s
LEFT JOIN simulation_results AS is_res
       ON is_res.sim_uid = s.sim_uid AND is_res.sample = 'is'
LEFT JOIN simulation_results AS os_res
       ON os_res.sim_uid = s.sim_uid AND os_res.sample = 'os'
ORDER BY s.submitted_at DESC;
"""

# BRAIN sample keys in the /alphas/{id} payload.
SAMPLES: tuple[tuple[str, str], ...] = (
    ("is", "is"),
    ("os", "os"),
    ("train", "train"),
    ("test", "test"),
    ("prod", "prod"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def insert_simulation(connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO simulations (
            sim_uid, expression, language, instrument_type, region, universe, delay, decay,
            neutralization, truncation, pasteurization, unit_handling, nan_handling,
            settings_json, settings_hash, braint_sim_id, alpha_id, status, error,
            submitted_at, finished_at, duration_seconds, raw_simulation_json, raw_alpha_json
        ) VALUES (
            :sim_uid, :expression, :language, :instrument_type, :region, :universe, :delay, :decay,
            :neutralization, :truncation, :pasteurization, :unit_handling, :nan_handling,
            :settings_json, :settings_hash, :braint_sim_id, :alpha_id, :status, :error,
            :submitted_at, :finished_at, :duration_seconds, :raw_simulation_json, :raw_alpha_json
        )
        ON CONFLICT (sim_uid) DO UPDATE SET
            braint_sim_id       = excluded.braint_sim_id,
            alpha_id            = excluded.alpha_id,
            status              = excluded.status,
            error               = excluded.error,
            finished_at         = excluded.finished_at,
            duration_seconds    = excluded.duration_seconds,
            raw_simulation_json = excluded.raw_simulation_json,
            raw_alpha_json      = excluded.raw_alpha_json
        """,
        dict(row),
    )
    connection.commit()


def insert_alpha_results(
    connection: sqlite3.Connection,
    *,
    sim_uid: str,
    alpha: Mapping[str, Any],
) -> int:
    """Persist every sample block of an alpha record. Returns rows written."""
    written = 0
    for sample_name, key in SAMPLES:
        block = alpha.get(key)
        if not isinstance(block, Mapping):
            continue

        checks = [c for c in (block.get("checks") or []) if isinstance(c, dict)]
        numeric: dict[str, float] = {}
        text: dict[str, str] = {}
        for name, value in block.items():
            if name == "checks":
                continue
            if isinstance(value, bool):
                numeric[name] = float(value)
            elif isinstance(value, (int, float)):
                numeric[name] = float(value)
            elif isinstance(value, str):
                text[name] = value

        results = {c.get("result") for c in checks}
        wide = {
            "sim_uid": sim_uid,
            "sample": sample_name,
            "sharpe": numeric.get("sharpe"),
            "fitness": numeric.get("fitness"),
            "returns": numeric.get("returns"),
            "turnover": numeric.get("turnover"),
            "drawdown": numeric.get("drawdown"),
            "margin": numeric.get("margin"),
            "pnl": numeric.get("pnl"),
            "book_size": numeric.get("bookSize"),
            "long_count": _as_int(numeric.get("longCount")),
            "short_count": _as_int(numeric.get("shortCount")),
            "self_correlation": numeric.get("selfCorrelation"),
            "prod_correlation": numeric.get("prodCorrelation"),
            "start_date": text.get("startDate"),
            "drawdown_start_date": text.get("drawdownStartDate"),
            "checks_passed": sum(1 for r in results if r == "PASS"),
            "checks_failed": sum(1 for r in results if r == "FAIL"),
            "checks_pending": sum(1 for r in results if r == "PENDING"),
        }
        connection.execute(
            """
            INSERT INTO simulation_results (
                sim_uid, sample, sharpe, fitness, returns, turnover, drawdown, margin, pnl,
                book_size, long_count, short_count, self_correlation, prod_correlation,
                start_date, drawdown_start_date, checks_passed, checks_failed, checks_pending
            ) VALUES (
                :sim_uid, :sample, :sharpe, :fitness, :returns, :turnover, :drawdown, :margin, :pnl,
                :book_size, :long_count, :short_count, :self_correlation, :prod_correlation,
                :start_date, :drawdown_start_date, :checks_passed, :checks_failed, :checks_pending
            )
            ON CONFLICT (sim_uid, sample) DO UPDATE SET
                sharpe = excluded.sharpe, fitness = excluded.fitness, returns = excluded.returns,
                turnover = excluded.turnover, drawdown = excluded.drawdown, margin = excluded.margin,
                pnl = excluded.pnl, book_size = excluded.book_size,
                long_count = excluded.long_count, short_count = excluded.short_count,
                self_correlation = excluded.self_correlation,
                prod_correlation = excluded.prod_correlation,
                start_date = excluded.start_date,
                drawdown_start_date = excluded.drawdown_start_date,
                checks_passed = excluded.checks_passed, checks_failed = excluded.checks_failed,
                checks_pending = excluded.checks_pending
            """,
            wide,
        )
        written += 1

        connection.executemany(
            """
            INSERT INTO simulation_metrics (sim_uid, sample, metric, value, value_text)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (sim_uid, sample, metric) DO UPDATE SET
                value = excluded.value, value_text = excluded.value_text
            """,
            [(sim_uid, sample_name, name, value, None) for name, value in numeric.items()]
            + [(sim_uid, sample_name, name, None, value) for name, value in text.items()],
        )

        connection.executemany(
            """
            INSERT INTO simulation_checks (sim_uid, sample, name, result, value, "limit", year, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (sim_uid, sample, name) DO UPDATE SET
                result = excluded.result, value = excluded.value, "limit" = excluded."limit",
                year = excluded.year, message = excluded.message
            """,
            [
                (
                    sim_uid,
                    sample_name,
                    str(check.get("name")),
                    check.get("result"),
                    _as_float(check.get("value")),
                    _as_float(check.get("limit")),
                    _as_int(check.get("year")),
                    check.get("message"),
                )
                for check in checks
            ],
        )

    connection.commit()
    return written


def dump_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def write_outcome(
    connection: sqlite3.Connection,
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
) -> None:
    """Upsert the simulation row, then persist metrics when an alpha record exists."""
    insert_simulation(
        connection,
        {
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
            "error": error,
            "submitted_at": submitted_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
            "raw_simulation_json": dump_json(simulation_body),
            "raw_alpha_json": dump_json(alpha_body),
        },
    )
    if alpha_body:
        insert_alpha_results(connection, sim_uid=sim_uid, alpha=alpha_body)


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


def recent(connection: sqlite3.Connection, limit: int = 10) -> Iterable[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM v_simulation_summary LIMIT ?", (limit,)
    ).fetchall()

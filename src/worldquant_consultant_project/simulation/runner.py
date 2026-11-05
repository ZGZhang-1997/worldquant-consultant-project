"""Orchestration for one backtest: submit, poll, fetch alpha and persist."""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..acquisition.config import DEFAULT_DATA_DIR, load_credentials
from . import storage
from .client import BrainSimulationClient, BrainSimulationError
from .settings import SimulationSettings

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "simulations.db"


@dataclass
class SimulationOutcome:
    """Result of one backtest, mirroring the row stored in ``simulations``."""

    sim_uid: str
    expression: str
    status: str
    alpha_id: str | None = None
    braint_sim_id: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    submittable: int | None = None
    submit_blockers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def _extract_metrics(alpha: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for sample in ("is",) + storage.OTHER_SAMPLES:
        block = alpha.get(sample)
        if isinstance(block, dict):
            metrics[sample] = {
                name: value
                for name, value in block.items()
                if name != "checks" and isinstance(value, (int, float, str))
            }
    return metrics


def run_simulation(
    expression: str,
    settings: SimulationSettings | dict[str, Any] | None = None,
    *,
    db_path: Path | str | None = None,
    timeout: float = 900.0,
    credentials_path: Path | str | None = None,
    client: BrainSimulationClient | None = None,
    initialize_db: bool = True,
) -> SimulationOutcome:
    """Backtest one Fast Expression factor and store the run in SQLite.

    Failures (invalid expression, timeout, platform error) are recorded as
    ``completed=0`` rows and returned instead of raised, so callers get a
    complete audit trail of every submitted backtest.

    ``initialize_db=False`` skips schema migration on open; batch workers use it
    after the runner has initialised the database once.
    """
    if not expression or not expression.strip():
        raise ValueError("expression must be a non-empty Fast Expression string")
    expression = expression.strip()
    resolved = (
        settings
        if isinstance(settings, SimulationSettings)
        else SimulationSettings.from_dict(settings)
    )

    sim_uid = uuid.uuid4().hex
    simulated_at = storage.utc_now()
    started = time.monotonic()

    connection = storage.connect(db_path or DEFAULT_DB_PATH, initialize=initialize_db)
    owned_client = client is None
    if client is None:
        client = BrainSimulationClient(load_credentials(credentials_path), poll_timeout=timeout)

    outcome = SimulationOutcome(
        sim_uid=sim_uid,
        expression=expression,
        status="failed",
        settings=resolved.stored,
    )
    location: str | None = None
    simulation_body: dict[str, Any] | None = None
    alpha_body: dict[str, Any] | None = None

    try:
        location = client.submit(expression, resolved.payload)
        outcome.braint_sim_id = location.rstrip("/").rsplit("/", 1)[-1]
        storage.write_outcome(
            connection,
            sim_uid=sim_uid,
            expression=expression,
            settings=resolved.stored,
            completed=False,
            simulated_at=simulated_at,
            braint_sim_id=outcome.braint_sim_id,
        )
        simulation_body = client.poll(location, timeout=timeout)
        outcome.braint_sim_id = str(simulation_body.get("id") or outcome.braint_sim_id)
        alpha_id = simulation_body.get("alpha")
        if not alpha_id:
            raise BrainSimulationError("simulation finished without an alpha id")

        outcome.alpha_id = str(alpha_id)
        alpha_body = client.get_alpha(outcome.alpha_id)
        outcome.metrics = _extract_metrics(alpha_body)
        submittable, blockers = storage.derive_submittable(alpha_body)
        outcome.submittable = submittable
        outcome.submit_blockers = blockers
        outcome.status = "completed"
    except BrainSimulationError as exc:
        outcome.error = getattr(exc, "detail", None) or str(exc)
        logger.error("simulation failed for %r: %s", expression[:60], outcome.error)
    except Exception as exc:  # noqa: BLE001 - always keep an audit trail
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.exception("unexpected error during simulation")
    finally:
        if owned_client and client is not None:
            client.close()

        outcome.duration_seconds = round(time.monotonic() - started, 3)
        storage.write_outcome(
            connection,
            sim_uid=sim_uid,
            expression=expression,
            settings=resolved.stored,
            completed=outcome.ok,
            simulated_at=simulated_at,
            duration_seconds=outcome.duration_seconds,
            braint_sim_id=outcome.braint_sim_id,
            alpha_id=outcome.alpha_id,
            error=outcome.error,
            finished_at=storage.utc_now() if outcome.ok else None,
            simulation_body=simulation_body,
            alpha_body=alpha_body,
        )
        connection.close()

    if outcome.ok:
        logger.info(
            "simulation %s completed in %.1fs (alpha %s)",
            sim_uid,
            outcome.duration_seconds or 0.0,
            outcome.alpha_id,
        )
    return outcome


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the simulations database without running anything."""
    return storage.connect(db_path or DEFAULT_DB_PATH)

"""Simulation settings: defaults, validation, config-file loading and API payload rendering."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = Path(__file__).with_name("batch_simulation_settings.json")
#: Placeholder for the optimisation settings; filled in when that step is implemented.
OPTIMIZATION_SETTINGS_PATH = Path(__file__).with_name("optimization_simulation_settings.json")
DEFAULT_CONCURRENCY_PATH = Path(__file__).with_name("maximum_concurrent_number")

# BRAIN's usual defaults for a single REGULAR fast-expression simulation.
DEFAULT_SETTINGS: dict[str, Any] = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 0,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": False,
}

# BRAIN sends camelCase keys; the dataclass uses snake_case.
CAMEL_TO_FIELD = {
    "instrumentType": "instrument_type",
    "unitHandling": "unit_handling",
    "nanHandling": "nan_handling",
}

SUPPORTED_SIMULATION_TYPE = "REGULAR"


class SettingsFileError(RuntimeError):
    """The settings or concurrency file is missing or malformed."""


@dataclass
class SimulationSettings:
    """Backtest parameters; every field mirrors a BRAIN ``settings`` key."""

    instrument_type: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    pasteurization: str = "ON"
    unit_handling: str = "VERIFY"
    nan_handling: str = "OFF"
    language: str = "FASTEXPR"
    visualization: bool = False
    # Any additional BRAIN settings key (e.g. testPeriod, maxTrade) passed through verbatim.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument_type:
            raise ValueError("instrument_type must not be empty")
        if not self.region:
            raise ValueError("region must not be empty")
        if not self.universe:
            raise ValueError("universe must not be empty")
        if self.delay not in (0, 1):
            raise ValueError("delay must be 0 or 1")
        if self.decay < 0:
            raise ValueError("decay must be >= 0")
        if not 0 <= self.truncation <= 1:
            raise ValueError("truncation must be within [0, 1]")

    @property
    def payload(self) -> dict[str, Any]:
        """Settings block sent to ``POST /simulations``."""
        settings = dict(DEFAULT_SETTINGS)
        settings.update(
            {
                "instrumentType": self.instrument_type,
                "region": self.region,
                "universe": self.universe,
                "delay": self.delay,
                "decay": self.decay,
                "neutralization": self.neutralization,
                "truncation": self.truncation,
                "pasteurization": self.pasteurization,
                "unitHandling": self.unit_handling,
                "nanHandling": self.nan_handling,
                "language": self.language,
                "visualization": self.visualization,
            }
        )
        settings.update(self.extra)
        return settings

    @property
    def stored(self) -> dict[str, Any]:
        """Settings as persisted in the database (snake_case + extras)."""
        data = {
            key: value
            for key, value in asdict(self).items()
            if key != "extra"
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> "SimulationSettings":
        if not values:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in values.items() if k in known and k != "extra"}
        extras = {k: v for k, v in values.items() if k not in known}
        extra_field = values.get("extra") or {}
        kwargs["extra"] = {**extras, **extra_field}
        return cls(**kwargs)


def load_settings_file(path: Path | str | None = None) -> tuple[str, SimulationSettings]:
    """Read ``batch_simulation_settings.json`` and return ``(simulation_type, settings)``."""
    settings_path = Path(path) if path else DEFAULT_SETTINGS_PATH
    if not settings_path.is_file():
        raise SettingsFileError(f"settings file not found: {settings_path}")
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsFileError(f"cannot read settings file {settings_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SettingsFileError(f"{settings_path} must contain a JSON object")

    sim_type = str(payload.get("type") or SUPPORTED_SIMULATION_TYPE).upper()
    if sim_type != SUPPORTED_SIMULATION_TYPE:
        raise SettingsFileError(
            f"only {SUPPORTED_SIMULATION_TYPE} simulations are supported, got '{sim_type}'"
        )
    block = payload.get("settings")
    if not isinstance(block, dict) or not block:
        raise SettingsFileError(f"{settings_path} must contain a non-empty 'settings' object")

    values = {CAMEL_TO_FIELD.get(key, key): value for key, value in block.items()}
    try:
        return sim_type, SimulationSettings.from_dict(values)
    except ValueError as exc:
        raise SettingsFileError(f"invalid settings in {settings_path}: {exc}") from exc


def load_max_concurrency(path: Path | str | None = None) -> int:
    """Read the platform's simultaneous-simulation allowance."""
    concurrency_path = Path(path) if path else DEFAULT_CONCURRENCY_PATH
    if not concurrency_path.is_file():
        raise SettingsFileError(f"concurrency file not found: {concurrency_path}")
    try:
        raw = concurrency_path.read_text(encoding="utf-8").strip()
        value = int(raw.split()[0]) if raw else 0
    except (OSError, ValueError, IndexError) as exc:
        raise SettingsFileError(f"cannot parse {concurrency_path}: {exc}") from exc
    if value < 1:
        raise SettingsFileError(f"{concurrency_path} must contain a positive integer, got {raw!r}")
    return value

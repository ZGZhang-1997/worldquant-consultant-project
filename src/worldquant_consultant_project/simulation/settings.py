"""Simulation settings: defaults, validation and API payload rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

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

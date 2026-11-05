"""Discover which label providers can be used to filter fields."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from ..labeling.config import PROVIDERS, ProviderSpec, get_provider

logger = logging.getLogger(__name__)

UNION = "union"
INTERSECTION = "intersection"


class LabelSourceError(RuntimeError):
    """The requested label source is unknown or has no usable data."""


@dataclass(frozen=True)
class LabelSource:
    mode: str
    providers: tuple[ProviderSpec, ...]

    @property
    def name(self) -> str:
        joined = ",".join(spec.name for spec in self.providers)
        return f"{self.mode}:{joined}"


def available_providers(connection: sqlite3.Connection) -> list[ProviderSpec]:
    """Registered providers whose label columns actually exist in ``data_fields``.

    Reading from the shared registry keeps new providers working without touching
    this module: register one in ``labeling.config`` and run its labeling pass.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(data_fields)")}
    usable = [spec for spec in PROVIDERS.values() if set(spec.columns.values()) <= columns]
    disabled = sorted(set(PROVIDERS) - {spec.name for spec in usable})
    if disabled:
        logger.info("providers without label columns skipped: %s", ", ".join(disabled))
    return usable


def resolve_label_source(connection: sqlite3.Connection, requested: str | None) -> LabelSource:
    """Turn ``union`` / ``intersection`` / a provider name into a concrete source."""
    usable = available_providers(connection)
    if not usable:
        raise LabelSourceError(
            "no labelled provider found; run the labeling module for at least one model first"
        )
    names = ", ".join(spec.name for spec in usable)

    mode = (requested or UNION).strip().lower()
    if mode == UNION:
        return LabelSource(UNION, tuple(usable))
    if mode == INTERSECTION:
        if len(usable) < 2:
            raise LabelSourceError(f"intersection needs at least two labelled providers (have {names})")
        return LabelSource(INTERSECTION, tuple(usable))
    if mode in PROVIDERS:
        spec = get_provider(mode)
        if spec not in usable:
            raise LabelSourceError(f"provider '{mode}' has no label columns; available: {names}")
        return LabelSource(mode, (spec,))
    raise LabelSourceError(
        f"unknown label source '{requested}'; use union, intersection or one of: {names}"
    )

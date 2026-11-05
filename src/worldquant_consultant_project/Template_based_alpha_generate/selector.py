"""Select the data fields that satisfy one slot rule."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..labeling.config import LABEL_DIMENSIONS, ProviderSpec
from .providers import INTERSECTION, LabelSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldCandidate:
    dataset_id: str
    field_id: str
    description: str | None
    field_type: str | None
    matched_providers: tuple[str, ...]
    labels: dict[str, dict[str, str]]

    @property
    def labels_json(self) -> str:
        return json.dumps(self.labels, ensure_ascii=False, sort_keys=True)


def _field_query(spec: ProviderSpec, rule: dict[str, list[str]]) -> tuple[str, list[Any]]:
    select = ", ".join(f"{spec.columns[dim]} AS {dim}" for dim in LABEL_DIMENSIONS)
    conditions = []
    params: list[Any] = []
    for dimension, values in rule.items():
        conditions.append(f"{spec.columns[dimension]} IN ({','.join('?' * len(values))})")
        params.extend(values)
    # Labels are per field, identical across universes, so DISTINCT collapses the 6 copies.
    sql = (
        f"SELECT DISTINCT dataset_id, field_id, description, type, {select} "
        f"FROM data_fields WHERE {' AND '.join(conditions)}"
    )
    return sql, params


def _rows_for(connection: sqlite3.Connection, spec: ProviderSpec, rule: dict[str, list[str]]):
    sql, params = _field_query(spec, rule)
    return list(connection.execute(sql, params))


def select_fields(
    connection: sqlite3.Connection,
    rule: dict[str, list[str]],
    source: LabelSource,
) -> list[FieldCandidate]:
    """Return every field matching ``rule`` under the configured label source."""
    per_provider: dict[str, dict[str, Any]] = {}
    for spec in source.providers:
        rows = _rows_for(connection, spec, rule)
        keyed = {
            (row["dataset_id"], row["field_id"]): {
                "row": row,
                "labels": {dim: row[dim] for dim in LABEL_DIMENSIONS},
            }
            for row in rows
        }
        logger.debug("%s: %d fields match slot rule %s", spec.name, len(keyed), rule)
        per_provider[spec.name] = keyed

    if not per_provider:
        return []

    if source.mode == INTERSECTION:
        common = set.intersection(*(set(keys) for keys in per_provider.values()))
        keys = sorted(common)
    else:
        keys = sorted(set().union(*(set(keys) for keys in per_provider.values())))

    candidates: list[FieldCandidate] = []
    for key in keys:
        matched = [name for name, entries in per_provider.items() if key in entries]
        first = per_provider[matched[0]][key]["row"]
        candidates.append(
            FieldCandidate(
                dataset_id=key[0],
                field_id=key[1],
                description=first["description"],
                field_type=first["type"],
                matched_providers=tuple(matched),
                labels={name: per_provider[name][key]["labels"] for name in matched},
            )
        )
    return candidates

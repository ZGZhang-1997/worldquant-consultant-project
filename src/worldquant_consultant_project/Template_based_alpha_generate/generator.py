"""Cartesian slot filling: turn candidate fields into concrete expressions."""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterator

from .providers import LabelSource
from .selector import FieldCandidate, select_fields
from .templates import TemplateSpec

logger = logging.getLogger(__name__)

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GenerationError(RuntimeError):
    """A slot had no candidates, or a rendered expression is invalid."""


@dataclass
class GeneratedAlpha:
    expression: str
    expression_hash: str
    template: TemplateSpec
    slots: list[tuple[str, FieldCandidate]] = field(default_factory=list)

    @property
    def slot_fill(self) -> dict[str, str]:
        return {name: candidate.field_id for name, candidate in self.slots}


@dataclass
class TemplateCandidates:
    template: TemplateSpec
    slots: dict[str, list[FieldCandidate]]

    @property
    def combination_count(self) -> int:
        total = 1
        for candidates in self.slots.values():
            total *= len(candidates)
        return total


def collect_candidates(
    connection: sqlite3.Connection, template: TemplateSpec, source: LabelSource
) -> TemplateCandidates:
    slots = {
        name: select_fields(connection, rule, source) for name, rule in template.slots.items()
    }
    empty = [name for name, candidates in slots.items() if not candidates]
    if empty:
        raise GenerationError(
            f"{template.template_id}: no fields match slot(s) {empty} under {source.name}"
        )
    return TemplateCandidates(template=template, slots=slots)


def expression_hash(expression: str) -> str:
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()


def iter_combinations(candidates: TemplateCandidates) -> Iterator[GeneratedAlpha]:
    template = candidates.template
    # Follow the formula order so slot_index matches reading order.
    order = template.slot_order
    pools = [candidates.slots[name] for name in order]
    for combo in itertools.product(*pools):
        fill = {name: candidate.field_id for name, candidate in zip(order, combo)}
        for value in fill.values():
            if not IDENTIFIER.match(value):
                raise GenerationError(f"invalid field identifier: {value!r}")
        expression = template.render(fill)
        if "{" in expression or "}" in expression:
            raise GenerationError(
                f"{template.template_id}: unresolved placeholder in {expression!r}"
            )
        yield GeneratedAlpha(
            expression=expression,
            expression_hash=expression_hash(expression),
            template=template,
            slots=list(zip(order, combo)),
        )


def generate_alphas(
    connection: sqlite3.Connection,
    templates: list[TemplateSpec],
    source: LabelSource,
    *,
    limit_per_template: int | None = None,
    seen: set[str] | None = None,
) -> tuple[list[GeneratedAlpha], dict[str, TemplateCandidates], int]:
    """Generate expressions for every template.

    Returns the accepted alphas, the candidate pools and the number of duplicates skipped.
    ``seen`` lets callers dedupe across repeated calls (it is updated in place).
    """
    hashes = seen if seen is not None else set()
    generated: list[GeneratedAlpha] = []
    pools: dict[str, TemplateCandidates] = {}
    duplicates = 0

    for template in templates:
        candidates = collect_candidates(connection, template, source)
        pools[template.template_id] = candidates
        logger.info(
            "%s: %s -> %d combinations",
            template.template_id,
            " ".join(f"{name}={len(items)}" for name, items in candidates.slots.items()),
            candidates.combination_count,
        )

        accepted = 0
        for alpha in iter_combinations(candidates):
            if alpha.expression_hash in hashes:
                duplicates += 1
                continue
            hashes.add(alpha.expression_hash)
            generated.append(alpha)
            accepted += 1
            if limit_per_template is not None and accepted >= limit_per_template:
                break
    return generated, pools, duplicates

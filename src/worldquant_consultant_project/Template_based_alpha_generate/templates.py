"""Loading and validation of the template definitions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..labeling.config import LABEL_DIMENSIONS

DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("Template.json")

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TemplateError(RuntimeError):
    """A template definition is malformed."""


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    template_name: str
    description: str
    slot_count: int
    formula_pattern: str
    slots: dict[str, dict[str, list[str]]]

    @property
    def slot_order(self) -> list[str]:
        """Slot names in the order they first appear in the formula."""
        seen: list[str] = []
        for name in PLACEHOLDER.findall(self.formula_pattern):
            if name not in seen:
                seen.append(name)
        return seen

    @property
    def slot_names(self) -> list[str]:
        """Slot names ordered longest-first, so Slot_A never eats Slot_Abc when rendering."""
        return sorted(self.slot_order, key=len, reverse=True)

    def render(self, fill: dict[str, str]) -> str:
        expression = self.formula_pattern
        for name in self.slot_names:
            expression = expression.replace("{" + name + "}", fill[name])
        return expression


def _validate_rule(template_id: str, slot: str, rule: Any) -> dict[str, list[str]]:
    if not isinstance(rule, dict) or not rule:
        raise TemplateError(f"{template_id}: slot '{slot}' must map dimensions to value lists")
    cleaned: dict[str, list[str]] = {}
    for dimension, values in rule.items():
        if dimension not in LABEL_DIMENSIONS:
            raise TemplateError(
                f"{template_id}: slot '{slot}' uses unknown dimension '{dimension}' "
                f"(allowed: {', '.join(LABEL_DIMENSIONS)})"
            )
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values:
            raise TemplateError(f"{template_id}: slot '{slot}' dimension '{dimension}' needs a list")
        allowed = LABEL_DIMENSIONS[dimension]
        invalid = [value for value in values if value not in allowed]
        if invalid:
            raise TemplateError(
                f"{template_id}: slot '{slot}' dimension '{dimension}' has invalid values "
                f"{invalid} (allowed: {', '.join(allowed)})"
            )
        cleaned[dimension] = list(values)
    return cleaned


def _parse_template(raw: Any, index: int) -> TemplateSpec:
    if not isinstance(raw, dict):
        raise TemplateError(f"template #{index} is not an object")
    template_id = str(raw.get("template_id") or f"template_{index}")
    formula = raw.get("formula_pattern")
    if not isinstance(formula, str) or not formula.strip():
        raise TemplateError(f"{template_id}: formula_pattern is missing")
    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, dict) or not slots_raw:
        raise TemplateError(f"{template_id}: slots is missing")

    placeholders = PLACEHOLDER.findall(formula)
    # A slot may legitimately appear twice in one formula (e.g. ts_delay({Slot}, 252)).
    unique_placeholders = list(dict.fromkeys(placeholders))
    missing = sorted(set(unique_placeholders) - set(slots_raw))
    extra = sorted(set(slots_raw) - set(unique_placeholders))
    if missing:
        raise TemplateError(f"{template_id}: formula placeholders without a rule: {missing}")
    if extra:
        raise TemplateError(f"{template_id}: slots never used by the formula: {extra}")

    slot_count = raw.get("slot_count", len(unique_placeholders))
    if slot_count != len(unique_placeholders):
        raise TemplateError(
            f"{template_id}: slot_count={slot_count} but formula has "
            f"{len(unique_placeholders)} distinct placeholders"
        )

    slots = {name: _validate_rule(template_id, name, rule) for name, rule in slots_raw.items()}
    return TemplateSpec(
        template_id=template_id,
        template_name=str(raw.get("template_name") or template_id),
        description=str(raw.get("description") or ""),
        slot_count=slot_count,
        formula_pattern=formula,
        slots=slots,
    )


def load_templates(path: Path | str | None = None) -> list[TemplateSpec]:
    template_path = Path(path) if path else DEFAULT_TEMPLATE_PATH
    if not template_path.is_file():
        raise TemplateError(f"template file not found: {template_path}")
    try:
        payload = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateError(f"cannot read template file {template_path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise TemplateError(f"{template_path} must contain a non-empty list of templates")

    templates = [_parse_template(raw, index) for index, raw in enumerate(payload)]
    ids = [template.template_id for template in templates]
    duplicates = {tid for tid in ids if ids.count(tid) > 1}
    if duplicates:
        raise TemplateError(f"duplicate template ids: {sorted(duplicates)}")
    return templates


def select_templates(
    templates: list[TemplateSpec], wanted: list[str] | None
) -> list[TemplateSpec]:
    if not wanted:
        return templates
    by_id = {template.template_id: template for template in templates}
    missing = [name for name in wanted if name not in by_id]
    if missing:
        raise TemplateError(
            f"unknown template id(s) {missing}; available: {', '.join(sorted(by_id))}"
        )
    return [by_id[name] for name in wanted]

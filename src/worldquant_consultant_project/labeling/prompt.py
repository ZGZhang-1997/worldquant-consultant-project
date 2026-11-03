"""User prompt construction (mirrors the input format shown in system_prompt.txt)."""

from __future__ import annotations

import json
from typing import Any

from .config import LABEL_DIMENSIONS, FieldInput

FIELD_ORDER = (
    "field_id",
    "field_description",
    "dataset_name",
    "category_name",
    "subcategory_name",
    "type",
    "dateCoverage",
    "coverage",
)


def _render(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        # Avoid trailing zeros so 1.0 renders like the reference sample.
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value).strip()
    return text or "null"


def build_user_prompt(field: FieldInput) -> str:
    """Render the exact ``key: value;`` block the model was designed for."""
    values = {
        "field_id": field.field_id,
        "field_description": field.description,
        "dataset_name": field.dataset_name,
        "category_name": field.category_name,
        "subcategory_name": field.subcategory_name,
        "type": field.field_type,
        "dateCoverage": field.date_coverage,
        "coverage": field.coverage,
    }
    return "".join(f"{key}: {_render(values[key])};\n" for key in FIELD_ORDER)


def build_retry_prompt(field: FieldInput, previous: str, problem: str) -> str:
    """Re-ask with the validation error spelled out."""
    keys = json.dumps(list(LABEL_DIMENSIONS))
    return (
        f"{build_user_prompt(field)}\n"
        f"上一次输出不符合要求：{problem}\n"
        f"上一次输出为：{previous}\n"
        f"请只输出一个 JSON 对象，键必须且只能是 {keys}，"
        "值必须严格取自系统提示词中的枚举选项，不得自造或组合。"
    )

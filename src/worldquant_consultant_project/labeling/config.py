"""Configuration for the field labeling stage."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_PROVIDER = "deepseek"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_CREDENTIALS_PATH = _PROJECT_ROOT / "credentials.json"
DEFAULT_PROMPT_PATH = _PROJECT_ROOT / "system_prompt.txt"
DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "brain.db"
DEFAULT_LOG_DIR = DEFAULT_DATA_DIR / "logs"

REQUEST_TIMEOUT = 120.0
MAX_TOKENS = 200
# Gemini thinking parts (skipped when parsing) still consume this budget, so leave headroom.
GEMINI_MAX_OUTPUT_TOKENS = 1024

# Dimension name -> allowed values. Values mirror the options defined in system_prompt.txt.
LABEL_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "category": (
        "price_volume",
        "fundamental",
        "analyst",
        "sentiment_news",
        "ownership_trading",
        "alternative_other",
    ),
    "math_dimension": (
        "currency_stock",
        "currency_flow",
        "price_level",
        "volume_activity",
        "ratio_relative",
        "score_bounded",
        "dimensionless_other",
    ),
    "polarity": ("positive", "negative", "neutral_state"),
    "domain_support": (
        "strictly_positive",
        "non_negative",
        "real_unbounded",
        "bounded_interval",
        "boolean_binary",
    ),
    "frequency": ("daily_high", "quarterly_fnd", "event_irregular"),
}

COLUMN_PREFIX = "deepseek_"

# Backwards-compatible alias for the DeepSeek columns; prefer ProviderSpec.columns.
LABEL_COLUMNS: dict[str, str] = {name: f"{COLUMN_PREFIX}{name}" for name in LABEL_DIMENSIONS}


class LabelingConfigError(RuntimeError):
    """Raised when credentials or the system prompt cannot be loaded."""


@dataclass(frozen=True)
class ProviderSpec:
    """Everything that differs between labeling backends."""

    name: str
    column_prefix: str
    audit_table: str
    view_name: str
    default_model: str
    credential_key: str
    env_var: str

    @property
    def columns(self) -> dict[str, str]:
        return {dimension: f"{self.column_prefix}{dimension}" for dimension in LABEL_DIMENSIONS}


PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            name="deepseek",
            column_prefix="deepseek_",
            audit_table="deepseek_labels",
            view_name="v_deepseek_field_labels",
            default_model="deepseek-flash",
            credential_key="deepseek_api_key",
            env_var="DEEPSEEK_API_KEY",
        ),
        ProviderSpec(
            name="gemini",
            column_prefix="gemini_",
            audit_table="gemini_labels",
            view_name="v_gemini_field_labels",
            default_model="gemini-3.8-flash",
            credential_key="gemini_api_key",
            env_var="GEMINI_API_KEY",
        ),
    )
}


def get_provider(name: str) -> ProviderSpec:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise LabelingConfigError(
            f"unknown provider '{name}', available: {', '.join(sorted(PROVIDERS))}"
        ) from None


def load_api_key(
    path: Path | str | None = None, provider: str = DEFAULT_PROVIDER
) -> str:
    spec = get_provider(provider)
    env_key = os.environ.get(spec.env_var)
    if env_key:
        return env_key

    cred_path = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    if not cred_path.is_file():
        raise LabelingConfigError(
            f"credentials file not found: {cred_path} (or set {spec.env_var})"
        )
    try:
        payload = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LabelingConfigError(f"cannot read credentials file {cred_path}: {exc}") from exc

    key = payload.get(spec.credential_key)
    if not key:
        raise LabelingConfigError(
            f"credentials file {cred_path} has no '{spec.credential_key}'"
        )
    return str(key)


def load_system_prompt(path: Path | str | None = None) -> str:
    prompt_path = Path(path) if path else DEFAULT_PROMPT_PATH
    if not prompt_path.is_file():
        raise LabelingConfigError(f"system prompt file not found: {prompt_path}")
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabelingConfigError(f"cannot read system prompt {prompt_path}: {exc}") from exc
    if not text.strip():
        raise LabelingConfigError(f"system prompt {prompt_path} is empty")
    return text


def prompt_fingerprint(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FieldInput:
    """The payload sent to the model for one field."""

    dataset_id: str
    field_id: str
    description: str | None
    dataset_name: str | None
    category_name: str | None
    subcategory_name: str | None
    field_type: str | None
    date_coverage: float | None
    coverage: float | None

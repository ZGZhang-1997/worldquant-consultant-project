"""Configuration, paths and credential loading for the acquisition stage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://api.worldquantbrain.com"

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_CREDENTIALS_PATH = _PROJECT_ROOT / "credentials.json"
DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "brain.db"
DEFAULT_LOG_DIR = DEFAULT_DATA_DIR / "logs"

DEFAULT_INSTRUMENT_TYPE = "EQUITY"
DEFAULT_REGION = "USA"
DEFAULT_DELAY = 1

# Candidates are probed one by one; universes the API rejects are recorded as invalid.
UNIVERSE_CANDIDATES: tuple[str, ...] = (
    "TOP3000",
    "TOP2000",
    "TOP1000",
    "TOP500",
    "TOP200",
    "TOPSP500",
    "MINVOL1M",
    "ILLIQUID_MINVOL1M",
)

PAGE_SIZE = 50
REQUEST_TIMEOUT = 60.0


def mask(value: str) -> str:
    """Return a value safe to print in logs (never the full secret)."""
    if not value:
        return "<empty>"
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:  # defensive: keeps secrets out of logs and tracebacks
        return f"Credentials(email={mask(self.email)}, password=<redacted>)"


class CredentialsError(RuntimeError):
    """Raised when BRAIN credentials are missing or unreadable."""


def load_credentials(path: Path | str | None = None) -> Credentials:
    """Load BRAIN credentials from the environment or a JSON file.

    Environment variables ``BRAIN_EMAIL`` / ``BRAIN_PASSWORD`` take precedence
    when both are set.
    """
    env_email = os.environ.get("BRAIN_EMAIL")
    env_password = os.environ.get("BRAIN_PASSWORD")
    if env_email and env_password:
        return Credentials(email=env_email, password=env_password)

    cred_path = Path(path) if path else DEFAULT_CREDENTIALS_PATH
    if not cred_path.is_file():
        raise CredentialsError(
            f"credentials file not found: {cred_path} "
            "(or set both BRAIN_EMAIL and BRAIN_PASSWORD)"
        )
    try:
        payload = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"cannot read credentials file {cred_path}: {exc}") from exc

    email = payload.get("email")
    password = payload.get("password")
    if not email or not password:
        raise CredentialsError(f"credentials file {cred_path} must contain 'email' and 'password'")
    return Credentials(email=str(email), password=str(password))

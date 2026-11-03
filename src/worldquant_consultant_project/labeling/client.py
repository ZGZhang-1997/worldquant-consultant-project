"""Minimal DeepSeek chat-completions client with retry and usage reporting."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from .config import DEEPSEEK_BASE_URL, MAX_TOKENS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    """A completion request failed after exhausting retries."""


@dataclass(frozen=True)
class Completion:
    content: str
    raw: dict[str, Any]
    prompt_tokens: int | None
    completion_tokens: int | None


class DeepSeekClient:
    """Thin wrapper over ``POST /chat/completions``."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = 5,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "DeepSeekClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        thinking: bool = False,
        temperature: float = 0.0,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": MAX_TOKENS,
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if not thinking:
            # temperature is ignored (not rejected) while thinking mode is on.
            payload["temperature"] = temperature

        attempts = 0
        while True:
            try:
                response = self._session.post(
                    f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                attempts += 1
                if attempts > self.max_retries:
                    raise DeepSeekError(f"request failed after {attempts} attempts: {exc}") from exc
                self._sleep_backoff(attempts, "connection error")
                continue

            if response.status_code == 200:
                return self._parse(response)

            if response.status_code == 429 or 500 <= response.status_code < 600:
                attempts += 1
                if attempts > self.max_retries:
                    raise DeepSeekError(
                        f"HTTP {response.status_code} after {attempts} attempts: {_snippet(response)}"
                    )
                delay = self._retry_after(response)
                logger.warning(
                    "HTTP %s from DeepSeek, retrying in %.1fs (%d/%d)",
                    response.status_code,
                    delay,
                    attempts,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            raise DeepSeekError(f"HTTP {response.status_code}: {_snippet(response)}")

    # --------------------------------------------------------------- helpers

    def _parse(self, response: requests.Response) -> Completion:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError(f"unexpected response shape: {_snippet(response)}") from exc
        usage: Mapping[str, Any] = body.get("usage") or {}
        return Completion(
            content=str(content or ""),
            raw=body,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def _retry_after(self, response: requests.Response) -> float:
        raw = response.headers.get("Retry-After")
        try:
            delay = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            delay = 0.0
        return min(max(delay, 0.5), 60.0)

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        delay = min(2.0**attempt, 30.0)
        logger.warning("%s; retrying in %.1fs (%d/%d)", reason, delay, attempt, self.max_retries)
        time.sleep(delay)


def _snippet(response: requests.Response, limit: int = 300) -> str:
    text = (response.text or "").strip().replace("\n", " ")
    return text[:limit]

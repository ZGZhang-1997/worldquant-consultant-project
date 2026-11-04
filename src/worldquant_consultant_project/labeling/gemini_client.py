"""Gemini (Google Generative Language API) client with the same interface as DeepSeek."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .client import Completion
from .config import GEMINI_BASE_URL, GEMINI_MAX_OUTPUT_TOKENS, LABEL_DIMENSIONS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        dimension: {"type": "STRING", "enum": list(values)}
        for dimension, values in LABEL_DIMENSIONS.items()
    },
    "required": list(LABEL_DIMENSIONS),
    "propertyOrdering": list(LABEL_DIMENSIONS),
}


class GeminiError(RuntimeError):
    """A Gemini request failed after exhausting retries."""


class GeminiClient:
    """Thin wrapper over ``models/{model}:generateContent``."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GEMINI_BASE_URL,
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
            {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        )
        # Dropped automatically when the model rejects thinkingConfig.
        self._send_thinking_config = True

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "GeminiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_models(self) -> list[str]:
        try:
            response = self._session.get(
                f"{self.base_url}/v1beta/models", params={"pageSize": 200}, timeout=self.timeout
            )
            response.raise_for_status()
            return [m["name"].split("/")[-1] for m in response.json().get("models", [])]
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.debug("cannot list Gemini models: %s", exc)
            return []

    def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        thinking: bool = False,
        temperature: float = 0.0,
    ) -> Completion:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        }
        if not thinking and self._send_thinking_config:
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}

        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }

        attempts = 0
        while True:
            try:
                response = self._session.post(
                    f"{self.base_url}/v1beta/models/{model}:generateContent",
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                attempts += 1
                if attempts > self.max_retries:
                    raise GeminiError(f"request failed after {attempts} attempts: {exc}") from exc
                self._sleep_backoff(attempts, "connection error")
                continue

            if response.status_code == 200:
                return self._parse(response, model)

            if response.status_code == 429 or 500 <= response.status_code < 600:
                attempts += 1
                if attempts > self.max_retries:
                    raise GeminiError(
                        f"HTTP {response.status_code} after {attempts} attempts: {_snippet(response)}"
                    )
                delay = self._retry_delay(response)
                logger.warning(
                    "HTTP %s from Gemini, retrying in %.1fs (%d/%d)",
                    response.status_code,
                    delay,
                    attempts,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            if response.status_code == 400 and "thinkingConfig" in json.dumps(
                _error_payload(response), ensure_ascii=False
            ):
                if self._send_thinking_config:
                    logger.warning("model rejected thinkingConfig; retrying without it")
                    self._send_thinking_config = False
                    generation_config.pop("thinkingConfig", None)
                    continue
                raise GeminiError(f"HTTP 400: {_snippet(response)}")

            if response.status_code == 404:
                available = [m for m in self.list_models() if "flash" in m]
                raise GeminiError(
                    f"model '{model}' not found (HTTP 404). Available flash models: "
                    f"{', '.join(available[:15]) or 'unknown'}"
                )

            raise GeminiError(f"HTTP {response.status_code}: {_snippet(response)}")

    # --------------------------------------------------------------- helpers

    def _parse(self, response: requests.Response, model: str) -> Completion:
        try:
            body = response.json()
        except ValueError as exc:
            raise GeminiError(f"invalid JSON from Gemini: {_snippet(response)}") from exc

        candidates = body.get("candidates") or []
        if not candidates:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise GeminiError(f"no candidates returned ({reason})")

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        raw_parts = (candidate.get("content") or {}).get("parts") or []
        # Thinking models return extra parts flagged with ``thought: true``; the JSON answer
        # lives in the remaining parts only.
        parts = [
            part
            for part in raw_parts
            if isinstance(part, dict) and not part.get("thought", False)
        ]
        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            raise GeminiError(f"empty response (finishReason={finish_reason})")

        usage = body.get("usageMetadata") or {}
        return Completion(
            content=text,
            raw=body,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
        )

    def _retry_delay(self, response: requests.Response) -> float:
        delay = 0.0
        header = response.headers.get("Retry-After")
        if header:
            try:
                delay = float(header)
            except ValueError:
                delay = 0.0
        if delay <= 0:
            for detail in _error_payload(response).get("details", []) or []:
                raw = detail.get("retryDelay")
                if isinstance(raw, str) and raw.endswith("s"):
                    try:
                        delay = float(raw[:-1])
                    except ValueError:
                        delay = 0.0
                    break
        return min(max(delay, 0.5), 60.0)

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        delay = min(2.0**attempt, 30.0)
        logger.warning("%s; retrying in %.1fs (%d/%d)", reason, delay, attempt, self.max_retries)
        time.sleep(delay)


def _error_payload(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else {}


def _snippet(response: requests.Response, limit: int = 300) -> str:
    text = (response.text or "").strip().replace("\n", " ")
    return text[:limit]

"""Thin, rate-limit aware client for the WorldQuant BRAIN REST API."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Mapping

import requests

from .config import BASE_URL, PAGE_SIZE, REQUEST_TIMEOUT, Credentials, mask

logger = logging.getLogger(__name__)


class BrainAuthError(RuntimeError):
    """Authentication with the BRAIN API failed."""


class BrainApiError(RuntimeError):
    """An API request failed after exhausting retries."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        params: Mapping[str, Any] | None = None,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.params = dict(params or {})
        self.status_code = status_code
        self.body = body


class BrainClient:
    """Session wrapper handling authentication, rate limiting and pagination."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str = BASE_URL,
        page_size: int = PAGE_SIZE,
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = 6,
        max_rate_limit_waits: int = 200,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_rate_limit_waits = max_rate_limit_waits
        self._credentials = credentials
        self._session = session or requests.Session()
        self._authenticated = False

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "BrainClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ auth

    def authenticate(self, *, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        self._authenticated = False
        self._session.auth = (self._credentials.email, self._credentials.password)
        try:
            response = self._session.post(f"{self.base_url}/authentication", timeout=self.timeout)
        except requests.RequestException as exc:
            raise BrainAuthError(f"authentication request failed: {exc}") from exc
        finally:
            # Only the authentication call needs Basic auth; later calls use the session cookie.
            self._session.auth = None

        if response.status_code != 201:
            raise BrainAuthError(
                f"authentication failed with HTTP {response.status_code}: "
                f"{_snippet(response.text)}"
            )
        self._authenticated = True
        logger.info("authenticated with BRAIN API as %s", mask(self._credentials.email))

    # --------------------------------------------------------------- request

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET a BRAIN endpoint and return parsed JSON, honouring rate limits."""
        self.authenticate()
        url = f"{self.base_url}{path}"
        attempts = 0
        rate_limit_waits = 0
        reauthenticated = False

        while True:
            try:
                response = self._session.get(url, params=dict(params or {}), timeout=self.timeout)
            except requests.RequestException as exc:
                attempts += 1
                if attempts > self.max_retries:
                    raise BrainApiError(
                        f"request to {path} failed after {attempts} attempts: {exc}",
                        path=path,
                        params=params,
                    ) from exc
                self._sleep_backoff(attempts, f"connection error on {path}")
                continue

            status = response.status_code

            if status in (200, 201):
                try:
                    return response.json()
                except ValueError as exc:
                    raise BrainApiError(
                        f"invalid JSON from {path}: {_snippet(response.text)}",
                        path=path,
                        params=params,
                        status_code=status,
                    ) from exc

            if status == 204:
                return {"count": 0, "results": []}

            if status == 429:
                rate_limit_waits += 1
                if rate_limit_waits > self.max_rate_limit_waits:
                    raise BrainApiError(
                        f"rate limited {rate_limit_waits} times on {path}; giving up",
                        path=path,
                        params=params,
                        status_code=status,
                    )
                delay = self._retry_after(response)
                logger.info(
                    "rate limited on %s (wait %d/%d), sleeping %.1fs",
                    path,
                    rate_limit_waits,
                    self.max_rate_limit_waits,
                    delay,
                )
                time.sleep(delay)
                continue

            if status == 401 and not reauthenticated:
                reauthenticated = True
                logger.info("session rejected on %s, re-authenticating", path)
                self.authenticate(force=True)
                continue

            if 500 <= status < 600:
                attempts += 1
                if attempts > self.max_retries:
                    raise BrainApiError(
                        f"server error {status} on {path}: {_snippet(response.text)}",
                        path=path,
                        params=params,
                        status_code=status,
                        body=_snippet(response.text),
                    )
                self._sleep_backoff(attempts, f"HTTP {status} on {path}")
                continue

            raise BrainApiError(
                f"HTTP {status} on {path}: {_snippet(response.text)}",
                path=path,
                params=params,
                status_code=status,
                body=_snippet(response.text),
            )

    def iter_pages(
        self, path: str, params: Mapping[str, Any], *, page_size: int | None = None
    ) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Yield ``(total_count, page_items)`` for every page of a paged endpoint."""
        limit = page_size or self.page_size
        offset = 0
        while True:
            page_params = dict(params, limit=limit, offset=offset)
            payload = self.get_json(path, page_params)
            items = payload.get("results") or []
            total = int(payload.get("count") or 0)
            yield total, items
            offset += len(items)
            if not items or offset >= total:
                return

    # --------------------------------------------------------------- helpers

    def _retry_after(self, response: requests.Response) -> float:
        raw = response.headers.get("Retry-After")
        try:
            delay = float(raw) if raw is not None else 1.0
        except (TypeError, ValueError):
            delay = 1.0
        return min(max(delay, 0.5), 120.0)

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        delay = min(2.0 ** attempt, 60.0)
        logger.warning("%s; retrying in %.1fs (attempt %d/%d)", reason, delay, attempt, self.max_retries)
        time.sleep(delay)


def _snippet(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit]

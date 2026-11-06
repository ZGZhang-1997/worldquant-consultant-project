"""BRAIN simulation client: submit, poll and fetch alpha results."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from ..acquisition.config import BASE_URL, Credentials

logger = logging.getLogger(__name__)

SIMULATIONS_PATH = "/simulations"
# BRAIN signals "not ready yet" with a Retry-After header rather than a non-2xx status.
MIN_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 60.0


class BrainSimulationError(RuntimeError):
    """A simulation request failed (auth, HTTP or platform-side rejection)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}
        self.retry_after = retry_after

    @property
    def detail(self) -> str:
        """Platform error detail, including the offending expression location."""
        payload = self.payload
        message = payload.get("message") or ""
        location = payload.get("location") or {}
        if isinstance(location, dict) and location.get("line"):
            return f"{message} (line {location.get('line')}, start {location.get('start')})"
        return str(message) or str(self)

class SimulationTimeout(BrainSimulationError):
    """The simulation did not finish before the deadline."""


class BrainSimulationClient:
    """Minimal wrapper around the simulation endpoints."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        poll_timeout: float = 900.0,
        max_submit_retries: int = 60,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_timeout = poll_timeout
        self.max_submit_retries = max_submit_retries
        self._credentials = credentials
        self._session = session or requests.Session()
        self._authenticated = False

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "BrainSimulationClient":
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
            raise BrainSimulationError(f"authentication request failed: {exc}") from exc
        finally:
            self._session.auth = None
        if response.status_code != 201:
            raise BrainSimulationError(
                f"authentication failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        self._authenticated = True

    # ------------------------------------------------------------- simulate

    def submit(self, expression: str, settings_payload: dict[str, Any]) -> str:
        """POST a simulation and return the polling URL from the Location header."""
        self.authenticate()
        payload = {
            "type": "REGULAR",
            "settings": settings_payload,
            "regular": expression,
        }
        # The platform rejects submissions beyond the account's concurrency allowance with
        # 429 + Retry-After; wait it out instead of failing the whole backtest.
        attempts = 0
        while True:
            try:
                response = self._request("post", SIMULATIONS_PATH, json=payload)
                break
            except BrainSimulationError as exc:
                if exc.status_code != 429 or attempts >= self.max_submit_retries:
                    raise
                attempts += 1
                delay = exc.retry_after if exc.retry_after else _retry_after_from_payload(exc.payload)
                logger.info(
                    "submission rejected by the concurrency limit, retrying in %.1fs (%d/%d)",
                    delay,
                    attempts,
                    self.max_submit_retries,
                )
                time.sleep(delay)
        location = response.headers.get("Location") or response.headers.get("location")
        if not location:
            raise BrainSimulationError(
                "simulation accepted but no Location header returned",
                status_code=response.status_code,
                payload=_json_or_empty(response),
            )
        if location.startswith("/"):
            location = f"{self.base_url}{location}"
        logger.info("simulation submitted: %s", location)
        return location

    def poll(self, location: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Poll until the simulation finishes and return its result object."""
        deadline = time.monotonic() + (self.poll_timeout if timeout is None else timeout)
        last_progress: float | None = None
        while True:
            response = self._get_with_auth_retry(location)
            body = _json_or_empty(response)

            retry_after = response.headers.get("Retry-After")
            progress = body.get("progress")
            finished = not retry_after and body.get("status") not in (None, "PENDING", "RUNNING")
            if isinstance(progress, (int, float)) and progress < 1 and retry_after is None:
                finished = False

            if isinstance(progress, (int, float)) and progress != last_progress:
                logger.info("simulation progress: %.0f%%", float(progress) * 100)
                last_progress = float(progress)

            if finished:
                status = str(body.get("status", "")).upper()
                if status in ("ERROR", "FAIL", "FAILED"):
                    raise BrainSimulationError(
                        f"simulation failed: {body.get('message') or status}",
                        payload=body,
                    )
                if not body.get("alpha"):
                    raise BrainSimulationError(
                        f"simulation finished without an alpha id (status={status or 'unknown'})",
                        payload=body,
                    )
                return body

            if time.monotonic() > deadline:
                raise SimulationTimeout(
                    f"simulation did not finish within {timeout or self.poll_timeout:.0f}s",
                    payload=body,
                )
            time.sleep(_poll_delay(retry_after))

    def get_alpha(self, alpha_id: str, *, attempts: int = 3) -> dict[str, Any]:
        """Fetch the alpha record that carries the IS/OS metrics and checks."""
        self.authenticate()
        last: dict[str, Any] = {}
        for _ in range(attempts):
            response = self._get_with_auth_retry(f"{self.base_url}/alphas/{alpha_id}")
            body = _json_or_empty(response)
            last = body
            # Checks can still be running right after the simulation completes.
            if response.headers.get("Retry-After"):
                time.sleep(_poll_delay(response.headers.get("Retry-After")))
                continue
            return body
        return last

    def simulation_snapshot(self, sim_id: str) -> dict[str, Any] | None:
        """One non-blocking look at a submitted simulation.

        Returns ``None`` when the platform no longer knows the id (404), otherwise the
        body with ``finished`` added: True once the platform stopped computing, False
        while ``Retry-After`` still asks us to wait.
        """
        self.authenticate()
        try:
            response = self._get_with_auth_retry(f"{self.base_url}/simulations/{sim_id}")
        except BrainSimulationError as exc:
            if exc.status_code in (404, 403):
                return None
            raise
        body = _json_or_empty(response)
        status = str(body.get("status") or "").upper()
        retry_after = response.headers.get("Retry-After")
        finished = not retry_after and status not in ("", "PENDING", "RUNNING")
        body["finished"] = finished
        # Only a Retry-After means the platform is actively working on it; a bare
        # ``{"progress": x}`` reply can also mean an abandoned simulation.
        body["waiting"] = bool(retry_after)
        return body

    # --------------------------------------------------------------- helpers

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            response = getattr(self._session, method)(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise BrainSimulationError(f"{method.upper()} {path} failed: {exc}") from exc

        if response.status_code in (200, 201):
            return response

        payload = _json_or_empty(response)
        detail = payload.get("message") or (response.text or "").strip()[:300]
        location = payload.get("location")
        if isinstance(location, dict) and location.get("line"):
            detail = f"{detail} (line {location.get('line')}, start {location.get('start')})"
        retry_after = response.headers.get("Retry-After")
        raise BrainSimulationError(
            f"{method.upper()} {path} returned HTTP {response.status_code}: {detail}".rstrip(": "),
            status_code=response.status_code,
            payload=payload,
            retry_after=float(retry_after) if _is_number(retry_after) else None,
        )

    def _get_with_auth_retry(self, url: str) -> requests.Response:
        try:
            return self._request("get", url)
        except BrainSimulationError as exc:
            if exc.status_code != 401:
                raise
        self.authenticate(force=True)
        return self._request("get", url)


def _json_or_empty(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _poll_delay(retry_after: str | None) -> float:
    try:
        delay = float(retry_after) if retry_after else MIN_POLL_INTERVAL
    except (TypeError, ValueError):
        delay = MIN_POLL_INTERVAL
    return min(max(delay, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)


def _retry_after_from_payload(payload: dict[str, Any]) -> float:
    """Use the platform's Retry-After hint when present, else a short default."""
    for key in ("Retry-After", "retry_after", "retryAfter"):
        value = payload.get(key)
        if value is not None:
            try:
                return min(max(float(value), 1.0), 300.0)
            except (TypeError, ValueError):
                break
    return 5.0


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True

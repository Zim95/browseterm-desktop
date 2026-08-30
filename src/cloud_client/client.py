"""
The ONLY intended boundary through which Local code talks to central Cloud state (P06).

    Local Handler
          |
          v
      CloudClient
          |
        HTTPS
          |
          v
    Cloud browseterm-server

Deliberately narrow: only the P05 Device Cloud API surface P06/Desktop actually needs
(register/list/get/update/heartbeat). Do not add unrelated Cloud endpoints here without a
corresponding Cloud API existing first - see p.md's P06 "IMPORTANT MIGRATION-SCOPE RULE" note.

Auth (interim, pre-P07): the plan's final design (section 8) has Desktop reuse Cloud OAuth
through the system browser and receive a device-scoped credential via a one-time local
handoff/loopback callback - that handoff does not exist yet (it's P07's job), and p06.md
forbids inventing JWT/PKI/mTLS/API keys here. So callers supply the same opaque Redis
session-cookie value the browser already holds after logging in via Local's existing OAuth
flow; Cloud and Local currently share one Redis (documented in p.md's P05 section), so that
cookie validates against Cloud's `authenticate_session` decorator unchanged. This is a
placeholder - P07 replaces it with the real device-scoped credential flow.
"""
from typing import Any, Optional

import httpx

from src.cloud_client.config import BROWSETERM_CLOUD_API_URL, SESSION_COOKIE_NAME


class CloudClientError(Exception):
    """Raised for any non-2xx Cloud API response, or a transport-level failure (status_code=0)."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Cloud API error {status_code}: {message}")


class CloudClient:
    """Thin authenticated HTTP client for the Cloud Device API (P05)."""

    def __init__(
        self,
        base_url: str = BROWSETERM_CLOUD_API_URL,
        session_cookie: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._session_cookie = session_cookie
        self._timeout = timeout

    def _cookies(self) -> dict:
        if not self._session_cookie:
            return {}
        return {SESSION_COOKIE_NAME: self._session_cookie}

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        url = f"{self._base_url}{path}"
        try:
            response = httpx.request(
                method, url, json=json_body, cookies=self._cookies(), timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise CloudClientError(0, str(e)) from e
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except Exception:
                message = response.text
            raise CloudClientError(response.status_code, message)
        return response.json()

    def register_device(self, device: dict[str, Any]) -> dict:
        """POST /devices. Raises CloudClientError(status_code=409) on a duplicate
        (user_id, device_name) - per P05's actual semantics, this is NOT idempotent. Callers
        that want find-or-update behavior must catch 409 and use list_devices/update_device
        themselves (see desktop/device_registration.py)."""
        return self._request("POST", "/devices", json_body=device)["device"]

    def list_devices(self) -> list[dict]:
        """GET /devices - the authenticated user's own devices only."""
        return self._request("GET", "/devices")["devices"]

    def get_device(self, device_id: str) -> dict:
        return self._request("GET", f"/devices/{device_id}")["device"]

    def update_device(self, device_id: str, fields: dict[str, Any]) -> dict:
        """POST /devices/{device_id} - partial update of mutable metadata/allocation fields."""
        return self._request("POST", f"/devices/{device_id}", json_body=fields)["device"]

    def heartbeat(self, device_id: str) -> dict:
        """POST /devices/{device_id}/heartbeat."""
        return self._request("POST", f"/devices/{device_id}/heartbeat")["device"]

"""
The Desktop app's boundary to Cloud's Device API and device-bootstrap redemption.

P07 change: Device API calls now carry `Authorization: Bearer <device_token>` (the long-lived,
per-device credential from macOS Keychain - see desktop/keychain.py) instead of the browser
session cookie. This app never holds a Google/GitHub secret and never performs OAuth itself
(p07.md section 19/39) - the only OAuth-adjacent thing it does is the one-time bootstrap
redemption below, which trades a short-lived bootstrap code (obtained from Local, itself gated on
the already-authenticated WebView session) for the device token.
"""
from typing import Any, Optional

import httpx

from desktop.config import BROWSETERM_CLOUD_API_URL


class CloudClientError(Exception):
    """Raised for any non-2xx Cloud API response, or a transport-level failure (status_code=0)."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Cloud API error {status_code}: {message}")

    @property
    def is_auth_failure(self) -> bool:
        """True for a 401 -- the device token is missing/invalid/expired/revoked."""
        return self.status_code == 401


def redeem_device_bootstrap(code: str, device: dict[str, Any], base_url: str = BROWSETERM_CLOUD_API_URL) -> dict:
    """POST /auth/device-bootstrap/redeem -- public but possession-gated (holding the one-time
    bootstrap code Local just handed us). Returns {"device": {...}, "device_token": "bst_device_..."}.
    Raises CloudClientError(status_code=401) for an invalid/expired/already-used code."""
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/auth/device-bootstrap/redeem",
            json={"code": code, "device": device},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise CloudClientError(0, str(e)) from e
    if response.status_code < 200 or response.status_code >= 300:
        try:
            message = response.json().get("error", response.text)
        except Exception:
            message = response.text or response.reason_phrase
        raise CloudClientError(response.status_code, message)
    return response.json()


class CloudClient:
    """Thin device-token-authenticated HTTP client for Cloud's Device API."""

    def __init__(self, device_token: Optional[str] = None, base_url: str = BROWSETERM_CLOUD_API_URL, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token
        self._timeout = timeout

    def _headers(self) -> dict:
        if not self._device_token:
            return {}
        return {"Authorization": f"Bearer {self._device_token}"}

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        url = f"{self._base_url}{path}"
        try:
            response = httpx.request(
                method, url, json=json_body, headers=self._headers(), timeout=self._timeout, follow_redirects=False
            )
        except httpx.HTTPError as e:
            raise CloudClientError(0, str(e)) from e
        if response.status_code < 200 or response.status_code >= 300:
            try:
                message = response.json().get("error", response.text)
            except Exception:
                message = response.text or response.reason_phrase
            raise CloudClientError(response.status_code, message)
        return response.json()

    def get_device(self, device_id: str) -> dict:
        return self._request("GET", f"/devices/{device_id}")["device"]

    def update_device(self, device_id: str, fields: dict[str, Any]) -> dict:
        return self._request("POST", f"/devices/{device_id}", json_body=fields)["device"]

    def heartbeat(self, device_id: str) -> dict:
        """POST /devices/{id}/heartbeat -- marks this device ACTIVE (demoting any other active
        device of this user)."""
        return self._request("POST", f"/devices/{device_id}/heartbeat")["device"]

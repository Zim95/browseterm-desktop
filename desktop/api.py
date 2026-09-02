"""
The JS <-> Python bridge exposed to the app shell (desktop/web/app.html) as
`window.pywebview.api`. Holds no window reference itself.

P07: device bootstrap (trading a fresh WebView session cookie for the long-lived device token)
happens automatically in DesktopApp right after login (desktop/app.py) - by the time app.html is
showing, a device token normally already exists in Keychain. `activate_device` here is the
lighter-weight "re-activate this device" action (a heartbeat with the existing token, demoting
any other of this user's active devices) - it deliberately does NOT re-run bootstrap, since that
needs a live WebView session this shell no longer has once swapped away from it. If the token is
genuinely missing/invalid, the answer is "log out and log back in", not a hidden second bootstrap
path here (p07.md: "do not unnecessarily expand P07 into device-management UI").
"""
from typing import Any, Callable, Optional

from desktop.cloud_client import CloudClient, CloudClientError
from desktop.device_info import detect_hardware
from desktop.keychain import KeychainStorage
from desktop.state import DesktopState


def _device_status(device: Optional[dict]) -> str:
    if device is None:
        return "not_registered"
    return device.get("status", "unknown").lower()


class Api:
    """`on_logout` is called after local/Keychain state is cleared, so `DesktopApp` can swap the
    window back to the login page. `on_retry_login` backs the "Retry" button on the
    connection-error page (desktop/app.py) shown when Local can't be reached. `on_start_login`
    backs the "Log in" button on the login-start page - login now happens in the system browser
    (see desktop/app.py's module docstring), so this just kicks that off; `Api` itself holds no
    window/browser reference of its own, same as the other two callbacks."""

    def __init__(
        self, state: DesktopState, keychain: KeychainStorage,
        on_logout: Callable[[], None], on_retry_login: Callable[[], None],
        on_start_login: Callable[[], None],
    ):
        self._state = state
        self._keychain = keychain
        self._on_logout = on_logout
        self._on_retry_login = on_retry_login
        self._on_start_login = on_start_login

    def device_info(self) -> dict[str, Any]:
        hardware = detect_hardware()
        device = None
        error = None
        token = self._keychain.get_device_token()
        if token and self._state.device_id:
            try:
                client = CloudClient(device_token=token)
                device = client.get_device(self._state.device_id)
            except CloudClientError as e:
                if e.status_code == 404:
                    self._state.device_id = None
                    self._state.save()
                elif not e.is_auth_failure:
                    error = e.message
                # an auth failure (401) here just means "not activated yet" - not an error to
                # surface, the Activate button / a fresh login handles it.
        return {"hardware": hardware, "device": device, "status": _device_status(device), "error": error}

    def activate_device(self) -> dict[str, Any]:
        token = self._keychain.get_device_token()
        if not token or not self._state.device_id:
            return {
                "hardware": detect_hardware(), "device": None, "status": "not_registered",
                "error": "No device credential yet -- please log out and log back in to activate this device.",
            }
        try:
            client = CloudClient(device_token=token)
            device = client.heartbeat(self._state.device_id)
        except CloudClientError as e:
            return {"hardware": detect_hardware(), "device": None, "status": "unknown", "error": e.message}
        return {"hardware": detect_hardware(), "device": device, "status": _device_status(device), "error": None}

    def logout(self) -> None:
        self._on_logout()

    def retry_login(self) -> None:
        self._on_retry_login()

    def start_login(self) -> None:
        self._on_start_login()

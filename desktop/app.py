"""
Desktop app orchestration: owns the pywebview window and is the only place that navigates it.

Login flow (OAuth Device Authorization Grant, RFC 8628 - see the device-auth follow-up to
~/browseterm/p07.md): this app's WebView never hosts a provider's login page, and never needs
`browseterm-server-local` reachable at all - a deliberate change from the original P07 design
(system-browser + loopback server, still visible in git history), which routed login through
Local's own `/login` and therefore couldn't complete before Local itself was running. Since Local
only ever runs as a Kubernetes Deployment inside the very k3d cluster the Cluster section's Setup
button creates - a button that only appeared post-login - that was a real chicken-and-egg: no
cluster -> Local unreachable -> can't log in -> can't reach the button that creates the cluster.
Moving to the device grant breaks it, because this app now only ever talks to Cloud to log in:

1. The WebView shows a small local "Log in" page (`desktop/web/login_start.html`). Clicking it
   calls Cloud's `POST /auth/device/start` (`desktop/cloud_client.start_device_login`), which
   starts an OAuth Device Authorization Grant against the provider server-side (Cloud holds the
   provider's device-flow client secret - this app never does, see cloud_client.py's module
   docstring) and returns a short `user_code` plus a `verification_uri`.
2. This app shows that code in the WebView and opens `verification_uri` in the user's SYSTEM
   browser via `webbrowser.open()` - same "let Google/GitHub see a real browser" reasoning the
   old design had, just reached a different way. The user approves the code there, in their own
   time, in their own browser tab; this process is not involved in that step at all.
3. Meanwhile a background thread polls Cloud's `POST /auth/device/poll`
   (`desktop/cloud_client.poll_device_login`) every `interval` seconds. Once the user approves,
   a poll comes back `{"status": "complete", "device": {...}, "device_token": "..."}` - Cloud has
   already run the same find-or-create-user + register-device + issue-token pipeline the old
   bootstrap-redeem path used, so the result is handled identically: the device Bearer token goes
   into macOS Keychain (`desktop/keychain.py`), and every Device API call after that uses
   `Authorization: Bearer <device_token>`, never a browser session cookie (p07.md section 20).

Device heartbeat is no longer this app's job - it moved to `desktop/daemon.py` so the GUI and the
headless daemon never race each other heartbeating the same device independently. This app still
checks Keychain for a valid token on startup exactly as before; it just doesn't keep it fresh
itself any more.

Only "google" is wired up today - GitHub device flow needs its own OAuth-console follow-up first
(tracked separately, see cloud_client.py). Consequence for restart behavior, unchanged from
before: "am I logged in" is still "does Keychain hold a valid device token", independent of
anything in the system browser. A valid Keychain token on startup skips the login page entirely
and goes straight to the Device page.
"""
import json
import os
import threading
import time
import webbrowser
from typing import Optional

import webview

from desktop.api import Api
from desktop.cloud_client import CloudClient, CloudClientError, poll_device_login, start_device_login
from desktop.config import DESKTOP_LOGIN_TIMEOUT_SECONDS
from desktop.device_info import BYTES_PER_GB, detect_hardware
from desktop.keychain import KeychainStorage
from desktop.state import load_state

_APP_HTML = os.path.join(os.path.dirname(__file__), "web", "app.html")
_LOGIN_START_HTML = os.path.join(os.path.dirname(__file__), "web", "login_start.html")

# RFC 8628 recommends a default 5s poll interval when a provider's response doesn't specify one;
# also used as the minimum floor for whatever interval the provider does return, and as the "slow
# down" backoff step (section 3.5: add 5 seconds on a slow_down response).
_DEFAULT_POLL_INTERVAL_SECONDS = 5

# The two terminal (non-retryable) poll statuses Cloud can report, and the message to show for
# each - looked up by _run_login_flow instead of an if/elif per status.
_POLL_TERMINAL_ERROR_MESSAGES = {
    "expired": "The login code expired. Please try again.",
    "denied": "Login was denied.",
}


class DesktopApp:
    def __init__(self):
        self._state = load_state()
        self._keychain = KeychainStorage()
        self._authenticated = False
        self._login_in_progress = False
        self._api = Api(
            self._state, self._keychain,
            on_logout=self._handle_logout, on_retry_login=self._handle_retry_login,
            on_start_login=self._handle_start_login,
            on_setup_step=self._handle_setup_step,
        )
        self._window: Optional[webview.Window] = None

    def run(self) -> None:
        create_kwargs = self._resolve_start_kwargs()
        self._window = webview.create_window(
            "BrowseTerm",
            js_api=self._api,
            width=1200,
            height=800,
            min_size=(900, 600),
            background_color="#A8FBD3",
            **create_kwargs,
        )
        webview.start()

    def _resolve_start_kwargs(self) -> dict:
        '''Login no longer depends on Local being reachable (see module docstring) - so unlike
        the old design, there is no connection-error page to fall back to here at all; a Cloud
        that's unreachable simply surfaces as a login error once the user actually clicks "Log
        in" (start_device_login raises CloudClientError, caught in _run_login_flow), the same way
        any other Cloud API failure already does.'''
        if self._state.device_id and self._device_token_is_valid():
            self._authenticated = True
            return {"url": _APP_HTML}
        return {"url": _LOGIN_START_HTML}

    def _device_token_is_valid(self) -> bool:
        token = self._keychain.get_device_token()
        if not token:
            return False
        try:
            CloudClient(device_token=token).get_device(self._state.device_id)
            return True
        except CloudClientError as e:
            return not e.is_auth_failure and e.status_code != 404

    def _handle_retry_login(self) -> None:
        self._go_to_login_start()

    def _go_to_login_start(self) -> None:
        if self._window is None:
            return
        self._window.load_url(_LOGIN_START_HTML)

    def _handle_start_login(self, provider: str) -> None:
        '''Kicks off the device-grant login flow (see module docstring) in a background thread -
        polling Cloud blocks waiting for the user to approve the code, and this must never block
        the WebView's own event loop. `provider` is "google" or "github" (login_start.html's two
        buttons) - GitHub is wired up end to end but will error until its OAuth App has Device
        Flow enabled server-side (see Cloud's GithubDeviceAuthService docstring); this app doesn't
        special-case that; it just surfaces whatever error Cloud reports, same as any other
        provider failure.'''
        if self._login_in_progress:
            return
        self._login_in_progress = True
        threading.Thread(target=self._run_login_flow, args=(provider,), daemon=True).start()

    def _registration_payload(self) -> dict:
        '''Cloud's `POST /devices` (reached via device-auth poll, or the older device-bootstrap
        redeem) requires allocated_cpu/allocated_memory_bytes/allocated_storage_bytes up front,
        not just the physical totals -- see `RegisterDeviceRequest`
        (browseterm-server/src/cloud/device_data_models.py). On a machine that's never had an
        allocation chosen yet (first login, or Keychain/state got wiped), this defaults it the
        same way the Cluster section's own first-ever read does
        (`DesktopState.ensure_allocation_defaults`) and persists it immediately, so the value sent
        here and the value the Cluster section shows afterwards are always the same one.'''
        hardware = detect_hardware()
        self._state.ensure_allocation_defaults(hardware)
        return {
            **hardware,
            "allocated_cpu": self._state.allocated_cpu,
            "allocated_memory_bytes": int(self._state.allocated_memory_gb * BYTES_PER_GB),
            "allocated_storage_bytes": int(self._state.allocated_storage_gb * BYTES_PER_GB),
        }

    def _run_login_flow(self, provider: str) -> None:
        '''One try wrapping the whole flow (start + poll loop), one `except CloudClientError` and
        one `finally` - both steps only ever fail the same way (a Cloud API call raising
        CloudClientError), so two separate try blocks with identical except/finally bodies would
        just be the same handling written twice.'''
        result = None
        try:
            start = start_device_login(provider)
            verification_uri = start.get("verification_uri")
            self._show_device_code(start["user_code"], verification_uri)
            webbrowser.open(start.get("verification_uri_complete") or verification_uri)

            device_code = start["device_code"]
            device_payload = self._registration_payload()
            interval = max(start.get("interval") or _DEFAULT_POLL_INTERVAL_SECONDS, _DEFAULT_POLL_INTERVAL_SECONDS)
            deadline = time.monotonic() + (start.get("expires_in") or DESKTOP_LOGIN_TIMEOUT_SECONDS)

            while time.monotonic() < deadline:
                time.sleep(interval)
                poll = poll_device_login(device_code, device_payload, provider)
                status = poll.get("status")
                if status == "complete":
                    result = poll
                    break
                if status == "pending":
                    if poll.get("slow_down"):
                        interval += _DEFAULT_POLL_INTERVAL_SECONDS
                    continue
                # expired/denied/error all just show a message and give up - a dict lookup for
                # the two known terminal statuses, falling back to the provider's own message
                # (or a generic one) for anything else, instead of an if/elif per status.
                self._show_login_error(
                    _POLL_TERMINAL_ERROR_MESSAGES.get(status, poll.get("error") or "Login failed. Please try again.")
                )
                return
            else:
                self._show_login_error("Login timed out. Please try again.")
                return
        except CloudClientError as e:
            self._show_login_error(e.message)
            return
        finally:
            self._login_in_progress = False

        self._keychain.set_device_token(result["device_token"])
        self._state.device_id = result["device"]["id"]
        self._state.device_name = result["device"]["device_name"]
        self._state.save()
        self._authenticated = True
        if self._window is not None:
            self._window.load_url(_APP_HTML)

    def _show_device_code(self, user_code: str, verification_uri: str) -> None:
        if self._window is None:
            return
        message = f"Enter this code at {verification_uri} (opening in your browser now):"
        self._window.evaluate_js(
            "document.getElementById('status').textContent = " + json.dumps(message) + ";"
            "document.getElementById('deviceCode').textContent = " + json.dumps(user_code) + ";"
            "document.getElementById('codeRow').style.display = 'flex';"
        )

    def _show_login_error(self, message: str) -> None:
        if self._window is None:
            return
        self._window.evaluate_js(
            "document.getElementById('status').textContent = " + json.dumps(message) + ";"
            "document.getElementById('codeRow').style.display = 'none';"
            "document.getElementById('googleLoginBtn').disabled = false;"
            "document.getElementById('githubLoginBtn').disabled = false;"
        )

    def _handle_logout(self) -> None:
        '''Clears the device credential and returns to the login-start page. Does NOT call
        Local's /logout: this app never holds Local's session cookie at all (the whole login flow
        happens in the system browser, in its own cookie jar this process never touches) - see the
        module docstring. Any lingering browser session there is left to expire on its own TTL
        rather than being explicitly revoked - a documented trade-off, not an oversight.
        Heartbeating stops on its own next tick (desktop/daemon.py checks Keychain fresh every
        interval) - nothing here to stop directly any more.'''
        self._authenticated = False
        self._keychain.delete_device_token()
        self._state.clear()
        self._go_to_login_start()

    def _handle_setup_step(self, step_name: str, status: str, detail: str) -> None:
        '''Passed into Api as `on_setup_step` - the live progress feed for the Cluster section's
        Setup button (desktop/cluster_manager.py/desktop/local_stack.py's `on_step` callback).
        Pushes into the DOM the same way `_show_device_code`/`_show_login_error` already do below
        - `window.onSetupStep` (desktop/web/static/js/app.js) owns rendering the step list itself,
        this just delivers the event.'''
        if self._window is None:
            return
        self._window.evaluate_js(
            "window.onSetupStep && window.onSetupStep("
            + json.dumps(step_name) + ", " + json.dumps(status) + ", " + json.dumps(detail) + ");"
        )


def run() -> None:
    DesktopApp().run()

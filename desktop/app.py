"""
Desktop app orchestration: owns the pywebview window and is the only place that navigates it.

Login flow: this app's WebView never hosts Google/GitHub's login page itself any more. Google
(and increasingly GitHub) actively block or challenge OAuth sign-in attempted from an embedded
WebView - exactly what pywebview's window is - as a long-standing anti-phishing policy, so
showing Local's real `/login` page inside this window (the original P07 design, see
~/browseterm/p07.md) hits that block. Instead:

1. The WebView shows a small local "Log in" page (`desktop/web/login_start.html`). Clicking it
   starts a one-shot local loopback HTTP server (`desktop/loopback_server.py`) on 127.0.0.1 and
   opens Local's real `/login?target=desktop&desktop_port=<n>` in the user's actual SYSTEM
   browser via `webbrowser.open()` - Google/GitHub see a real browser, not an embedded one.
2. The user completes OAuth and Local's own login flow entirely in that system browser tab,
   exactly like any other browser visit to Local. Local's `auth_callback`
   (browseterm-server-local's `src/api_handlers.py`) recognizes the desktop_port cookie set in
   step 1 survived the OAuth round trip (same browser tab, same domain), mints a one-time
   device-bootstrap code server-side, and redirects the SYSTEM browser to this app's waiting
   loopback server with that code.
3. The loopback server hands the code back to this process, which redeems it against Cloud's
   public `/auth/device-bootstrap/redeem` exactly as before, and stores the resulting long-lived
   device Bearer token in macOS Keychain (`desktop/keychain.py`) - every Device API call after
   that uses `Authorization: Bearer <device_token>`, never a browser session cookie at all
   (p07.md section 20 - unchanged from the original design, only how the bootstrap CODE gets to
   this process changed).

Consequence for restart behavior: "am I logged in" is still "does Keychain hold a valid device
token" - independent of the system browser's own session. A valid Keychain token on startup skips
the login page entirely and goes straight to the Device page.

Unreachable Local: pywebview has no "failed to load" event to react to (only `loaded`, which
simply never fires on a failed navigation), so a bad `BROWSETERM_LOCAL_URL` or Local just not
running would otherwise leave the window showing nothing but its raw `background_color` forever,
with no explanation. `_backend_reachable` probes Local with a short timeout *before* the window
ever tries to load the login-start page or open the system browser, so that failure mode always
shows a real, actionable error page (`_connection_error_html`, with a Retry button) instead of a
silent blank screen or a browser tab pointed at nothing.
"""
import json
import os
import threading
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import httpx
import webview

from desktop.api import Api
from desktop.cloud_client import CloudClient, CloudClientError, redeem_device_bootstrap
from desktop.config import (
    BROWSETERM_LOCAL_URL,
    DESKTOP_LOGIN_TIMEOUT_SECONDS,
    DEVICE_HEARTBEAT_INTERVAL_SECONDS,
)
from desktop.device_info import detect_hardware
from desktop.keychain import KeychainStorage
from desktop.loopback_server import LoopbackServer
from desktop.state import load_state

_APP_HTML = os.path.join(os.path.dirname(__file__), "web", "app.html")
_LOGIN_START_HTML = os.path.join(os.path.dirname(__file__), "web", "login_start.html")
_BACKEND_CHECK_TIMEOUT_SECONDS = 4.0


def _backend_reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=_BACKEND_CHECK_TIMEOUT_SECONDS, follow_redirects=True)
        return True
    except httpx.HTTPError:
        return False


def _connection_error_html(target_url: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body {{ margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
        background: #A8FBD3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
.card {{ background: white; padding: 2rem 2.5rem; border-radius: 8px; max-width: 440px;
         text-align: center; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
h1 {{ margin: 0 0 0.75rem; font-size: 1.4rem; color: #333; }}
p {{ color: #666; font-size: 0.95rem; line-height: 1.5; margin: 0.5rem 0; }}
code {{ background: #f1f1f3; padding: 0.15rem 0.4rem; border-radius: 4px; word-break: break-all; }}
button {{ margin-top: 1rem; padding: 0.6rem 1.5rem; border: none; border-radius: 6px;
          background: #637AB9; color: white; font-size: 0.95rem; cursor: pointer; }}
</style></head>
<body>
<div class="card">
<h1>BrowseTerm</h1>
<p>Can't reach the BrowseTerm server at<br><code>{target_url}</code></p>
<p>Make sure it's running, or set <code>BROWSETERM_LOCAL_URL</code> to point at it.</p>
<button onclick="window.pywebview.api.retry_login()">Retry</button>
</div>
</body></html>"""


class DesktopApp:
    def __init__(self):
        self._state = load_state()
        self._keychain = KeychainStorage()
        self._authenticated = False
        self._login_in_progress = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._api = Api(
            self._state, self._keychain,
            on_logout=self._handle_logout, on_retry_login=self._handle_retry_login,
            on_start_login=self._handle_start_login,
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
        if self._authenticated:
            self._start_heartbeat()
        webview.start()

    def _resolve_start_kwargs(self) -> dict:
        if self._state.device_id and self._device_token_is_valid():
            self._authenticated = True
            return {"url": _APP_HTML}
        if _backend_reachable(BROWSETERM_LOCAL_URL):
            return {"url": _LOGIN_START_HTML}
        return {"html": _connection_error_html(BROWSETERM_LOCAL_URL)}

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
        if _backend_reachable(BROWSETERM_LOCAL_URL):
            self._window.load_url(_LOGIN_START_HTML)
        else:
            self._window.load_html(_connection_error_html(BROWSETERM_LOCAL_URL))

    def _handle_start_login(self) -> None:
        '''Kicks off the system-browser login flow (see module docstring) in a background
        thread - the loopback server blocks waiting for the callback, and this must never block
        the WebView's own event loop.'''
        if self._login_in_progress:
            return
        self._login_in_progress = True
        threading.Thread(target=self._run_login_flow, daemon=True).start()

    def _run_login_flow(self) -> None:
        try:
            server = LoopbackServer()
            server.start()
            login_url = (
                f"{BROWSETERM_LOCAL_URL}/login?"
                f"{urlencode({'target': 'desktop', 'desktop_port': server.port})}"
            )
            webbrowser.open(login_url)
            bootstrap_code = server.wait_for_code(DESKTOP_LOGIN_TIMEOUT_SECONDS)
            if not bootstrap_code:
                self._show_login_error("Login timed out or was cancelled. Please try again.")
                return
            result = redeem_device_bootstrap(bootstrap_code, detect_hardware())
        except (CloudClientError, httpx.HTTPError) as e:
            message = getattr(e, "message", str(e))
            self._show_login_error(message)
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
        self._start_heartbeat()

    def _show_login_error(self, message: str) -> None:
        if self._window is None:
            return
        self._window.evaluate_js(
            "document.getElementById('status').textContent = " + json.dumps(message) + ";"
            "document.getElementById('loginBtn').disabled = false;"
        )

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(DEVICE_HEARTBEAT_INTERVAL_SECONDS):
            token = self._keychain.get_device_token()
            if not token or not self._state.device_id:
                continue
            client = CloudClient(device_token=token)
            try:
                client.heartbeat(self._state.device_id)
            except CloudClientError as e:
                if e.is_auth_failure:
                    self._handle_device_credential_expired()
                    return
                # transient/network failure -- try again next interval

    def _handle_device_credential_expired(self) -> None:
        self._authenticated = False
        self._keychain.delete_device_token()
        self._state.clear()
        self._go_to_login_start()

    def _handle_logout(self) -> None:
        '''Clears the device credential and returns to the login-start page. Does NOT call
        Local's /logout: this app never holds Local's session cookie at all (the whole login flow
        happens in the system browser, in its own cookie jar this process never touches) - see the
        module docstring. Any lingering browser session there is left to expire on its own TTL
        rather than being explicitly revoked - a documented trade-off, not an oversight.'''
        self._heartbeat_stop.set()
        self._heartbeat_thread = None
        self._heartbeat_stop = threading.Event()
        self._authenticated = False
        self._keychain.delete_device_token()
        self._state.clear()
        self._go_to_login_start()


def run() -> None:
    DesktopApp().run()

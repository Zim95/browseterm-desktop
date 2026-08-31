"""
Desktop app orchestration: owns the pywebview window and is the only place that navigates it.

P07 (see ~/browseterm/p07.md) rewrite: the WebView shows Local's real `/login` page and shares
Local's own login flow exactly like a browser tab would (no separate Desktop OAuth
implementation) - Google/GitHub OAuth now happens entirely on Cloud, Local's part is just
redirecting there and later redeeming a one-time handoff code. Once that flow lands back on
Local's home page, this app does NOT keep the resulting session cookie around: it uses it exactly
once, immediately, to bootstrap a separate, long-lived device credential (`_bootstrap_device`) via
Local's `/device/bootstrap` (session-cookie authenticated) + Cloud's public
`/auth/device-bootstrap/redeem` - that device token is what's actually persisted, in macOS
Keychain (desktop/keychain.py), and every Device API call after that uses
`Authorization: Bearer <device_token>`, never the browser session cookie (p07.md section 20).

Consequence for restart behavior: "am I logged in" is now "does Keychain hold a valid device
token" - independent of whether the browser/WebView session cookie is still valid. On startup, a
valid Keychain token skips the WebView login entirely and goes straight to the Device page;
Local's browser session, no longer being used once the WebView is swapped away, is simply left to
expire on its own TTL rather than explicitly kept alive or revoked (see `_handle_logout`'s
docstring for the same trade-off on logout).

Unreachable Local: pywebview has no "failed to load" event to react to (only `loaded`, which
simply never fires on a failed navigation), so a bad `BROWSETERM_LOCAL_URL` or Local just not
running would otherwise leave the window showing nothing but its raw `background_color` forever,
with no explanation. `_backend_reachable` probes Local with a short timeout *before* the window
ever tries to load it, so that failure mode always shows a real, actionable error page
(`_connection_error_html`, with a Retry button) instead of a silent blank screen.
"""
import os
import threading
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
import webview

from desktop.api import Api
from desktop.cloud_client import CloudClient, CloudClientError, redeem_device_bootstrap
from desktop.config import (
    BROWSETERM_LOCAL_URL,
    CSRF_COOKIE_NAME,
    DEVICE_HEARTBEAT_INTERVAL_SECONDS,
    SESSION_COOKIE_NAME,
)
from desktop.device_info import detect_hardware
from desktop.keychain import KeychainStorage
from desktop.state import load_state

_APP_HTML = os.path.join(os.path.dirname(__file__), "web", "app.html")
_LOGIN_URL = f"{BROWSETERM_LOCAL_URL}/login"
_LOCAL_NETLOC = urlparse(BROWSETERM_LOCAL_URL).netloc
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
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._api = Api(
            self._state, self._keychain, on_logout=self._handle_logout, on_retry_login=self._handle_retry_login
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
        self._window.events.loaded += self._on_loaded
        if self._authenticated:
            self._start_heartbeat()
        webview.start()

    def _resolve_start_kwargs(self) -> dict:
        if self._state.device_id and self._device_token_is_valid():
            self._authenticated = True
            return {"url": _APP_HTML}
        if _backend_reachable(_LOGIN_URL):
            return {"url": _LOGIN_URL}
        return {"html": _connection_error_html(_LOGIN_URL)}

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
        self._go_to_login()

    def _go_to_login(self) -> None:
        if self._window is None:
            return
        if _backend_reachable(_LOGIN_URL):
            self._window.load_url(_LOGIN_URL)
        else:
            self._window.load_html(_connection_error_html(_LOGIN_URL))

    def _on_loaded(self) -> None:
        if self._authenticated:
            return
        current_url = self._window.get_current_url() or ""
        parsed = urlparse(current_url)
        if parsed.netloc != _LOCAL_NETLOC or parsed.path != "/":
            return  # not yet past OAuth back to Local's home page

        session_cookie, csrf_token = self._extract_cookies()
        if not session_cookie:
            return
        try:
            self._bootstrap_device(session_cookie, csrf_token)
        except (CloudClientError, httpx.HTTPError) as e:
            message = getattr(e, "message", str(e))
            self._window.load_url(f"{_LOGIN_URL}?{urlencode({'auth_result': 'error', 'error_message': message})}")
            return

        self._authenticated = True
        self._window.load_url(_APP_HTML)
        self._start_heartbeat()

    def _bootstrap_device(self, session_cookie: str, csrf_token: Optional[str]) -> None:
        '''Session-cookie -> one-time bootstrap code (Local) -> device token (Cloud). Raises
        CloudClientError/httpx.HTTPError on any failure - caller decides what to show.'''
        headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}
        response = httpx.post(
            f"{BROWSETERM_LOCAL_URL}/device/bootstrap",
            cookies={SESSION_COOKIE_NAME: session_cookie},
            headers=headers,
            timeout=10.0,
        )
        if response.status_code != 200:
            message = response.json().get("error", response.text) if response.content else response.reason_phrase
            raise CloudClientError(response.status_code, message)
        bootstrap_code = response.json()["code"]

        result = redeem_device_bootstrap(bootstrap_code, detect_hardware())
        self._keychain.set_device_token(result["device_token"])
        self._state.device_id = result["device"]["id"]
        self._state.device_name = result["device"]["device_name"]
        self._state.save()

    def _extract_cookies(self) -> tuple[Optional[str], Optional[str]]:
        try:
            cookies = self._window.get_cookies()
        except Exception:
            return None, None
        session_value, csrf_value = None, None
        for cookie in cookies:
            try:
                items = cookie.items()
            except AttributeError:
                continue
            for name, morsel in items:
                if name == SESSION_COOKIE_NAME:
                    session_value = morsel.value
                elif name == CSRF_COOKIE_NAME:
                    csrf_value = morsel.value
        return session_value, csrf_value

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
        self._go_to_login()

    def _handle_logout(self) -> None:
        '''Clears the device credential and returns to login. Does NOT call Local's /logout: by
        the time app.html is showing, this app no longer holds Local's session cookie (P07 -
        it was only ever used transiently for bootstrap, see the module docstring), so there is
        nothing to send that endpoint. Any lingering browser session is left to expire on its own
        TTL rather than being explicitly revoked - a documented trade-off, not an oversight.'''
        self._heartbeat_stop.set()
        self._heartbeat_thread = None
        self._heartbeat_stop = threading.Event()
        self._authenticated = False
        self._keychain.delete_device_token()
        self._state.clear()
        self._go_to_login()


def run() -> None:
    DesktopApp().run()

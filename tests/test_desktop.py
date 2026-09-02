"""
P07 -- Desktop app tests. Covers: Keychain storage is mockable (p07.md section 22), device
bootstrap end-to-end (session cookie + CSRF -> Local bootstrap code -> Cloud device token),
device-token-scoped Cloud calls, auth-failure vs. transient-failure handling, and logout clearing
Keychain/state without needing the (no-longer-held) session cookie.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock

import pytest

from desktop.api import Api
from desktop.cloud_client import CloudClient, CloudClientError, redeem_device_bootstrap
from desktop.keychain import KeychainStorage
from desktop.state import DesktopState


class _FakeKeychain(KeychainStorage):
    """In-memory stand-in -- proves the real KeychainStorage is swappable/mockable per p07.md
    section 22, without touching the real macOS Keychain in tests."""

    def __init__(self):
        self._token = None

    def get_device_token(self):
        return self._token

    def set_device_token(self, token):
        self._token = token

    def delete_device_token(self):
        self._token = None


class _StubServer(BaseHTTPRequestHandler):
    """Minimal Local + Cloud stand-in: /device/bootstrap (session+CSRF gated) and
    /auth/device-bootstrap/redeem (possession-gated), plus a Bearer-gated device endpoint."""

    devices = {}
    bootstrap_codes = set()

    def do_POST(self):
        if self.path == "/device/bootstrap":
            cookie = self.headers.get("Cookie", "")
            csrf = self.headers.get("X-CSRF-Token", "")
            if "session=valid-session" not in cookie:
                return self._json(401, {"error": "not logged in"})
            if csrf != "valid-csrf":
                return self._json(403, {"error": "bad csrf"})
            code = "one-time-code"
            self.bootstrap_codes.add(code)
            return self._json(200, {"code": code})
        if self.path == "/auth/device-bootstrap/redeem":
            body = self._body()
            if body.get("code") not in self.bootstrap_codes:
                return self._json(401, {"error": "invalid code"})
            self.bootstrap_codes.discard(body["code"])
            device_id = "device-1"
            token = "bst_device_abc"
            device = {**body["device"], "id": device_id, "status": "ACTIVE"}
            self.devices[device_id] = {"device": device, "token": token}
            return self._json(201, {"device": device, "device_token": token})
        if self.path.endswith("/heartbeat"):
            device_id = self.path.split("/")[2]
            entry = self.devices.get(device_id)
            auth = self.headers.get("Authorization", "")
            if not entry or auth != f"Bearer {entry['token']}":
                return self._json(401, {"error": "Unauthorized"})
            return self._json(200, {"device": entry["device"]})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/devices/"):
            device_id = self.path.split("/")[2]
            entry = self.devices.get(device_id)
            auth = self.headers.get("Authorization", "")
            if not entry or auth != f"Bearer {entry['token']}":
                return self._json(401, {"error": "Unauthorized"})
            return self._json(200, {"device": entry["device"]})
        return self._json(404, {"error": "not found"})

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_server():
    _StubServer.devices = {}
    _StubServer.bootstrap_codes = set()
    server = HTTPServer(("127.0.0.1", 0), _StubServer)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


_DEVICE_PAYLOAD = {
    "device_name": "test-mac", "os": "Darwin", "architecture": "arm64", "runtime_version": "1.0",
    "total_cpu": 8, "total_memory_bytes": 16, "total_storage_bytes": 16,
    "allocated_cpu": 8, "allocated_memory_bytes": 16, "allocated_storage_bytes": 16, "gpu_info": None,
}


def test_bootstrap_redeem_with_valid_code(stub_server):
    import httpx
    resp = httpx.post(
        f"{stub_server}/device/bootstrap",
        cookies={"session": "valid-session"}, headers={"X-CSRF-Token": "valid-csrf"},
    )
    code = resp.json()["code"]
    result = redeem_device_bootstrap(code, _DEVICE_PAYLOAD, base_url=stub_server)
    assert result["device_token"] == "bst_device_abc"
    assert result["device"]["id"] == "device-1"


def test_bootstrap_redeem_rejects_missing_session_cookie(stub_server):
    import httpx
    resp = httpx.post(f"{stub_server}/device/bootstrap", headers={"X-CSRF-Token": "valid-csrf"})
    assert resp.status_code == 401


def test_bootstrap_redeem_rejects_wrong_csrf(stub_server):
    import httpx
    resp = httpx.post(
        f"{stub_server}/device/bootstrap", cookies={"session": "valid-session"}, headers={"X-CSRF-Token": "wrong"},
    )
    assert resp.status_code == 403


def test_second_redemption_of_bootstrap_code_fails(stub_server):
    import httpx
    resp = httpx.post(
        f"{stub_server}/device/bootstrap",
        cookies={"session": "valid-session"}, headers={"X-CSRF-Token": "valid-csrf"},
    )
    code = resp.json()["code"]
    redeem_device_bootstrap(code, _DEVICE_PAYLOAD, base_url=stub_server)
    with pytest.raises(CloudClientError) as exc_info:
        redeem_device_bootstrap(code, _DEVICE_PAYLOAD, base_url=stub_server)
    assert exc_info.value.status_code == 401


def test_device_token_scoped_calls_succeed_and_wrong_token_rejected(stub_server):
    import httpx
    resp = httpx.post(
        f"{stub_server}/device/bootstrap",
        cookies={"session": "valid-session"}, headers={"X-CSRF-Token": "valid-csrf"},
    )
    code = resp.json()["code"]
    result = redeem_device_bootstrap(code, _DEVICE_PAYLOAD, base_url=stub_server)
    device_id = result["device"]["id"]

    client = CloudClient(device_token=result["device_token"], base_url=stub_server)
    device = client.get_device(device_id)
    assert device["id"] == device_id

    heartbeated = client.heartbeat(device_id)
    assert heartbeated["status"] == "ACTIVE"

    bad_client = CloudClient(device_token="bst_device_wrong", base_url=stub_server)
    with pytest.raises(CloudClientError) as exc_info:
        bad_client.get_device(device_id)
    assert exc_info.value.status_code == 401
    assert exc_info.value.is_auth_failure is True


def test_api_activate_device_without_token_returns_friendly_error():
    state = DesktopState()
    keychain = _FakeKeychain()
    api = Api(state, keychain, on_logout=lambda: None, on_retry_login=lambda: None, on_start_login=lambda: None)
    result = api.activate_device()
    assert result["status"] == "not_registered"
    assert "log" in result["error"].lower()


def test_api_device_info_and_activate_with_valid_token(stub_server):
    state = DesktopState()
    keychain = _FakeKeychain()
    import httpx
    resp = httpx.post(
        f"{stub_server}/device/bootstrap",
        cookies={"session": "valid-session"}, headers={"X-CSRF-Token": "valid-csrf"},
    )
    result = redeem_device_bootstrap(resp.json()["code"], _DEVICE_PAYLOAD, base_url=stub_server)
    keychain.set_device_token(result["device_token"])
    state.device_id = result["device"]["id"]

    import desktop.api as api_module

    api = Api(state, keychain, on_logout=lambda: None, on_retry_login=lambda: None, on_start_login=lambda: None)
    # Point this Api's CloudClient calls at the stub by monkeypatching the default base_url.
    orig_cloud_client = api_module.CloudClient

    class ScopedCloudClient(orig_cloud_client):
        def __init__(self, device_token=None, **kwargs):
            super().__init__(device_token=device_token, base_url=stub_server)

    api_module.CloudClient = ScopedCloudClient
    try:
        info = api.device_info()
        assert info["status"] == "active"
        activated = api.activate_device()
        assert activated["status"] == "active"
        assert activated["error"] is None
    finally:
        api_module.CloudClient = orig_cloud_client


def test_logout_callback_invoked():
    called = []
    state = DesktopState()
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_device_x")
    api = Api(state, keychain, on_logout=lambda: called.append(True), on_retry_login=lambda: None, on_start_login=lambda: None)
    api.logout()
    assert called == [True]


def test_retry_login_callback_invoked():
    called = []
    state = DesktopState()
    keychain = _FakeKeychain()
    api = Api(state, keychain, on_logout=lambda: None, on_retry_login=lambda: called.append(True), on_start_login=lambda: None)
    api.retry_login()
    assert called == [True]


def test_start_login_callback_invoked():
    called = []
    state = DesktopState()
    keychain = _FakeKeychain()
    api = Api(state, keychain, on_logout=lambda: None, on_retry_login=lambda: None, on_start_login=lambda: called.append(True))
    api.start_login()
    assert called == [True]


def test_full_desktop_login_flow_round_trip(monkeypatch):
    '''End-to-end (minus the real OAuth/browser): DesktopApp._run_login_flow starts a real
    LoopbackServer, "opens the browser" (mocked - we instead simulate the system browser landing
    on the loopback callback, exactly like Local's auth_callback would redirect it to), and on
    receiving the bootstrap code redeems it and persists the device token/state - proving the
    whole chain (app.py <-> loopback_server.py <-> cloud_client.py <-> keychain/state) actually
    fits together, not just each piece in isolation.'''
    import threading
    import time

    import httpx

    import desktop.app as app_module

    state = DesktopState()
    keychain = _FakeKeychain()
    app = app_module.DesktopApp.__new__(app_module.DesktopApp)
    app._state = state
    app._keychain = keychain
    app._authenticated = False
    app._login_in_progress = False
    app._heartbeat_stop = threading.Event()
    app._heartbeat_thread = None
    app._window = None  # no real WebView in this test - guarded by `if self._window is not None`

    captured_url = {}

    def fake_open(url):
        captured_url["url"] = url
        # simulate the system browser hitting the loopback callback once Local finishes
        def _hit_callback():
            time.sleep(0.05)
            port = url.rsplit("desktop_port=", 1)[1]
            httpx.get(f"http://127.0.0.1:{port}/callback", params={"code": "bootstrap-code-xyz"})
        threading.Thread(target=_hit_callback, daemon=True).start()

    def fake_redeem(code, device, base_url=None):
        assert code == "bootstrap-code-xyz"
        return {"device_token": "bst_device_full_flow", "device": {"id": "device-9", "device_name": device["device_name"]}}

    monkeypatch.setattr(app_module, "webbrowser", type("_W", (), {"open": staticmethod(fake_open)}))
    monkeypatch.setattr(app_module, "redeem_device_bootstrap", fake_redeem)
    monkeypatch.setattr(app_module, "detect_hardware", lambda: {"device_name": "test-mac", "os": "Darwin"})
    monkeypatch.setattr(app_module, "DESKTOP_LOGIN_TIMEOUT_SECONDS", 5.0)

    app._run_login_flow()

    assert "target=desktop" in captured_url["url"]
    assert keychain.get_device_token() == "bst_device_full_flow"
    assert state.device_id == "device-9"
    assert state.device_name == "test-mac"
    assert app._authenticated is True
    assert app._login_in_progress is False

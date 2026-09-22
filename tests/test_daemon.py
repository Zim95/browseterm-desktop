"""
desktop/daemon.py -- the headless heartbeat + cluster health-check loops. Tests call the `_once`
methods directly rather than running the real threaded loops (which sleep on
DEVICE_HEARTBEAT_INTERVAL_SECONDS/DAEMON_HEALTH_CHECK_INTERVAL_SECONDS) - `run()`/the loop wrapper
methods are just `while not stop.wait(interval): _once()`, not worth re-testing separately.
"""
import threading

from desktop import cluster_manager
from desktop.cloud_client import CloudClientError
from desktop.daemon import Daemon
from desktop.keychain import KeychainStorage
from desktop.state import DesktopState


class _FakeKeychain(KeychainStorage):
    def __init__(self):
        self._token = None

    def get_device_token(self):
        return self._token

    def set_device_token(self, token):
        self._token = token

    def delete_device_token(self):
        self._token = None


def _daemon_with_keychain(keychain):
    d = Daemon.__new__(Daemon)
    d._keychain = keychain
    d._stop = threading.Event()
    d._threads = []
    return d


def test_heartbeat_skips_when_no_token(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    called = []
    monkeypatch.setattr("desktop.daemon.CloudClient", lambda **kw: (_ for _ in ()).throw(AssertionError("should not construct a client with no token")))
    d = _daemon_with_keychain(_FakeKeychain())
    d._heartbeat_once()  # no token -- must not raise, must not call Cloud


def test_heartbeat_skips_when_no_device_id(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id=None))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    d = _daemon_with_keychain(keychain)
    d._heartbeat_once()  # no device_id -- must not raise


def test_heartbeat_calls_cloud_when_logged_in(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    calls = []

    class _FakeClient:
        def __init__(self, device_token):
            calls.append(device_token)

        def heartbeat(self, device_id):
            calls.append(device_id)
            return {"status": "ACTIVE"}

    monkeypatch.setattr("desktop.daemon.CloudClient", _FakeClient)
    d = _daemon_with_keychain(keychain)
    d._heartbeat_once()
    assert calls == ["bst_token", "dev-1"]


def test_heartbeat_clears_credential_on_auth_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")

    class _FailingClient:
        def __init__(self, device_token):
            pass

        def heartbeat(self, device_id):
            raise CloudClientError(401, "revoked")

    monkeypatch.setattr("desktop.daemon.CloudClient", _FailingClient)
    d = _daemon_with_keychain(keychain)
    d._heartbeat_once()
    assert keychain.get_device_token() is None


def test_heartbeat_keeps_credential_on_transient_failure(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")

    class _FlakyClient:
        def __init__(self, device_token):
            pass

        def heartbeat(self, device_id):
            raise CloudClientError(0, "connection refused")

    monkeypatch.setattr("desktop.daemon.CloudClient", _FlakyClient)
    d = _daemon_with_keychain(keychain)
    d._heartbeat_once()
    assert keychain.get_device_token() == "bst_token"  # not cleared -- try again next interval


def test_health_check_skips_when_not_logged_in(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id=None))
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: (_ for _ in ()).throw(AssertionError("must not check cluster when logged out")))
    d = _daemon_with_keychain(_FakeKeychain())
    d._health_check_once()


def test_health_check_skips_when_no_cluster(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: False)
    monkeypatch.setattr(cluster_manager, "vm_state", lambda: (_ for _ in ()).throw(AssertionError("must not check VM state when no cluster exists")))
    d = _daemon_with_keychain(keychain)
    d._health_check_once()


def test_health_check_starts_stopped_vm(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: True)
    monkeypatch.setattr(cluster_manager, "vm_state", lambda: "Stopped")
    started = []
    monkeypatch.setattr(cluster_manager, "start_vm", lambda: started.append(True))
    monkeypatch.setattr(cluster_manager, "list_pods", lambda: (_ for _ in ()).throw(AssertionError("must not list pods in the same tick a restart was triggered")))
    d = _daemon_with_keychain(keychain)
    d._health_check_once()
    assert started == [True]


def test_health_check_restarts_crashing_pods_only(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: True)
    monkeypatch.setattr(cluster_manager, "vm_state", lambda: "Running")
    monkeypatch.setattr(cluster_manager, "list_pods", lambda: [
        {"namespace": "browseterm", "name": "container-maker-abc", "crashing": False},
        {"namespace": "browseterm", "name": "status-monitor-def", "crashing": True},
    ])
    restarted = []
    monkeypatch.setattr(cluster_manager, "restart_pod", lambda ns, name: restarted.append((ns, name)))
    d = _daemon_with_keychain(keychain)
    d._health_check_once()
    assert restarted == [("browseterm", "status-monitor-def")]


def test_health_check_swallows_cluster_errors(monkeypatch):
    monkeypatch.setattr("desktop.daemon.load_state", lambda: DesktopState(device_id="dev-1"))
    keychain = _FakeKeychain()
    keychain.set_device_token("bst_token")
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: True)

    def raise_error():
        raise cluster_manager.ClusterError("multipass not found")

    monkeypatch.setattr(cluster_manager, "vm_state", raise_error)
    d = _daemon_with_keychain(keychain)
    d._health_check_once()  # must not raise -- tries again next interval

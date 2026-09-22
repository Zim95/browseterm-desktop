"""
Headless Linux CLI (Part 18) - mocks desktop.native_k3s/local_stack/cloud_client directly (their
own real subprocess/HTTP calls are covered by test_native_k3s.py/test_local_stack.py and
test_desktop.py's own login-flow tests), so what's under test here is cli.py's own command
wiring, allocation validation, and the headless device-linking poll loop.

Every test monkeypatches DesktopState.save to a no-op, same convention test_api_cluster.py
already established - a real .save() would write to this machine's real
~/.browseterm/desktop_state.json.
"""
import pytest

from desktop import cli
from desktop.cli import CliError
from desktop.cloud_client import CloudClientError
from desktop.linux_credential_store import LinuxFileCredentialStore
from desktop.state import DesktopState

_HARDWARE = {
    "device_name": "test-linux", "os": "Linux", "architecture": "x86_64", "runtime_version": "6.8.0",
    "total_cpu": 8, "total_memory_bytes": 16 * 1024 ** 3, "total_storage_bytes": 400 * 1024 ** 3,
    "gpu_info": None,
}


class _FakeCredentialStore(LinuxFileCredentialStore):
    def __init__(self, token=None):
        self._token = token

    def get_device_token(self):
        return self._token

    def set_device_token(self, token):
        self._token = token

    def delete_device_token(self):
        self._token = None


@pytest.fixture(autouse=True)
def _no_real_disk_writes(monkeypatch):
    monkeypatch.setattr(DesktopState, "save", lambda self: None)


@pytest.fixture(autouse=True)
def _fake_hardware(monkeypatch):
    monkeypatch.setattr(cli, "detect_hardware", lambda: dict(_HARDWARE))


@pytest.fixture(autouse=True)
def _as_root(monkeypatch):
    monkeypatch.setattr(cli.native_k3s.os, "geteuid", lambda: 0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)


# ---------- _parse_size / _validate_allocation ----------

def test_parse_size_accepts_gi_suffix():
    assert cli._parse_size("8Gi") == 8.0
    assert cli._parse_size("50G") == 50.0
    assert cli._parse_size("4") == 4.0


def test_validate_allocation_rejects_over_capacity():
    with pytest.raises(CliError, match="--cpus"):
        cli._validate_allocation(99, 8, 50, _HARDWARE)
    with pytest.raises(CliError, match="--memory"):
        cli._validate_allocation(4, 9999, 50, _HARDWARE)
    with pytest.raises(CliError, match="--storage"):
        cli._validate_allocation(4, 8, 99999, _HARDWARE)


def test_validate_allocation_accepts_within_capacity():
    cli._validate_allocation(4, 8, 50, _HARDWARE)  # must not raise


# ---------- ensure_login ----------

def test_ensure_login_returns_existing_token_without_calling_cloud(monkeypatch):
    called = {"start": False}
    monkeypatch.setattr(cli, "start_device_login", lambda provider="google": called.__setitem__("start", True) or {})
    store = _FakeCredentialStore(token="existing-token")
    token = cli.ensure_login(DesktopState(), store)
    assert token == "existing-token"
    assert called["start"] is False


def test_ensure_login_runs_full_device_grant_flow_to_completion(monkeypatch, capsys):
    monkeypatch.setattr(cli, "start_device_login", lambda provider="google": {
        "device_code": "dc-1", "user_code": "ABCD-EFGH",
        "verification_uri": "https://app.browseterm.puhtaeto.com/device/link",
        "interval": 1, "expires_in": 60,
    })
    polls = iter([
        {"status": "pending"},
        {"status": "complete", "device_token": "bst_device_new", "device": {"id": "dev-1", "device_name": "test-linux"}},
    ])
    monkeypatch.setattr(cli, "poll_device_login", lambda *a, **kw: next(polls))

    state = DesktopState()
    store = _FakeCredentialStore()
    token = cli.ensure_login(state, store)

    assert token == "bst_device_new"
    assert store.get_device_token() == "bst_device_new"
    assert state.device_id == "dev-1"
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out


def test_ensure_login_slow_down_increases_interval_without_erroring(monkeypatch):
    monkeypatch.setattr(cli, "start_device_login", lambda provider="google": {
        "device_code": "dc-1", "user_code": "ABCD-EFGH", "verification_uri": "https://x", "interval": 1, "expires_in": 60,
    })
    polls = iter([
        {"status": "pending", "slow_down": True},
        {"status": "complete", "device_token": "tok", "device": {"id": "dev-1", "device_name": "n"}},
    ])
    monkeypatch.setattr(cli, "poll_device_login", lambda *a, **kw: next(polls))
    token = cli.ensure_login(DesktopState(), _FakeCredentialStore())
    assert token == "tok"


@pytest.mark.parametrize("status,expected_message", [
    ("expired", "expired"),
    ("denied", "denied"),
])
def test_ensure_login_terminal_statuses_raise_cli_error(monkeypatch, status, expected_message):
    monkeypatch.setattr(cli, "start_device_login", lambda provider="google": {
        "device_code": "dc-1", "user_code": "ABCD-EFGH", "verification_uri": "https://x", "interval": 1, "expires_in": 60,
    })
    monkeypatch.setattr(cli, "poll_device_login", lambda *a, **kw: {"status": status})
    with pytest.raises(CliError, match=expected_message):
        cli.ensure_login(DesktopState(), _FakeCredentialStore())


def test_ensure_login_start_failure_raises_cli_error(monkeypatch):
    def boom(provider="google"):
        raise CloudClientError(500, "cloud is down")
    monkeypatch.setattr(cli, "start_device_login", boom)
    with pytest.raises(CliError, match="cloud is down"):
        cli.ensure_login(DesktopState(), _FakeCredentialStore())


def test_ensure_login_times_out(monkeypatch):
    monkeypatch.setattr(cli, "start_device_login", lambda provider="google": {
        "device_code": "dc-1", "user_code": "ABCD-EFGH", "verification_uri": "https://x", "interval": 1, "expires_in": 60,
    })
    monkeypatch.setattr(cli, "poll_device_login", lambda *a, **kw: {"status": "pending"})
    times = iter([0, 30, 61, 9999])  # monotonic() called once up front then once per loop iteration
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    with pytest.raises(CliError, match="timed out"):
        cli.ensure_login(DesktopState(), _FakeCredentialStore())


# ---------- cmd_setup ----------

def _args(**overrides):
    defaults = {"cpus": None, "memory": None, "storage": None, "non_interactive": False, "provider": "google"}
    defaults.update(overrides)
    return type("Args", (), defaults)()


def test_cmd_setup_non_interactive_requires_all_three_sizes(monkeypatch):
    monkeypatch.setattr(cli.local_stack, "check_prerequisites", lambda: None)
    monkeypatch.setattr(cli, "load_state", lambda: DesktopState())
    monkeypatch.setattr(cli, "LinuxFileCredentialStore", lambda: _FakeCredentialStore(token="tok"))
    with pytest.raises(CliError, match="non-interactive requires"):
        cli.cmd_setup(_args(non_interactive=True, cpus=4))


def test_cmd_setup_non_interactive_happy_path_creates_cluster_and_deploys(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.local_stack, "check_prerequisites", lambda: None)
    monkeypatch.setattr(cli.local_stack, "deploy", lambda device_id, token, on_step=None: calls.append(("deploy", device_id, token)))
    monkeypatch.setattr(cli.native_k3s, "create_cluster", lambda cpu, mem, storage, on_step=None: calls.append(("create_cluster", cpu, mem, storage)))
    monkeypatch.setattr(cli, "load_state", lambda: DesktopState(device_id="dev-1"))
    monkeypatch.setattr(cli, "LinuxFileCredentialStore", lambda: _FakeCredentialStore(token="tok"))

    result = cli.cmd_setup(_args(non_interactive=True, cpus=4, memory="8Gi", storage="50Gi"))

    assert result == 0
    assert ("create_cluster", 4, 8.0, 50.0) in calls
    assert ("deploy", "dev-1", "tok") in calls


def test_cmd_setup_requires_root(monkeypatch):
    monkeypatch.setattr(cli.native_k3s.os, "geteuid", lambda: 501)
    with pytest.raises(CliError, match="root"):
        cli.cmd_setup(_args(non_interactive=True, cpus=4, memory="8Gi", storage="50Gi"))


# ---------- cmd_status / cmd_logs ----------

def test_cmd_status_reports_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(cli.native_k3s, "cluster_exists", lambda: False)
    assert cli.cmd_status(_args()) == 0
    assert "Cluster installed: False" in capsys.readouterr().out


def test_cmd_status_lists_pods(monkeypatch, capsys):
    monkeypatch.setattr(cli.native_k3s, "cluster_exists", lambda: True)
    monkeypatch.setattr(cli.native_k3s, "list_pods", lambda: [
        {"namespace": "browseterm", "name": "container-maker-abc", "phase": "Running", "ready": "1/1", "restarts": 0, "crashing": False},
    ])
    assert cli.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "container-maker-abc" in out
    assert "Running" in out


def test_cmd_logs_pod_calls_pod_logs(monkeypatch, capsys):
    monkeypatch.setattr(cli.native_k3s, "pod_logs", lambda ns, pod, tail=200: f"logs for {pod}")
    args = type("Args", (), {"pod": "container-maker-abc", "namespace": "browseterm"})()
    assert cli.cmd_logs(args) == 0
    assert "logs for container-maker-abc" in capsys.readouterr().out


def test_cmd_logs_no_pod_calls_service_logs(monkeypatch, capsys):
    monkeypatch.setattr(cli.native_k3s, "service_logs", lambda: "service log output")
    args = type("Args", (), {"pod": None, "namespace": "browseterm"})()
    assert cli.cmd_logs(args) == 0
    assert "service log output" in capsys.readouterr().out


# ---------- cmd_activate ----------

def test_cmd_activate_requires_existing_credential(monkeypatch):
    monkeypatch.setattr(cli, "load_state", lambda: DesktopState())
    monkeypatch.setattr(cli, "LinuxFileCredentialStore", lambda: _FakeCredentialStore())
    with pytest.raises(CliError, match="setup"):
        cli.cmd_activate(_args())


def test_cmd_activate_heartbeats_with_existing_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: DesktopState(device_id="dev-1"))
    monkeypatch.setattr(cli, "LinuxFileCredentialStore", lambda: _FakeCredentialStore(token="tok"))

    class _FakeClient:
        def __init__(self, device_token=None):
            pass

        def heartbeat(self, device_id):
            return {"device_name": "test-linux"}

    monkeypatch.setattr(cli, "CloudClient", _FakeClient)
    assert cli.cmd_activate(_args()) == 0
    assert "test-linux" in capsys.readouterr().out


# ---------- cmd_uninstall ----------

def test_cmd_uninstall_preserves_state_by_default(monkeypatch):
    monkeypatch.setattr(cli.native_k3s, "delete_cluster", lambda: None)
    args = type("Args", (), {"purge_state": False})()
    assert cli.cmd_uninstall(args) == 0


def test_cmd_uninstall_purges_state_when_asked(monkeypatch):
    monkeypatch.setattr(cli.native_k3s, "delete_cluster", lambda: None)
    cleared = {"state": False, "token": False}
    fake_state = DesktopState(device_id="dev-1")
    monkeypatch.setattr(fake_state, "clear", lambda: cleared.__setitem__("state", True))
    monkeypatch.setattr(cli, "load_state", lambda: fake_state)
    fake_store = _FakeCredentialStore(token="tok")
    monkeypatch.setattr(fake_store, "delete_device_token", lambda: cleared.__setitem__("token", True))
    monkeypatch.setattr(cli, "LinuxFileCredentialStore", lambda: fake_store)

    args = type("Args", (), {"purge_state": True})()
    assert cli.cmd_uninstall(args) == 0
    assert cleared == {"state": True, "token": True}


# ---------- argparse wiring ----------

def test_build_parser_parses_setup_non_interactive():
    parser = cli._build_parser()
    args = parser.parse_args(["setup", "--non-interactive", "--cpus", "4", "--memory", "8Gi", "--storage", "50Gi"])
    assert args.command == "setup"
    assert args.cpus == 4
    assert args.memory == "8Gi"
    assert args.func == cli.cmd_setup


def test_build_parser_parses_logs_with_optional_pod():
    parser = cli._build_parser()
    args = parser.parse_args(["logs"])
    assert args.pod is None
    args = parser.parse_args(["logs", "container-maker-abc"])
    assert args.pod == "container-maker-abc"


def test_main_reports_cli_error_and_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_status", lambda args: (_ for _ in ()).throw(CliError("boom")))
    exit_code = cli.main(["status"])
    assert exit_code == 1
    assert "boom" in capsys.readouterr().err

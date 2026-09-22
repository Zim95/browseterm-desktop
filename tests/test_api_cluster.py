"""
Cluster section of the Device page -- desktop/api.py's Api.cluster_status/setup_cluster/
teardown_cluster/list_cluster_pods. These mock `desktop.api.cluster_manager` directly (its own
k3d/docker/kubectl shelling-out is covered by tests/test_cluster_manager.py) so what's under test
here is Api's own allocation defaulting/persistence/Cloud-sync logic instead.

Every test monkeypatches `DesktopState.save` to a no-op: a real `.save()` would write to this
machine's real `~/.browseterm/desktop_state.json`, which these tests must never touch.
"""
from unittest.mock import MagicMock

import pytest

from desktop import api as api_module
from desktop.api import Api
from desktop.cluster_manager import ClusterError
from desktop.keychain import KeychainStorage
from desktop.local_stack import LocalStackError
from desktop.state import DesktopState

_HARDWARE = {
    "device_name": "test-mac", "os": "Darwin", "architecture": "arm64", "runtime_version": "15.0",
    "total_cpu": 8, "total_memory_bytes": 16 * 1024 ** 3, "total_storage_bytes": 400 * 1024 ** 3,
    "gpu_info": None,
}


class _FakeKeychain(KeychainStorage):
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
    monkeypatch.setattr(api_module, "detect_hardware", lambda: dict(_HARDWARE))


@pytest.fixture(autouse=True)
def _fake_local_stack(monkeypatch):
    """local_stack.deploy() shells out to `make`/`kubectl`/`docker` across several sibling repos --
    every test in this file except the ones dedicated to this wiring itself treats it as a no-op,
    the same way cluster_manager's own real k3d/docker/kubectl calls are mocked per-test below."""
    monkeypatch.setattr(api_module.local_stack, "check_prerequisites", lambda: None)
    monkeypatch.setattr(api_module.local_stack, "deploy", lambda device_id, device_token=None, on_step=None: None)


def _make_api(state=None, keychain=None, on_setup_step=None):
    return Api(
        state or DesktopState(), keychain or _FakeKeychain(),
        on_logout=lambda: None, on_retry_login=lambda: None, on_start_login=lambda provider: None,
        on_setup_step=on_setup_step,
    )


def test_cluster_status_defaults_allocation_to_half_of_detected_totals(monkeypatch):
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: False)
    api = _make_api()
    status = api.cluster_status()
    assert status["allocated_cpu"] == 4
    assert status["allocated_memory_gb"] == 8
    assert status["allocated_storage_gb"] == 200
    assert status["total_cpu"] == 8
    assert status["total_memory_gb"] == 16
    assert status["total_storage_gb"] == 400
    assert status["cluster_exists"] is False
    assert status["error"] is None


def test_cluster_status_does_not_overwrite_a_previously_chosen_allocation(monkeypatch):
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: False)
    state = DesktopState(allocated_cpu=2, allocated_memory_gb=4, allocated_storage_gb=50)
    status = _make_api(state=state).cluster_status()
    assert (status["allocated_cpu"], status["allocated_memory_gb"], status["allocated_storage_gb"]) == (2, 4, 50)


def test_cluster_status_surfaces_cluster_manager_error(monkeypatch):
    def raise_error():
        raise ClusterError("k3d not found -- is it installed and on PATH?")
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", raise_error)
    status = _make_api().cluster_status()
    assert status["cluster_exists"] is False
    assert "k3d not found" in status["error"]


def test_setup_cluster_saves_allocation_and_creates_cluster(monkeypatch):
    create_calls = []
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: create_calls.append((cpu, mem)))
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)

    state = DesktopState()
    status = _make_api(state=state).setup_cluster(cpu=3, memory_gb=6, storage_gb=100)

    assert create_calls == [(3, 6)]
    assert (state.allocated_cpu, state.allocated_memory_gb, state.allocated_storage_gb) == (3, 6, 100)
    assert status["cluster_exists"] is True
    assert status["allocated_cpu"] == 3


def test_setup_cluster_returns_error_but_keeps_saved_allocation_on_failure(monkeypatch):
    def raise_error(cpu, mem, storage=None, on_step=None):
        raise ClusterError("k3d cluster create failed")
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", raise_error)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: False)

    state = DesktopState()
    status = _make_api(state=state).setup_cluster(cpu=3, memory_gb=6, storage_gb=100)

    assert state.allocated_cpu == 3  # allocation is a local preference, saved regardless
    assert status["cluster_exists"] is False
    assert "k3d cluster create failed" in status["error"]


def test_setup_cluster_syncs_allocation_to_cloud_when_device_is_registered(monkeypatch):
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: None)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)

    fake_client = MagicMock()
    monkeypatch.setattr(api_module, "CloudClient", lambda **kwargs: fake_client)

    state = DesktopState(device_id="device-1")
    keychain = _FakeKeychain(token="bst_device_abc")
    _make_api(state=state, keychain=keychain).setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    fake_client.update_device.assert_called_once_with("device-1", {
        "allocated_cpu": 2,
        "allocated_memory_bytes": 4 * 1024 ** 3,
        "allocated_storage_bytes": 50 * 1024 ** 3,
    })


def test_setup_cluster_cloud_sync_failure_does_not_block_local_setup(monkeypatch):
    from desktop.cloud_client import CloudClientError

    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: None)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)

    fake_client = MagicMock()
    fake_client.update_device.side_effect = CloudClientError(500, "Cloud is down")
    monkeypatch.setattr(api_module, "CloudClient", lambda **kwargs: fake_client)

    state = DesktopState(device_id="device-1")
    keychain = _FakeKeychain(token="bst_device_abc")
    status = _make_api(state=state, keychain=keychain).setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    assert status["cluster_exists"] is True
    assert status["error"] is None


def test_setup_cluster_skips_cloud_sync_when_device_not_registered(monkeypatch):
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: None)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)
    cloud_client_ctor = MagicMock()
    monkeypatch.setattr(api_module, "CloudClient", cloud_client_ctor)

    _make_api().setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    cloud_client_ctor.assert_not_called()


def test_setup_cluster_deploys_local_stack_with_device_id_after_cluster_creation(monkeypatch):
    calls = []
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: calls.append(("create_cluster", cpu, mem)))
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)
    monkeypatch.setattr(
        api_module.local_stack, "deploy",
        lambda device_id, device_token=None, on_step=None: calls.append(("deploy", device_id)),
    )

    state = DesktopState(device_id="device-1")
    _make_api(state=state).setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    assert calls == [("create_cluster", 2, 4), ("deploy", "device-1")]


def test_setup_cluster_checks_local_stack_prerequisites_before_creating_cluster(monkeypatch):
    def raise_error():
        raise LocalStackError("BROWSETERM_CLOUD_INTERNAL_API_TOKEN is not set.")
    monkeypatch.setattr(api_module.local_stack, "check_prerequisites", raise_error)
    create_calls = []
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: create_calls.append(True))
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: False)

    status = _make_api().setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    assert create_calls == []  # never even tried to create the cluster
    assert "BROWSETERM_CLOUD_INTERNAL_API_TOKEN" in status["error"]


def test_setup_cluster_surfaces_local_stack_deploy_failure(monkeypatch):
    monkeypatch.setattr(api_module.cluster_manager, "create_cluster", lambda cpu, mem, storage=None, on_step=None: None)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)

    def raise_error(device_id, device_token=None, on_step=None):
        raise LocalStackError("container-maker: make prod_setup failed")
    monkeypatch.setattr(api_module.local_stack, "deploy", raise_error)

    status = _make_api().setup_cluster(cpu=2, memory_gb=4, storage_gb=50)

    assert status["cluster_exists"] is True  # the k3d cluster itself did come up
    assert "container-maker" in status["error"]


def test_setup_cluster_uses_constructor_on_setup_step_by_default(monkeypatch):
    """JS never passes its own on_step (pywebview's bridge only marshals JSON, not callables) --
    setup_cluster must fall back to whatever this Api was constructed with, which desktop/app.py
    wires to push live progress into the WebView via evaluate_js."""
    seen = []
    monkeypatch.setattr(
        api_module.cluster_manager, "create_cluster",
        lambda cpu, mem, storage=None, on_step=None: on_step("Creating Multipass VM", "started", ""),
    )
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)
    monkeypatch.setattr(
        api_module.local_stack, "deploy",
        lambda device_id, device_token=None, on_step=None: on_step("Deploying MinIO", "succeeded", ""),
    )

    _make_api(on_setup_step=lambda name, status, detail: seen.append((name, status, detail))).setup_cluster(
        cpu=2, memory_gb=4, storage_gb=50,
    )

    assert seen == [("Creating Multipass VM", "started", ""), ("Deploying MinIO", "succeeded", "")]


def test_setup_cluster_explicit_on_step_overrides_constructor_default(monkeypatch):
    monkeypatch.setattr(
        api_module.cluster_manager, "create_cluster",
        lambda cpu, mem, storage=None, on_step=None: on_step("Creating Multipass VM", "started", ""),
    )
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)
    monkeypatch.setattr(api_module.local_stack, "deploy", lambda device_id, device_token=None, on_step=None: None)

    constructor_seen = []
    explicit_seen = []
    api = _make_api(on_setup_step=lambda *a: constructor_seen.append(a))
    api.setup_cluster(cpu=2, memory_gb=4, storage_gb=50, on_step=lambda *a: explicit_seen.append(a))

    assert explicit_seen and not constructor_seen


def test_teardown_cluster_deletes_and_reports_status(monkeypatch):
    deleted = []
    monkeypatch.setattr(api_module.cluster_manager, "delete_cluster", lambda: deleted.append(True))
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: False)

    status = _make_api().teardown_cluster()

    assert deleted == [True]
    assert status["cluster_exists"] is False


def test_teardown_cluster_surfaces_error(monkeypatch):
    def raise_error():
        raise ClusterError("k3d cluster delete failed")
    monkeypatch.setattr(api_module.cluster_manager, "delete_cluster", raise_error)
    monkeypatch.setattr(api_module.cluster_manager, "cluster_exists", lambda: True)

    status = _make_api().teardown_cluster()

    assert "k3d cluster delete failed" in status["error"]


def test_open_browser_opens_cloud_host(monkeypatch):
    """Migration Part 3 moved the browser UI to Cloud entirely - this opens Cloud's real host now,
    not a local Ingress this repo deploys."""
    opened = []
    monkeypatch.setattr(api_module.webbrowser, "open", lambda url: opened.append(url))
    _make_api().open_browser()
    assert opened == [api_module.BROWSETERM_CLOUD_API_URL]


def test_list_cluster_pods_returns_pods_on_success(monkeypatch):
    pods = [{"namespace": "browseterm", "name": "x", "phase": "Running", "ready": "1/1", "restarts": 0}]
    monkeypatch.setattr(api_module.cluster_manager, "list_pods", lambda: pods)
    result = _make_api().list_cluster_pods()
    assert result == {"pods": pods, "error": None}


def test_list_cluster_pods_returns_error_on_failure(monkeypatch):
    def raise_error():
        raise ClusterError("kubectl get pods failed")
    monkeypatch.setattr(api_module.cluster_manager, "list_pods", raise_error)
    result = _make_api().list_cluster_pods()
    assert result == {"pods": [], "error": "kubectl get pods failed"}


def test_restart_workload_pod_deletes_then_returns_fresh_pod_list(monkeypatch):
    calls = []
    monkeypatch.setattr(api_module.cluster_manager, "restart_pod", lambda ns, name: calls.append((ns, name)))
    pods = [{"namespace": "browseterm", "name": "container-maker-79f9-abcde", "phase": "Running",
             "ready": "1/1", "restarts": 0}]
    monkeypatch.setattr(api_module.cluster_manager, "list_pods", lambda: pods)
    result = _make_api().restart_workload_pod("browseterm", "container-maker-79f9-abcde")
    assert calls == [("browseterm", "container-maker-79f9-abcde")]
    assert result == {"pods": pods, "error": None}


def test_restart_workload_pod_surfaces_error_but_still_returns_pod_list(monkeypatch):
    def raise_error(ns, name):
        raise ClusterError("pod not found")
    monkeypatch.setattr(api_module.cluster_manager, "restart_pod", raise_error)
    pods = [{"namespace": "browseterm", "name": "container-maker-79f9-abcde", "phase": "Running",
             "ready": "1/1", "restarts": 0}]
    monkeypatch.setattr(api_module.cluster_manager, "list_pods", lambda: pods)
    result = _make_api().restart_workload_pod("browseterm", "does-not-exist")
    assert result == {"pods": pods, "error": "pod not found"}

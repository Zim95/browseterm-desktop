"""
Cluster section of the Device page -- desktop/cluster_manager.py shells out to multipass/kubectl
(Part 15/16: a real Multipass VM + k3s, not k3d), so these tests swap out `subprocess.run` itself
rather than requiring real CLI tools/a real VM.
"""
import json

import pytest

from desktop import cluster_manager
from desktop.cluster_manager import ClusterError


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(responses):
    """Returns a subprocess.run stand-in keyed by the invoked command's first two tokens
    (e.g. ("multipass", "info")), recording every call it sees for assertions."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        key = tuple(cmd[:2])
        if key in responses:
            response = responses[key]
            return response(cmd) if callable(response) else response
        return _FakeCompleted(0, "", "")

    run.calls = calls
    return run


def _vm_info(state="Running", ip="10.0.0.5"):
    return json.dumps({"info": {cluster_manager.VM_NAME: {"state": state, "ipv4": [ip] if ip else []}}})


def test_cluster_exists_true_when_present(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(0, _vm_info(), ""),
    })))
    assert cluster_manager.cluster_exists() is True


def test_vm_state_running(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(0, _vm_info(state="Running"), ""),
    })))
    assert cluster_manager.vm_state() == "Running"


def test_vm_state_stopped(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(0, _vm_info(state="Stopped"), ""),
    })))
    assert cluster_manager.vm_state() == "Stopped"


def test_vm_state_missing_when_vm_does_not_exist(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(0, json.dumps({"info": {}}), ""),
    })))
    assert cluster_manager.vm_state() == "Missing"


def test_vm_state_unknown_when_multipass_errors(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(1, "", "some transient error"),
    })))
    assert cluster_manager.vm_state() == "Unknown"


def test_start_vm_invokes_multipass_start(monkeypatch):
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    cluster_manager.start_vm()
    assert ["multipass", "start", cluster_manager.VM_NAME] in fake.calls


def test_cluster_exists_false_when_multipass_errors(monkeypatch):
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("multipass", "info"): _FakeCompleted(1, "", "instance does not exist"),
    })))
    assert cluster_manager.cluster_exists() is False


def test_create_cluster_skips_launch_if_vm_exists(monkeypatch):
    fake = _fake_run({
        ("multipass", "info"): _FakeCompleted(0, _vm_info(), ""),
    })
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    monkeypatch.setattr(cluster_manager, "_install_k3s", lambda: None)
    monkeypatch.setattr(cluster_manager, "_install_gvisor", lambda: None)
    monkeypatch.setattr(cluster_manager, "_fetch_and_merge_kubeconfig", lambda: None)
    cluster_manager.create_cluster(4, 8.0)
    assert not any(c[:2] == ["multipass", "launch"] for c in fake.calls)


def test_create_cluster_launches_when_vm_absent(monkeypatch):
    calls_seen = {"launch": False}

    def run(cmd, **kwargs):
        if cmd[:2] == ["multipass", "info"]:
            return _FakeCompleted(1, "", "does not exist")
        if cmd[:2] == ["multipass", "launch"]:
            calls_seen["launch"] = True
            return _FakeCompleted(0, "", "")
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    monkeypatch.setattr(cluster_manager, "_install_k3s", lambda: None)
    monkeypatch.setattr(cluster_manager, "_install_gvisor", lambda: None)
    monkeypatch.setattr(cluster_manager, "_fetch_and_merge_kubeconfig", lambda: None)
    cluster_manager.create_cluster(4, 8.0)
    assert calls_seen["launch"] is True


def test_create_cluster_tolerates_already_exists_race(monkeypatch):
    """cluster_exists() can false-negative under host load (its own `multipass info` call times
    out, caught as a plain ClusterError and reported as "doesn't exist" - see its own docstring in
    cluster_manager.py). When that happens, `multipass launch` itself is the authoritative check:
    if IT says the instance already exists, create_cluster must treat that as success, not
    propagate a failure for a VM that was fine the whole time - caught for real against this
    project's own hardware under load."""

    def run(cmd, **kwargs):
        if cmd[:2] == ["multipass", "info"]:
            return _FakeCompleted(1, "", "info timed out")  # cluster_exists() false-negatives
        if cmd[:2] == ["multipass", "launch"]:
            return _FakeCompleted(1, "", "launch failed: instance \"browseterm\" already exists")
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    monkeypatch.setattr(cluster_manager, "_install_k3s", lambda: None)
    monkeypatch.setattr(cluster_manager, "_install_gvisor", lambda: None)
    monkeypatch.setattr(cluster_manager, "_fetch_and_merge_kubeconfig", lambda: None)
    cluster_manager.create_cluster(4, 8.0)  # must not raise


def test_create_cluster_reraises_other_launch_failures(monkeypatch):
    """The "already exists" tolerance in _create_vm must not swallow a real launch failure (e.g.
    out of disk space) - only that one specific, known-safe race."""

    def run(cmd, **kwargs):
        if cmd[:2] == ["multipass", "info"]:
            return _FakeCompleted(1, "", "does not exist")
        if cmd[:2] == ["multipass", "launch"]:
            return _FakeCompleted(1, "", "launch failed: not enough disk space")
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    with pytest.raises(ClusterError, match="not enough disk space"):
        cluster_manager.create_cluster(4, 8.0)


def test_create_cluster_reports_steps_in_order(monkeypatch):
    monkeypatch.setattr(cluster_manager, "_create_vm", lambda *a: None)
    monkeypatch.setattr(cluster_manager, "_install_k3s", lambda: None)
    monkeypatch.setattr(cluster_manager, "_install_gvisor", lambda: None)
    monkeypatch.setattr(cluster_manager, "_fetch_and_merge_kubeconfig", lambda: None)
    events = []
    cluster_manager.create_cluster(4, 8.0, on_step=lambda name, status, detail: events.append((name, status)))
    assert events == [
        ("Creating Multipass VM", "started"), ("Creating Multipass VM", "succeeded"),
        ("Installing k3s", "started"), ("Installing k3s", "succeeded"),
        ("Installing gVisor sandbox runtime", "started"), ("Installing gVisor sandbox runtime", "succeeded"),
        ("Configuring kubectl access", "started"), ("Configuring kubectl access", "succeeded"),
    ]


def test_create_cluster_reports_failed_step_and_reraises(monkeypatch):
    monkeypatch.setattr(cluster_manager, "_create_vm", lambda *a: None)

    def boom():
        raise ClusterError("k3s install failed")

    monkeypatch.setattr(cluster_manager, "_install_k3s", boom)
    events = []
    with pytest.raises(ClusterError, match="k3s install failed"):
        cluster_manager.create_cluster(4, 8.0, on_step=lambda name, status, detail: events.append((name, status, detail)))
    assert ("Installing k3s", "failed", "k3s install failed") in events
    assert not any(e[0] == "Configuring kubectl access" for e in events)


def test_create_cluster_works_without_on_step(monkeypatch):
    """on_step is optional everywhere - existing callers that don't pass it must keep working."""
    monkeypatch.setattr(cluster_manager, "_create_vm", lambda *a: None)
    monkeypatch.setattr(cluster_manager, "_install_k3s", lambda: None)
    monkeypatch.setattr(cluster_manager, "_install_gvisor", lambda: None)
    monkeypatch.setattr(cluster_manager, "_fetch_and_merge_kubeconfig", lambda: None)
    cluster_manager.create_cluster(4, 8.0)  # must not raise


def test_delete_cluster_purges_vm(monkeypatch):
    fake = _fake_run({("multipass", "delete"): _FakeCompleted(0, "", "")})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    cluster_manager.delete_cluster()
    assert ["multipass", "delete", cluster_manager.VM_NAME, "--purge"] in fake.calls


def test_delete_cluster_context_cleanup_is_best_effort(monkeypatch):
    """A failed `kubectl config delete-context` (e.g. it was never merged) must not blow up
    delete_cluster - the VM being gone is what actually matters."""
    def run(cmd, **kwargs):
        if cmd[:2] == ["multipass", "delete"]:
            return _FakeCompleted(0, "", "")
        return _FakeCompleted(1, "", "not found")

    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    cluster_manager.delete_cluster()  # must not raise


def test_is_monitored_pod_matches_real_current_components():
    assert cluster_manager.is_monitored_pod("container-maker-abc123") is True
    assert cluster_manager.is_monitored_pod("browseterm-device-agent-xyz") is True
    assert cluster_manager.is_monitored_pod("socket-ssh-def456") is True
    assert cluster_manager.is_monitored_pod("status-monitor-ghi789") is True
    assert cluster_manager.is_monitored_pod("minio-jkl012") is True
    # Old Local browser UI is gone (migration Part 3 moved it to Cloud) - must not be monitored.
    assert cluster_manager.is_monitored_pod("browseterm-server-local-abc") is False
    # One-shot Jobs, not long-lived Deployments - excluded deliberately.
    assert cluster_manager.is_monitored_pod("minio-createbucket-abc") is False
    assert cluster_manager.is_monitored_pod("reaper-29123456-abcde") is False
    assert cluster_manager.is_monitored_pod("unrelated-pod") is False


def test_list_pods_empty_when_cluster_absent(monkeypatch):
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: False)
    assert cluster_manager.list_pods() == []


def test_list_pods_filters_and_shapes(monkeypatch):
    monkeypatch.setattr(cluster_manager, "cluster_exists", lambda: True)
    pods_json = json.dumps({"items": [
        {
            "metadata": {"name": "container-maker-abc", "namespace": "browseterm"},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
        },
        {
            "metadata": {"name": "unrelated-xyz", "namespace": "browseterm"},
            "status": {"phase": "Running", "containerStatuses": []},
        },
    ]})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(_fake_run({
        ("kubectl", "--context"): _FakeCompleted(0, pods_json, ""),
    })))
    pods = cluster_manager.list_pods()
    assert len(pods) == 1
    assert pods[0]["name"] == "container-maker-abc"
    assert pods[0]["ready"] == "1/1"
    assert pods[0]["crashing"] is False


def test_fetch_and_merge_kubeconfig_renames_default_and_rewrites_server(monkeypatch, tmp_path):
    raw = (
        "apiVersion: v1\nclusters:\n- cluster:\n    server: https://127.0.0.1:6443\n  name: default\n"
        "contexts:\n- context:\n    cluster: default\n    user: default\n  name: default\n"
        "current-context: default\nkind: Config\nusers:\n- name: default\n"
    )
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text("")
    monkeypatch.setattr(cluster_manager, "KUBE_CONFIG_PATH", str(kubeconfig_path))
    monkeypatch.setattr(cluster_manager, "_vm_ip", lambda info: "10.0.0.5")

    rewritten_holder = {}

    def run(cmd, **kwargs):
        if cmd[:2] == ["multipass", "info"]:
            return _FakeCompleted(0, _vm_info(ip="10.0.0.5"), "")
        if cmd[:3] == ["multipass", "exec", cluster_manager.VM_NAME]:
            return _FakeCompleted(0, raw, "")
        if cmd[:2] == ["kubectl", "config"] and "view" in cmd:
            # The real assertion: read back the actual rewritten temp file this call was given
            # via KUBECONFIG (not a canned stand-in), so a regex that silently fails to rename
            # one of the three "name: default" lines (cluster/context/user) is caught here rather
            # than passing on a mocked "merged-output" that was never really produced by the
            # rewrite logic under test.
            fetched_path = kwargs["env"]["KUBECONFIG"].split(":")[1]
            with open(fetched_path) as f:
                rewritten_holder["content"] = f.read()
            return _FakeCompleted(0, "merged-output", "")
        if cmd[:3] == ["kubectl", "config", "use-context"]:
            return _FakeCompleted(0, "", "")
        return _FakeCompleted(0, "", "")

    fake = _FakeModule(run)
    monkeypatch.setattr(cluster_manager, "subprocess", fake)
    monkeypatch.setattr(cluster_manager.subprocess, "run", run)
    cluster_manager._fetch_and_merge_kubeconfig()
    assert kubeconfig_path.read_text() == "merged-output"

    rewritten = rewritten_holder["content"]
    assert "https://10.0.0.5:6443" in rewritten
    assert "default" not in rewritten
    # users:' own list item writes "name:" as its first field right after "- " (`- name: default`)
    # rather than as a later sibling key on its own indented line the way clusters:/contexts: do
    # (`  name: default`) - this specifically exercises that shape, since a plain `^\s*name:`
    # pattern silently fails to match a line starting with a literal "-".
    assert "- name: browseterm" in rewritten


def test_install_k3s_disables_traefik_and_servicelb(monkeypatch):
    """No LoadBalancer-type Service and no real Ingress consumer exists on this single-tenant
    local cluster (see cluster_manager._install_k3s's own docstring) - the bundled controllers
    this disables would be unused, not load-bearing."""
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    cluster_manager._install_k3s()
    install_call = next(c for c in fake.calls if c[:2] == ["multipass", "exec"] and "curl -sfL https://get.k3s.io" in c[-1])
    assert "--disable traefik" in install_call[-1]
    assert "--disable servicelb" in install_call[-1]
    # local-path (default StorageClass) must NOT be disabled - MinIO/Postgres/Redis PVCs need it.
    assert "local-storage" not in install_call[-1]


def test_install_gvisor_skips_download_when_runsc_already_present(monkeypatch):
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    cluster_manager._install_gvisor()
    gvisor_call = next(c for c in fake.calls if c[:2] == ["multipass", "exec"] and "runsc" in c[-1])
    assert "command -v runsc" in gvisor_call[-1]
    assert "containerd.runtimes.runsc" in gvisor_call[-1]


def test_install_gvisor_fetches_and_installs_gvisor_sentry():
    '''
    Regression test for a real production bug: the install script only extracted
    runsc/containerd-shim-runsc-v1 from the release archive, explicitly treating gvisor_sentry
    (from the archive's gvisor-bin/ subdirectory) as an unused extra - but the runsc shim actually
    requires it at container-creation time ("sidecar gvisor_sentry not usable ... no such file or
    directory", --sidecar-usage-policy=STRICT), so every pod's sandbox creation failed outright
    and stayed Pending/ContainerCreating forever, on every VM this Setup flow had ever built.
    '''
    script = cluster_manager._GVISOR_INSTALL_SCRIPT
    assert "gvisor-bin/gvisor_sentry" in script
    assert "/usr/local/bin/gvisor-bin/gvisor_sentry" in script
    # The idempotency check must not skip re-installing on a VM that already has runsc but is
    # missing gvisor_sentry (every VM built before this fix) - re-running Setup against one must
    # detect and fix the gap, not silently declare success because runsc alone is present.
    assert "-x /usr/local/bin/gvisor-bin/gvisor_sentry" in script


def test_install_gvisor_waits_for_node_ready_again(monkeypatch):
    """gVisor's containerd-template write path restarts k3s (see the script's own comment) -
    _install_gvisor must wait for the node to come back Ready, the same way _install_k3s already
    does after its own initial install."""
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    cluster_manager._install_gvisor()
    assert any(
        c[:3] == ["multipass", "exec", cluster_manager.VM_NAME] and "wait" in c and "Ready" in " ".join(c)
        for c in fake.calls
    )


class _FakeModule:
    """Stands in for the `subprocess` module so `cluster_manager.subprocess.run(...)` (used both
    by `_run` and directly inside `_fetch_and_merge_kubeconfig`) resolves to the same fake."""
    def __init__(self, run_fn):
        self.run = run_fn

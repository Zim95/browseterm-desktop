"""
Cluster section of the Device page -- desktop/cluster_manager.py shells out to k3d/docker/kubectl
(this project's existing convention for cluster lifecycle, see SETUP-LOCAL.md), so these tests
swap out `subprocess.run` itself rather than requiring real CLI tools/a real cluster.
"""
import json
import subprocess

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
    (e.g. ("k3d", "cluster")), recording every call it sees for assertions."""
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


def test_cluster_exists_true_when_name_present(monkeypatch):
    listing = json.dumps([{"name": "browseterm-k3s-local"}, {"name": "other"}])
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(0, listing, ""),
    }))
    assert cluster_manager.cluster_exists() is True


def test_cluster_exists_false_when_absent(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(0, json.dumps([{"name": "other"}]), ""),
    }))
    assert cluster_manager.cluster_exists() is False


def test_cluster_exists_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(1, "", "boom"),
    }))
    with pytest.raises(ClusterError, match="boom"):
        cluster_manager.cluster_exists()


def test_run_raises_on_missing_binary(monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(cluster_manager.subprocess, "run", run)
    with pytest.raises(ClusterError, match="not found"):
        cluster_manager.cluster_exists()


def test_run_raises_on_timeout(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    monkeypatch.setattr(cluster_manager.subprocess, "run", run)
    with pytest.raises(ClusterError, match="timed out"):
        cluster_manager.cluster_exists()


def test_create_cluster_no_ops_if_already_exists(monkeypatch):
    fake = _fake_run({
        ("k3d", "cluster"): lambda cmd: (
            _FakeCompleted(0, json.dumps([{"name": "browseterm-k3s-local"}]), "")
            if cmd[2] == "list" else pytest.fail(f"unexpected k3d call: {cmd}")
        ),
    })
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)
    cluster_manager.create_cluster(cpu_cores=2, memory_gb=4)
    assert [c[2] for c in fake.calls if tuple(c[:2]) == ("k3d", "cluster")] == ["list"]


def test_create_cluster_creates_and_applies_cpu_limit(monkeypatch):
    def k3d_cluster(cmd):
        if cmd[2] == "list":
            return _FakeCompleted(0, json.dumps([]), "")
        if cmd[2] == "create":
            assert "--servers-memory" in cmd
            assert cmd[cmd.index("--servers-memory") + 1] == "4G"
            assert cmd[cmd.index("--agents-memory") + 1] == "4G"
            return _FakeCompleted(0, "", "")
        pytest.fail(f"unexpected k3d cluster call: {cmd}")

    def docker(cmd):
        if cmd[1] == "ps":
            return _FakeCompleted(0, "k3d-browseterm-k3s-local-server-0\n", "")
        if cmd[1] == "update":
            assert cmd[2:] == ["--cpus", "2", "k3d-browseterm-k3s-local-server-0"]
            return _FakeCompleted(0, "", "")
        pytest.fail(f"unexpected docker call: {cmd}")

    fake = _fake_run({("k3d", "cluster"): k3d_cluster, ("docker", "ps"): docker, ("docker", "update"): docker})
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)
    cluster_manager.create_cluster(cpu_cores=2, memory_gb=4)


def test_create_cluster_raises_on_failure(monkeypatch):
    def k3d_cluster(cmd):
        if cmd[2] == "list":
            return _FakeCompleted(0, "[]", "")
        return _FakeCompleted(1, "", "create failed")

    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({("k3d", "cluster"): k3d_cluster}))
    with pytest.raises(ClusterError, match="create failed"):
        cluster_manager.create_cluster(cpu_cores=2, memory_gb=4)


def test_apply_cpu_limit_is_best_effort_if_docker_fails(monkeypatch):
    """CPU capping must never take down an otherwise-successful cluster create."""
    def k3d_cluster(cmd):
        return _FakeCompleted(0, "[]" if cmd[2] == "list" else "", "")

    def docker_ps(cmd):
        return _FakeCompleted(1, "", "docker not running")

    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): k3d_cluster, ("docker", "ps"): docker_ps,
    }))
    cluster_manager.create_cluster(cpu_cores=2, memory_gb=4)  # must not raise


def test_delete_cluster_calls_k3d_delete(monkeypatch):
    fake = _fake_run({("k3d", "cluster"): _FakeCompleted(0, "", "")})
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)
    cluster_manager.delete_cluster()
    assert fake.calls[0][:3] == ["k3d", "cluster", "delete"]


def test_delete_cluster_raises_on_failure(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(1, "", "delete failed"),
    }))
    with pytest.raises(ClusterError, match="delete failed"):
        cluster_manager.delete_cluster()


def test_restart_pod_deletes_the_named_pod(monkeypatch):
    fake = _fake_run({("kubectl", "--context"): _FakeCompleted(0, "", "")})
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)
    cluster_manager.restart_pod("browseterm", "container-maker-79f9-abcde")
    assert fake.calls[0] == [
        "kubectl", "--context", cluster_manager.KUBE_CONTEXT,
        "-n", "browseterm", "delete", "pod", "container-maker-79f9-abcde",
    ]


def test_restart_pod_raises_on_failure(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("kubectl", "--context"): _FakeCompleted(1, "", "pod not found"),
    }))
    with pytest.raises(ClusterError, match="pod not found"):
        cluster_manager.restart_pod("browseterm", "does-not-exist")


def test_list_pods_empty_when_cluster_missing(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(0, "[]", ""),
    }))
    assert cluster_manager.list_pods() == []


def test_list_pods_parses_ready_and_restart_counts(monkeypatch):
    pods_json = json.dumps({"items": [
        {
            "metadata": {"namespace": "browseterm", "name": "container-maker-abc"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True, "restartCount": 2}],
            },
        },
        {
            "metadata": {"namespace": "browseterm", "name": "status-monitor-xyz"},
            "status": {
                "phase": "Pending",
                "containerStatuses": [{"ready": False, "restartCount": 0}, {"ready": True, "restartCount": 1}],
            },
        },
    ]})
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(0, json.dumps([{"name": "browseterm-k3s-local"}]), ""),
        ("kubectl", "--context"): _FakeCompleted(0, pods_json, ""),
    }))
    pods = cluster_manager.list_pods()
    assert pods[0] == {
        "namespace": "browseterm", "name": "container-maker-abc",
        "phase": "Running", "ready": "1/1", "restarts": 2, "crashing": False,
    }
    assert pods[1] == {
        "namespace": "browseterm", "name": "status-monitor-xyz",
        "phase": "Pending", "ready": "1/2", "restarts": 1, "crashing": False,
    }


@pytest.mark.parametrize("phase,statuses", [
    ("Pending", [{"ready": False, "restartCount": 0}]),  # no state at all yet -- brand new pod
    ("Pending", [{"ready": False, "restartCount": 0, "state": {"waiting": {"reason": "ContainerCreating"}}}]),
    ("Pending", [{"ready": False, "restartCount": 0, "state": {"waiting": {"reason": "PodInitializing"}}}]),
    ("Running", [{"ready": True, "restartCount": 3}]),  # restarted before but currently fine
])
def test_is_crashing_false_during_normal_startup(phase, statuses):
    '''The exact complaint this was built to fix: a pod cycling through Pending/
    ContainerCreating/PodInitializing on a completely normal Setup run must never be flagged as
    crashing just because it isn't Running yet.'''
    assert cluster_manager._is_crashing(phase, statuses) is False


@pytest.mark.parametrize("phase,statuses", [
    ("Running", [{"ready": False, "restartCount": 5, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]),
    ("Pending", [{"ready": False, "restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff"}}}]),
    ("Pending", [{"ready": False, "restartCount": 0, "state": {"waiting": {"reason": "ErrImagePull"}}}]),
    ("Running", [{"ready": False, "restartCount": 1, "state": {"terminated": {"reason": "Error"}}}]),
    ("Failed", [{"ready": False, "restartCount": 0}]),
])
def test_is_crashing_true_on_real_failure_signals(phase, statuses):
    assert cluster_manager._is_crashing(phase, statuses) is True


def test_list_pods_filters_out_non_monitored_system_pods(monkeypatch):
    """The pod monitor is scoped to the actual BrowseTerm local-stack workloads -- k3s/k3d's own
    system pods (coredns, svclb-*, local-path-provisioner, ...) must never show up."""
    pods_json = json.dumps({"items": [
        {
            "metadata": {"namespace": "kube-system", "name": "coredns-5d78c9869d-abcde"},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
        },
        {
            "metadata": {"namespace": "kube-system", "name": "svclb-ingress-nginx-controller-xyz"},
            "status": {"phase": "Running", "containerStatuses": []},
        },
        {
            "metadata": {"namespace": "kube-system", "name": "local-path-provisioner-abc"},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
        },
        {
            "metadata": {"namespace": "browseterm", "name": "container-maker-development-79f9-abcde"},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
        },
    ]})
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run({
        ("k3d", "cluster"): _FakeCompleted(0, json.dumps([{"name": "browseterm-k3s-local"}]), ""),
        ("kubectl", "--context"): _FakeCompleted(0, pods_json, ""),
    }))
    pods = cluster_manager.list_pods()
    assert [p["name"] for p in pods] == ["container-maker-development-79f9-abcde"]


@pytest.mark.parametrize("pod_name", [
    "container-maker-79f9-abcde",                  # prod-style manifest
    "container-maker-development-79f9-abcde",      # dev-style manifest
    "socket-ssh-79f9-abcde",
    "socket-ssh-development-79f9-abcde",
    "browseterm-server-79f9-abcde",                # browseterm-server-local's actual Deployment name
    "browseterm-server-development-79f9-abcde",
    "status-monitor-79f9-abcde",
    "minio-79f9-abcde",                            # minio.yaml's own Deployment
    "ingress-nginx-controller-79f9-abcde",
])
def test_monitored_prefixes_cover_every_always_on_local_stack_component(pod_name):
    assert cluster_manager.is_monitored_pod(pod_name)


@pytest.mark.parametrize("pod_name", [
    "cert-manager-development-79f9-abcde",         # CronJob (dev-style Deployment variant doesn't exist in prod)
    "cert-manager-28912345-abcde",                 # CronJob's own spawned Job pod
    "reaper-28912345-abcde",                       # CronJob's own spawned Job pod
    "snapshot-job-development-79f9-abcde",         # no persistent Deployment in prod -- spawned per-save
    "minio-createbucket-abcde",                    # minio.yaml's one-shot bucket-creation Job
])
def test_monitored_prefixes_deliberately_exclude_triggered_only_workloads(pod_name):
    '''Verified against each component's actual manifest (see MONITORED_WORKLOAD_PREFIXES' own
    docstring), not assumed: cert-manager and reaper are CronJobs, snapshot-job has no persistent
    Deployment in prod at all, and minio-createbucket is a one-shot Job -- none of these are
    pods that are supposed to stay running, so an "up/down" health read doesn't mean anything for
    them and they must never show up in the pod monitor.'''
    assert not cluster_manager.is_monitored_pod(pod_name)

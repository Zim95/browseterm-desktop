"""
Native (no VM) k3s lifecycle for headless Linux (Part 18) - same fake-subprocess convention
test_cluster_manager.py already established, since there is no real Linux/systemd host to test
against in this environment either (same practical constraint as Part 17's Windows blocker, just
one where pure-logic unit tests against a fake command runner are still meaningfully possible).

native_k3s._run/_run_step are IMPORTED BY VALUE from cluster_manager (see native_k3s.py's own
module docstring), so patching subprocess for anything that goes through them means patching
`cluster_manager.subprocess`, not `native_k3s.subprocess` - the one exception is
_merge_kubeconfig, which calls `subprocess.run` directly via native_k3s's own local import
(mirroring cluster_manager._fetch_and_merge_kubeconfig's identical structure), so that one test
patches `native_k3s.subprocess` instead.
"""
import pytest

from desktop import cluster_manager, native_k3s
from desktop.cluster_manager import ClusterError


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(responses):
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


class _FakeModule:
    def __init__(self, run_fn):
        self.run = run_fn


@pytest.fixture(autouse=True)
def _as_root(monkeypatch):
    monkeypatch.setattr(native_k3s.os, "geteuid", lambda: 0)


def test_require_root_raises_when_not_root(monkeypatch):
    monkeypatch.setattr(native_k3s.os, "geteuid", lambda: 501)
    with pytest.raises(ClusterError, match="root"):
        native_k3s.require_root()


def test_require_root_passes_as_root():
    native_k3s.require_root()  # must not raise, autouse fixture stubs geteuid() -> 0


def test_cluster_exists_true_when_kubeconfig_present(monkeypatch):
    monkeypatch.setattr(native_k3s.os.path, "isfile", lambda path: path == native_k3s.K3S_KUBECONFIG_PATH)
    assert native_k3s.cluster_exists() is True


def test_cluster_exists_false_when_kubeconfig_absent(monkeypatch):
    monkeypatch.setattr(native_k3s.os.path, "isfile", lambda path: False)
    assert native_k3s.cluster_exists() is False


def test_create_cluster_reports_steps_in_order(monkeypatch):
    monkeypatch.setattr(native_k3s, "_install_k3s", lambda: None)
    monkeypatch.setattr(native_k3s, "_install_gvisor", lambda: None)
    monkeypatch.setattr(native_k3s, "_merge_kubeconfig", lambda: None)
    events = []
    native_k3s.create_cluster(4, 8.0, on_step=lambda name, status, detail: events.append((name, status)))
    assert events == [
        ("Installing k3s", "started"), ("Installing k3s", "succeeded"),
        ("Installing gVisor sandbox runtime", "started"), ("Installing gVisor sandbox runtime", "succeeded"),
        ("Configuring kubectl access", "started"), ("Configuring kubectl access", "succeeded"),
    ]


def test_create_cluster_requires_root(monkeypatch):
    monkeypatch.setattr(native_k3s.os, "geteuid", lambda: 501)
    with pytest.raises(ClusterError, match="root"):
        native_k3s.create_cluster(4, 8.0)


def test_install_k3s_disables_traefik_and_servicelb(monkeypatch):
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    native_k3s._install_k3s()
    install_call = next(c for c in fake.calls if c[:2] == ["bash", "-c"] and "curl -sfL https://get.k3s.io" in c[-1])
    assert "--disable traefik" in install_call[-1]
    assert "--disable servicelb" in install_call[-1]


def test_install_gvisor_runs_the_shared_script(monkeypatch):
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    native_k3s._install_gvisor()
    gvisor_call = next(c for c in fake.calls if c[:2] == ["bash", "-c"] and "runsc" in c[-1])
    assert "command -v runsc" in gvisor_call[-1]


def test_merge_kubeconfig_renames_default_and_skips_ip_rewrite(monkeypatch, tmp_path):
    """Unlike cluster_manager's VM variant, no server-IP rewrite is needed here - 127.0.0.1 is
    already correct since this process *is* the k3s node."""
    raw = (
        "apiVersion: v1\nclusters:\n- cluster:\n    server: https://127.0.0.1:6443\n  name: default\n"
        "contexts:\n- context:\n    cluster: default\n    user: default\n  name: default\n"
        "current-context: default\nkind: Config\nusers:\n- name: default\n"
    )
    k3s_yaml = tmp_path / "k3s.yaml"
    k3s_yaml.write_text(raw)
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text("")
    monkeypatch.setattr(native_k3s, "K3S_KUBECONFIG_PATH", str(k3s_yaml))
    monkeypatch.setattr(native_k3s, "KUBE_CONFIG_PATH", str(kubeconfig_path))

    rewritten_holder = {}

    def run(cmd, **kwargs):
        if cmd[:2] == ["kubectl", "config"] and "view" in cmd:
            # Read back the actual rewritten temp file (see
            # test_fetch_and_merge_kubeconfig_renames_default_and_rewrites_server in
            # test_cluster_manager.py for why this - a canned "merged-output" stand-in never
            # exercises the regex under test at all).
            fetched_path = kwargs["env"]["KUBECONFIG"].split(":")[1]
            with open(fetched_path) as f:
                rewritten_holder["content"] = f.read()
            return _FakeCompleted(0, "merged-output", "")
        if cmd[:3] == ["kubectl", "config", "use-context"]:
            return _FakeCompleted(0, "", "")
        return _FakeCompleted(0, "", "")

    monkeypatch.setattr(native_k3s, "subprocess", _FakeModule(run))
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    native_k3s._merge_kubeconfig()
    assert kubeconfig_path.read_text() == "merged-output"

    rewritten = rewritten_holder["content"]
    assert "default" not in rewritten
    assert "- name: browseterm" in rewritten


def test_delete_cluster_runs_uninstall_script_when_present(monkeypatch):
    monkeypatch.setattr(native_k3s.os.path, "isfile", lambda path: path == native_k3s.K3S_UNINSTALL_SCRIPT)
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    native_k3s.delete_cluster()
    assert any(c == [native_k3s.K3S_UNINSTALL_SCRIPT] for c in fake.calls)


def test_delete_cluster_skips_uninstall_script_when_absent(monkeypatch):
    monkeypatch.setattr(native_k3s.os.path, "isfile", lambda path: False)
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    native_k3s.delete_cluster()  # must not raise
    assert not any(c == [native_k3s.K3S_UNINSTALL_SCRIPT] for c in fake.calls)


def test_delete_cluster_context_cleanup_is_best_effort(monkeypatch):
    monkeypatch.setattr(native_k3s.os.path, "isfile", lambda path: False)

    def run(cmd, **kwargs):
        return _FakeCompleted(1, "", "not found")

    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(run))
    native_k3s.delete_cluster()  # must not raise


def test_start_service_calls_systemctl(monkeypatch):
    fake = _fake_run({})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    native_k3s.start_service()
    assert ["systemctl", "start", "k3s"] in fake.calls


def test_stop_service_requires_root(monkeypatch):
    monkeypatch.setattr(native_k3s.os, "geteuid", lambda: 501)
    with pytest.raises(ClusterError, match="root"):
        native_k3s.stop_service()


def test_service_logs_calls_journalctl(monkeypatch):
    fake = _fake_run({("journalctl", "-u"): _FakeCompleted(0, "log line 1\nlog line 2", "")})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    output = native_k3s.service_logs(lines=50)
    assert output == "log line 1\nlog line 2"
    assert ["journalctl", "-u", "k3s", "-n", "50", "--no-pager"] in fake.calls


def test_pod_logs_calls_kubectl_with_context_and_namespace(monkeypatch):
    fake = _fake_run({("kubectl", "--context"): _FakeCompleted(0, "pod output", "")})
    monkeypatch.setattr(cluster_manager, "subprocess", _FakeModule(fake))
    output = native_k3s.pod_logs("browseterm", "container-maker-abc")
    assert output == "pod output"
    assert ["kubectl", "--context", native_k3s.KUBE_CONTEXT, "-n", "browseterm", "logs", "container-maker-abc", "--tail=200"] in fake.calls

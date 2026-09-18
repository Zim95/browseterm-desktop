"""
Cluster section's local-stack deployment (desktop/local_stack.py) -- deploying the real
container-maker/socket-ssh/browseterm-server-local/status_monitor/cert-manager/reaper/minio
workloads into the k3d cluster desktop/cluster_manager.py creates.

Two groups of tests: `deploy()`'s own step ORDERING (mocking each internal step function and
asserting call order -- more maintainable than mocking every subprocess call across a 7-component
pipeline), and a handful of individual low-level functions mocked at the `subprocess.run` boundary
(same pattern as tests/test_cluster_manager.py) to cover exact command construction.
"""
import json

import pytest

from desktop import cluster_manager, local_stack
from desktop.local_stack import LocalStackError

# Captured before the autouse _isolated_config fixture below monkeypatches the module-level name
# with a fake for every other test in this file - the two tests dedicated to this function itself
# need the real implementation.
_real_etc_hosts_maps_to_loopback = local_stack._etc_hosts_maps_to_loopback


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(default=_FakeCompleted(0, "", "")):
    calls = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return default

    run.calls = calls
    return run


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Every test gets a real, valid token/etc-hosts setup by default and an isolated repos dir --
    individual tests override just the piece they're exercising."""
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "shared-secret-token")
    monkeypatch.setattr(local_stack, "LOCAL_STACK_REPOS_DIR", str(tmp_path))
    fake_hosts = tmp_path / "hosts"
    fake_hosts.write_text(f"127.0.0.1\t{local_stack.INGRESS_HOST}\n127.0.0.1\t{local_stack.SOCKET_SSH_HOST}\n")
    monkeypatch.setattr(local_stack, "_etc_hosts_maps_to_loopback", lambda h: h in fake_hosts.read_text())


def test_check_prerequisites_passes_with_token_and_etc_hosts_set():
    local_stack.check_prerequisites()  # must not raise


def test_check_prerequisites_fails_without_internal_api_token(monkeypatch):
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "")
    with pytest.raises(LocalStackError, match="BROWSETERM_CLOUD_INTERNAL_API_TOKEN"):
        local_stack.check_prerequisites()


def test_check_prerequisites_fails_with_missing_etc_hosts_entries(monkeypatch):
    monkeypatch.setattr(local_stack, "_etc_hosts_maps_to_loopback", lambda h: False)
    with pytest.raises(LocalStackError, match="socketssh.local"):
        local_stack.check_prerequisites()


@pytest.mark.parametrize("hosts_content", [
    "192.168.252.200  socketssh.local\n127.0.0.1\tbrowseterm.local.com\n",  # real bug hit live:
    # a stale IP from an old cluster, not a missing line at all
    "# 127.0.0.1\tsocketssh.local\n127.0.0.1\tbrowseterm.local.com\n",       # commented out
])
def test_etc_hosts_maps_to_loopback_rejects_wrong_or_commented_entries(tmp_path, hosts_content):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(hosts_content)
    assert _real_etc_hosts_maps_to_loopback("socketssh.local", path=str(hosts_file)) is False


def test_etc_hosts_maps_to_loopback_accepts_a_real_loopback_entry(tmp_path):
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text("127.0.0.1\tsocketssh.local\n")
    assert _real_etc_hosts_maps_to_loopback("socketssh.local", path=str(hosts_file)) is True


def test_repo_path_raises_if_checkout_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(local_stack, "LOCAL_STACK_REPOS_DIR", str(tmp_path))
    with pytest.raises(LocalStackError, match="container-maker"):
        local_stack._repo_path("container-maker")


def test_make_touches_placeholder_env_mk_and_invokes_make_with_variables(tmp_path, monkeypatch):
    repo = tmp_path / "container-maker"
    repo.mkdir()
    fake = _fake_run()
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)

    local_stack._make("container-maker", "prod_setup", NAMESPACE="browseterm", REPO_NAME="zim95")

    assert (repo / "env.mk").exists()
    call = fake.calls[0]
    assert call["cmd"] == ["make", "prod_setup", "NAMESPACE=browseterm", "REPO_NAME=zim95"]
    assert call["cwd"] == str(repo)


def test_make_does_not_overwrite_existing_env_mk(tmp_path, monkeypatch):
    repo = tmp_path / "browseterm_workload" / "reaper"
    repo.mkdir(parents=True)
    (repo / "env.mk").write_text("DEVICE_ID=already-here\n")
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run())

    local_stack._make("reaper", "dev_setup")

    assert (repo / "env.mk").read_text() == "DEVICE_ID=already-here\n"


def test_write_env_mk_writes_key_value_lines(tmp_path):
    repo = tmp_path / "browseterm_workload" / "status_monitor"
    repo.mkdir(parents=True)
    local_stack._write_env_mk("status_monitor", {"NAMESPACE": "browseterm", "REPO_NAME": "zim95"})
    assert (repo / "env.mk").read_text() == "NAMESPACE=browseterm\nREPO_NAME=zim95\n"


def test_strip_gvisor_runtime_class_patches_deployment(monkeypatch):
    fake = _fake_run()
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)

    local_stack._strip_gvisor_runtime_class()

    cmd = fake.calls[0]["cmd"]
    assert cmd[:6] == ["kubectl", "--context", local_stack.KUBE_CONTEXT, "-n", "browseterm", "patch"]
    assert "deployment" in cmd and "container-maker" in cmd
    patch_json = cmd[cmd.index("-p") + 1]
    patch = json.loads(patch_json)
    env = patch["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [{"name": "USER_POD_RUNTIME_CLASS", "value": ""}]


def test_trigger_and_wait_cronjob_creates_then_waits(monkeypatch):
    fake = _fake_run()
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)

    local_stack._trigger_and_wait_cronjob("cert-manager", "cert-manager-bootstrap", timeout=60)

    create_cmd, wait_cmd = fake.calls[0]["cmd"], fake.calls[1]["cmd"]
    assert "create" in create_cmd and "job" in create_cmd and "--from=cronjob/cert-manager" in create_cmd
    assert "wait" in wait_cmd and "--for=condition=complete" in wait_cmd


def test_resolve_cloud_ingress_host_ip_parses_first_token(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run(_FakeCompleted(0, "192.168.65.254\n", "")))
    assert local_stack._resolve_cloud_ingress_host_ip() == "192.168.65.254"


def test_resolve_cloud_ingress_host_ip_raises_on_empty_output(monkeypatch):
    monkeypatch.setattr(cluster_manager.subprocess, "run", _fake_run(_FakeCompleted(0, "", "")))
    with pytest.raises(LocalStackError, match="host.docker.internal"):
        local_stack._resolve_cloud_ingress_host_ip()


def test_ensure_ingress_nginx_skips_everything_when_already_installed(monkeypatch):
    """A second Setup click against an already-provisioned cluster must not re-delete Traefik,
    re-apply the manifest, or re-wait on the rollout -- just confirm it's there and move on."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(0, "deployment.apps/ingress-nginx-controller\n", "")

    monkeypatch.setattr(cluster_manager.subprocess, "run", run)
    local_stack._ensure_ingress_nginx()
    assert len(calls) == 1
    assert "deployment" in calls[0] and "ingress-nginx-controller" in calls[0]


def test_ensure_ingress_nginx_installs_when_missing(monkeypatch):
    """A cluster created before this check existed (Traefik still running, ingress-nginx never
    installed) self-heals the next time Setup runs -- self-heals here means the full
    delete-Traefik / apply-manifest / wait-for-rollout sequence SETUP-LOCAL.md step 2 describes."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(0, "", "")  # --ignore-not-found: empty means not installed yet

    monkeypatch.setattr(cluster_manager.subprocess, "run", run)
    local_stack._ensure_ingress_nginx()

    assert len(calls) == 4
    assert "deployment" in calls[0] and "ingress-nginx-controller" in calls[0]
    assert "delete" in calls[1] and "helmchart" in calls[1] and "traefik" in calls[1]
    assert calls[2][-3:] == ["apply", "-f", local_stack._INGRESS_NGINX_MANIFEST_URL]
    assert "rollout" in calls[3] and "status" in calls[3]


def test_deploy_minio_applies_the_bundled_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "minio.yaml"
    manifest.write_text("kind: Secret\n")
    monkeypatch.setattr(local_stack, "_MINIO_MANIFEST_PATH", str(manifest))
    fake = _fake_run()
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)

    local_stack._deploy_minio()

    assert fake.calls[0]["input"] == "kind: Secret\n"
    assert fake.calls[0]["cmd"] == ["kubectl", "--context", local_stack.KUBE_CONTEXT, "apply", "-f", "-"]


def test_deploy_minio_raises_if_manifest_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(local_stack, "_MINIO_MANIFEST_PATH", str(tmp_path / "nope.yaml"))
    with pytest.raises(LocalStackError, match="minio manifest not found"):
        local_stack._deploy_minio()


def test_deploy_calls_every_step_in_dependency_order(monkeypatch):
    calls = []
    steps = [
        "_ensure_ingress_nginx", "_ensure_namespace", "_ensure_internal_api_token_secret",
        "_ensure_device_credentials_secret",
        "_ensure_db_placeholder_secret", "_deploy_minio", "_deploy_cert_manager",
        "_deploy_container_maker", "_deploy_socket_ssh",
    ]
    for step in steps:
        monkeypatch.setattr(local_stack, step, lambda *a, name=step: calls.append(name))
    monkeypatch.setattr(local_stack, "_resolve_cloud_ingress_host_ip", lambda: calls.append("_resolve_cloud_ingress_host_ip") or "1.2.3.4")
    monkeypatch.setattr(local_stack, "_deploy_browseterm_server_local", lambda ip: calls.append(("_deploy_browseterm_server_local", ip)))
    monkeypatch.setattr(local_stack, "_deploy_status_monitor", lambda ip: calls.append(("_deploy_status_monitor", ip)))
    monkeypatch.setattr(local_stack, "_deploy_reaper", lambda device_id, ip: calls.append(("_deploy_reaper", device_id, ip)))
    monkeypatch.setattr(local_stack, "_run", lambda *a, **k: calls.append(("kubectl_use_context",)))

    local_stack.deploy("device-123", "device-token-abc")

    assert calls == [
        ("kubectl_use_context",),
        "_ensure_ingress_nginx",
        "_ensure_namespace", "_ensure_internal_api_token_secret", "_ensure_device_credentials_secret",
        "_ensure_db_placeholder_secret",
        "_deploy_minio", "_deploy_cert_manager",
        "_resolve_cloud_ingress_host_ip",
        "_deploy_container_maker", "_deploy_socket_ssh",
        ("_deploy_browseterm_server_local", "1.2.3.4"),
        ("_deploy_status_monitor", "1.2.3.4"),
        ("_deploy_reaper", "device-123", "1.2.3.4"),
    ]


def test_deploy_raises_before_any_step_if_prerequisites_fail(monkeypatch):
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "")
    called = []
    monkeypatch.setattr(local_stack, "_ensure_namespace", lambda: called.append(True))

    with pytest.raises(LocalStackError):
        local_stack.deploy("device-123")

    assert called == []


def test_ensure_device_credentials_secret_creates_it_from_the_keychain_token(monkeypatch):
    fake = _fake_run()
    monkeypatch.setattr(cluster_manager.subprocess, "run", fake)

    local_stack._ensure_device_credentials_secret("device-123", "token-abc")

    # _create_or_update's own idempotent-apply pattern: render via `create --dry-run=client -o
    # yaml` first, then `kubectl apply -f -` the result - see its own docstring.
    assert fake.calls[0]["cmd"] == [
        "kubectl", "--context", local_stack.KUBE_CONTEXT, "-n", local_stack.NAMESPACE,
        "create", "secret", "generic", "device-credentials",
        "--from-literal=DEVICE_ID=device-123", "--from-literal=DEVICE_TOKEN=token-abc",
        "--dry-run=client", "-o", "yaml",
    ]


def test_ensure_device_credentials_secret_rejects_missing_token() -> None:
    """tunnel_registrar has nothing to authenticate with otherwise - fail loudly here rather than
    silently deploying a sidecar that will never successfully register a tunnel."""
    with pytest.raises(LocalStackError, match="device_id/device_token"):
        local_stack._ensure_device_credentials_secret("device-123", "")

    with pytest.raises(LocalStackError, match="device_id/device_token"):
        local_stack._ensure_device_credentials_secret("", "token-abc")

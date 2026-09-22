"""
desktop/local_stack.py deploys the REAL current local-stack workloads (Part 12's Device-Agent-
based architecture, not the old CLOUD_INTERNAL_API_TOKEN/browseterm-server-local model) - these
tests mock `_run`/`_make`/`_run_script` directly rather than requiring real repo checkouts, `make`,
or a real cluster.
"""
import pytest

from desktop import local_stack
from desktop.local_stack import LocalStackError


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "test-token")


def test_check_prerequisites_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "")
    with pytest.raises(LocalStackError, match="BROWSETERM_CLOUD_INTERNAL_API_TOKEN"):
        local_stack.check_prerequisites()


def test_check_prerequisites_passes_when_token_set():
    local_stack.check_prerequisites()  # must not raise, token set by the autouse fixture


def test_ensure_device_credential_secret_requires_real_values():
    with pytest.raises(LocalStackError, match="device_id/device_token"):
        local_stack._ensure_device_credential_secret("", "")


def test_ensure_device_credential_secret_uses_correct_key_names(monkeypatch):
    """Keys must be literally `device_id`/`token` - browseterm-device-agent's manifest mounts
    this Secret's keys as files at those exact paths (DEVICE_ID_FILE/DEVICE_TOKEN_FILE)."""
    calls = []
    monkeypatch.setattr(local_stack, "_create_or_update", lambda cmd: calls.append(cmd))
    local_stack._ensure_device_credential_secret("dev-123", "tok-abc")
    (cmd,) = calls
    assert "--from-literal=device_id=dev-123" in cmd
    assert "--from-literal=token=tok-abc" in cmd


def test_resolve_cloud_ingress_host_ip_uses_real_dns(monkeypatch):
    monkeypatch.setattr(local_stack, "BROWSETERM_CLOUD_API_URL", "https://app.browseterm.puhtaeto.com")
    monkeypatch.setattr(local_stack.socket, "gethostbyname", lambda host: "203.0.113.5")
    assert local_stack._resolve_cloud_ingress_host_ip() == "203.0.113.5"


def test_resolve_cloud_ingress_host_ip_raises_local_stack_error_on_dns_failure(monkeypatch):
    def fail(host):
        raise OSError("no such host")
    monkeypatch.setattr(local_stack.socket, "gethostbyname", fail)
    with pytest.raises(LocalStackError, match="could not resolve"):
        local_stack._resolve_cloud_ingress_host_ip()


def test_deploy_calls_every_step_in_order(monkeypatch):
    order = []
    for name in (
        "_ensure_namespace", "_ensure_internal_api_token_secret", "_ensure_device_credential_secret",
        "_deploy_gvisor_runtimeclass", "_deploy_minio", "_deploy_cert_manager", "_deploy_container_maker",
        "_build_device_agent_image", "_deploy_device_agent",
        "_deploy_status_monitor", "_deploy_reaper", "_deploy_socket_ssh",
    ):
        monkeypatch.setattr(local_stack, name, lambda *a, n=name, **kw: order.append(n))
    monkeypatch.setattr(local_stack, "_run", lambda *a, **kw: "")
    monkeypatch.setattr(local_stack, "_resolve_cloud_ingress_host_ip", lambda: "203.0.113.5")

    local_stack.deploy("dev-123", "tok-abc")

    assert order == [
        "_ensure_namespace", "_ensure_internal_api_token_secret", "_ensure_device_credential_secret",
        "_deploy_gvisor_runtimeclass", "_deploy_minio", "_deploy_cert_manager", "_deploy_container_maker",
        "_build_device_agent_image", "_deploy_device_agent",
        "_deploy_status_monitor", "_deploy_reaper", "_deploy_socket_ssh",
    ]


def test_deploy_reports_steps_via_on_step(monkeypatch):
    for name in (
        "_ensure_namespace", "_ensure_internal_api_token_secret", "_ensure_device_credential_secret",
        "_deploy_gvisor_runtimeclass", "_deploy_minio", "_deploy_cert_manager", "_deploy_container_maker",
        "_build_device_agent_image", "_deploy_device_agent",
        "_deploy_status_monitor", "_deploy_reaper", "_deploy_socket_ssh",
    ):
        monkeypatch.setattr(local_stack, name, lambda *a, **kw: None)
    monkeypatch.setattr(local_stack, "_run", lambda *a, **kw: "")
    monkeypatch.setattr(local_stack, "_resolve_cloud_ingress_host_ip", lambda: "203.0.113.5")

    events = []
    local_stack.deploy("dev-123", "tok-abc", on_step=lambda name, status, detail: events.append((name, status)))
    names = [n for n, _ in events]
    assert "Deploying MinIO" in names
    assert "Deploying Device Agent" in names
    assert "Deploying Socket-SSH" in names
    assert events[0] == ("Applying namespace and secrets", "started")


def test_deploy_stops_on_first_failure(monkeypatch):
    monkeypatch.setattr(local_stack, "_ensure_namespace", lambda: None)
    monkeypatch.setattr(local_stack, "_ensure_internal_api_token_secret", lambda: None)
    monkeypatch.setattr(local_stack, "_ensure_device_credential_secret", lambda *a: None)
    monkeypatch.setattr(local_stack, "_deploy_gvisor_runtimeclass", lambda: None)
    monkeypatch.setattr(local_stack, "_run", lambda *a, **kw: "")

    def boom():
        raise LocalStackError("minio failed")
    monkeypatch.setattr(local_stack, "_deploy_minio", boom)

    deployed_after = []
    monkeypatch.setattr(local_stack, "_deploy_cert_manager", lambda: deployed_after.append("cert-manager"))

    with pytest.raises(LocalStackError, match="minio failed"):
        local_stack.deploy("dev-123", "tok-abc")
    assert deployed_after == []


def test_repo_path_raises_when_checkout_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stack, "LOCAL_STACK_REPOS_DIR", str(tmp_path))
    with pytest.raises(LocalStackError, match="repo checkout not found"):
        local_stack._repo_path("container-maker")


def test_write_env_mk_writes_expected_content(monkeypatch, tmp_path):
    repo_dir = tmp_path / "browseterm_workload" / "status_monitor"
    repo_dir.mkdir(parents=True)
    monkeypatch.setattr(local_stack, "LOCAL_STACK_REPOS_DIR", str(tmp_path))
    local_stack._write_env_mk("status_monitor", {"NAMESPACE": "browseterm", "DEVICE_ID": "dev-1"})
    content = (repo_dir / "env.mk").read_text()
    assert "NAMESPACE=browseterm" in content
    assert "DEVICE_ID=dev-1" in content


def test_deploy_gvisor_runtimeclass_applies_the_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "gvisor-runtimeclass.yaml"
    manifest.write_text("kind: RuntimeClass\nmetadata:\n  name: gvisor\nhandler: runsc\n")
    monkeypatch.setattr(local_stack, "_GVISOR_RUNTIMECLASS_MANIFEST_PATH", str(manifest))
    calls = []
    monkeypatch.setattr(local_stack, "_run", lambda *a, **kw: calls.append((a, kw)))
    local_stack._deploy_gvisor_runtimeclass()
    (args, kwargs) = calls[0]
    assert args[0] == ["kubectl", "--context", local_stack.KUBE_CONTEXT, "apply", "-f", "-"]
    assert "RuntimeClass" in kwargs["input_text"]


def test_deploy_gvisor_runtimeclass_raises_when_manifest_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stack, "_GVISOR_RUNTIMECLASS_MANIFEST_PATH", str(tmp_path / "missing.yaml"))
    with pytest.raises(LocalStackError, match="gVisor RuntimeClass manifest not found"):
        local_stack._deploy_gvisor_runtimeclass()


def test_deploy_socket_ssh_no_longer_passes_cloud_config(monkeypatch):
    """Migration Part 13: socket-ssh talks to Device Agent's local API only - no
    BROWSETERM_CLOUD_API_URL/CLOUD_INGRESS_HOST(_IP)/DEVICE_TOKEN of any kind any more."""
    calls = []
    monkeypatch.setattr(local_stack, "_make", lambda *a, **kw: calls.append((a, kw)))
    local_stack._deploy_socket_ssh()
    (args, kwargs) = calls[0]
    assert args == ("socket-ssh", "prod_setup")
    assert "DEVICE_AGENT_LOCAL_API_URL" in kwargs
    assert "BROWSETERM_CLOUD_API_URL" not in kwargs
    assert "CLOUD_INGRESS_HOST" not in kwargs
    assert "CLOUD_INGRESS_HOST_IP" not in kwargs

"""
Deploys Browseterm's REAL current local-stack workloads into the Multipass/k3s cluster
`cluster_manager.py` creates: MinIO, cert-manager (mints container-maker's mTLS certs),
container-maker, browseterm-device-agent, status-monitor, reaper, socket-ssh (whose Pod also runs
the tunnel-registrar sidecar). This is a from-scratch rewrite, not an edit of the previous version
of this file - that version deployed browseterm-server-local (a full local browser-UI server) and
relied on the old global CLOUD_INTERNAL_API_TOKEN model for status_monitor/reaper/tunnel_registrar,
both of which are now wrong: migration Part 3 moved the browser UI to Cloud entirely, and Part 12
rewired those three workloads onto browseterm-device-agent's local API instead.

No ingress-nginx or MetalLB step, unlike the project's old pre-migration single-node-k3s reference
setup (scripts/deploy.k3s.sh, 00_docs/k3s_single_node.md). Verified before dropping them, not
assumed: no Service anywhere in the current local-stack manifests is `type: LoadBalancer` (MetalLB
exists only to hand those an external IP), and socket-ssh/infra/deployment/deployment.yaml still
declares an Ingress object (ingressClassName: nginx), but nothing routes to it externally any more
- the real terminal-traffic exposure path is ngrok, via the tunnel-registrar sidecar already in
that same manifest. A Kubernetes API server does not reject creating an Ingress that references a
non-existent IngressClass (no controller ever claims it, it just sits inert) - the Ingress object
still applies successfully without ingress-nginx installed, so skipping both whole steps (manifest
fetch, controller rollout wait, IPAddressPool) is safe, not a functional gap. `cluster_manager.
_install_k3s` disables k3s's own bundled Traefik/servicelb for the same reason - nothing here
needs either. gVisor is the one node-level piece from that old setup that IS still required (see
_deploy_gvisor_runtimeclass's own docstring) - installed by cluster_manager._install_gvisor and
wired in here as its own deploy step.

Reuses each component's own already-tested deploy script, same principle the old version of this
module used - but for container-maker specifically, its own `make prod_setup` target only forwards
9 of the 11 positional args its underlying script actually accepts (confirmed by reading
container-maker/Makefile and .../scripts/k8s/deployment/k8s-development-setup.sh directly -
CLOUD_INGRESS_HOST/CLOUD_INGRESS_HOST_IP are silently dropped by the Makefile wrapper, a real gap
in that repo, not something to route around by copying its YAML here), so this module calls that
one script directly instead of through `make`. Every other component's own Makefile target forwards
everything it needs.

Cloud is now a real external HTTPS host (`app.browseterm.puhtaeto.com`), not another local cluster
on the same Mac - the old CLOUD_INGRESS_HOST_IP value here used to resolve host.docker.internal
(a two-local-clusters-on-one-Mac workaround); now it's simply Cloud's real public IP via normal DNS,
making the hostAliases override in status_monitor/reaper/container-maker's manifests an accurate
pin rather than a workaround hack - still required because those manifests still declare the field,
but it now just resolves the same address DNS would find anyway.
"""
import os
import socket
import time
from typing import Optional
from urllib.parse import urlparse

from desktop.cluster_manager import KUBE_CONTEXT, ClusterError, StepCallback, _noop_step, _run, _run_step
from desktop.config import (
    BROWSETERM_CLOUD_API_URL,
    BROWSETERM_CLOUD_INTERNAL_API_TOKEN,
    CLOUD_INTERNAL_API_TOKEN_FILE,
    DOCKER_HUB_REPO_NAME,
    DOCKER_HUB_REPO_PASSWORD,
    LOCAL_STACK_REPOS_DIR,
)

NAMESPACE = "browseterm"
CERT_MANAGER_CRON_JOB_NAME = "cert-manager"
CONTAINER_MAKER_HOST = "container-maker-service"
CONTAINER_MAKER_CERTS_SECRET_NAME = "container-maker-service-certs"
DEVICE_AGENT_LOCAL_API_URL = "browseterm-device-agent-local.browseterm.svc.cluster.local:50061"
_IDLE_THRESHOLD_SECONDS = 7 * 24 * 3600  # matches reaper/src/config.py's own default
# container-maker still needs a real Ingress *host* string even though nothing routes to it
# externally any more (see module docstring) - a harmless, non-invented placeholder, not a real
# domain anyone needs to own or resolve.
_CONTAINER_MAKER_INGRESS_HOST = "container-maker.browseterm.local"

_REPO_DIRS = {
    "container-maker": "container-maker",
    "browseterm-device-agent": "browseterm-device-agent",
    "socket-ssh": "socket-ssh",
    "status_monitor": "browseterm_workload/status_monitor",
    "cert-manager": "browseterm_workload/cert-manager",
    "reaper": "browseterm_workload/reaper",
}
_MINIO_MANIFEST_PATH = os.path.join(LOCAL_STACK_REPOS_DIR, "browseterm-monorepo", "02_cluster_infra", "minio.yaml")
_GVISOR_RUNTIMECLASS_MANIFEST_PATH = os.path.join(
    LOCAL_STACK_REPOS_DIR, "browseterm-monorepo", "02_cluster_infra", "gvisor-runtimeclass.yaml"
)

_MAKE_TIMEOUT_SECONDS = 90.0
_CRONJOB_TRIGGER_TIMEOUT_SECONDS = 120.0


class LocalStackError(ClusterError):
    """Raised when deploying one of the local-stack workloads fails. Subclasses ClusterError so
    existing `except ClusterError` handling (desktop/api.py's setup_cluster) catches this too."""


def _repo_path(name: str) -> str:
    path = os.path.join(LOCAL_STACK_REPOS_DIR, _REPO_DIRS[name])
    if not os.path.isdir(path):
        raise LocalStackError(
            f"{name}'s repo checkout not found at {path} -- set LOCAL_STACK_REPOS_DIR if your "
            f"repos live somewhere other than {LOCAL_STACK_REPOS_DIR}."
        )
    return path


def _ensure_env_mk_exists(repo_path: str) -> None:
    env_mk = os.path.join(repo_path, "env.mk")
    if not os.path.exists(env_mk):
        open(env_mk, "a").close()


def _write_env_mk(repo_name: str, values: dict[str, str]) -> None:
    repo_path = _repo_path(repo_name)
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    with open(os.path.join(repo_path, "env.mk"), "w") as f:
        f.write(content)


def _make(repo_name: str, target: str, **variables: str) -> None:
    repo_path = _repo_path(repo_name)
    _ensure_env_mk_exists(repo_path)
    cmd = ["make", target] + [f"{key}={value}" for key, value in variables.items()]
    _run(cmd, timeout=_MAKE_TIMEOUT_SECONDS, cwd=repo_path)


def _run_script(repo_name: str, relative_script: str, *args: str, timeout: float = _MAKE_TIMEOUT_SECONDS) -> None:
    """Invokes a repo's own deploy script directly rather than through `make` - only used for
    container-maker's prod setup script (see module docstring for why: its Makefile target drops
    the last two args the script itself accepts)."""
    repo_path = _repo_path(repo_name)
    script = os.path.join(repo_path, relative_script)
    _run(["bash", script, *args], timeout=timeout, cwd=repo_path)


def _create_or_update(create_cmd: list[str]) -> None:
    """`kubectl create ... --dry-run=client -o yaml | kubectl apply -f -` without a shell pipe --
    idempotent, so re-running Setup against an already-partially-deployed cluster is safe."""
    yaml_text = _run(create_cmd + ["--dry-run=client", "-o", "yaml"])
    _run(["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"], input_text=yaml_text)


def check_prerequisites() -> None:
    """Called before the VM is even created (desktop/api.py's setup_cluster) - fails fast on the
    one thing this module cannot fix for the user: container-maker still calls Cloud directly with
    the global CLOUD_INTERNAL_API_TOKEN for one remaining DB-row lookup (documented, out-of-scope
    gap - see BROWSETERM_MIGRATION_PROGRESS.md's Part 12 section, "container-maker itself calls
    Cloud directly with the global CLOUD_INTERNAL_API_TOKEN"), and status_monitor/reaper/
    snapshot_job all still read the SAME shared Secret too (confirmed in their current manifests -
    Part 12 deliberately left this one credential in place, it did not remove it). Must be
    byte-identical to Cloud's own CLOUD_INTERNAL_API_TOKEN."""
    if not BROWSETERM_CLOUD_INTERNAL_API_TOKEN:
        raise LocalStackError(
            "BROWSETERM_CLOUD_INTERNAL_API_TOKEN is not set. It must be the exact same value as "
            f"Cloud's own CLOUD_INTERNAL_API_TOKEN -- put it in {CLOUD_INTERNAL_API_TOKEN_FILE} "
            "(see desktop/config.py), or set it as an environment variable to override that for "
            "one run."
        )


def _ensure_namespace() -> None:
    _create_or_update(["kubectl", "--context", KUBE_CONTEXT, "create", "namespace", NAMESPACE])


def _ensure_internal_api_token_secret() -> None:
    _create_or_update([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "secret", "generic",
        "browseterm-internal-api-token",
        f"--from-literal=CLOUD_INTERNAL_API_TOKEN={BROWSETERM_CLOUD_INTERNAL_API_TOKEN}",
    ])


def _ensure_device_credential_secret(device_id: str, device_token: str) -> None:
    """browseterm-device-agent/infra/deployment.yaml mounts this Secret's `device_id`/`token` keys
    as files at /etc/browseterm/device/{device_id,token} (DEVICE_ID_FILE/DEVICE_TOKEN_FILE) - key
    names must match exactly, a Kubernetes Secret volume mount names each file after its key. The
    token itself lives only in this process's Keychain (desktop/keychain.py) until now; this is
    the one place it gets copied into the cluster, and only as a Secret, never a plain env var or
    config file on disk anywhere in this repo."""
    if not device_id or not device_token:
        raise LocalStackError(
            "no device_id/device_token available -- log in and activate this device via Desktop "
            "before setting up the Cluster (Device Agent needs this device's own Bearer credential)"
        )
    _create_or_update([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "secret", "generic",
        "browseterm-device-credential",
        f"--from-literal=device_id={device_id}", f"--from-literal=token={device_token}",
    ])


def _resolve_cloud_ingress_host_ip() -> str:
    """Cloud's real public IP, via plain DNS - unlike the old host.docker.internal trick this
    replaces, there is nothing local-cluster-specific about this any more; it's the same address
    a browser resolving app.browseterm.puhtaeto.com would get."""
    hostname = urlparse(BROWSETERM_CLOUD_API_URL).hostname or "app.browseterm.puhtaeto.com"
    try:
        return socket.gethostbyname(hostname)
    except OSError as e:
        raise LocalStackError(f"could not resolve {hostname}'s IP: {e}") from e


def _deploy_minio() -> None:
    if not os.path.isfile(_MINIO_MANIFEST_PATH):
        raise LocalStackError(f"minio manifest not found at {_MINIO_MANIFEST_PATH}")
    with open(_MINIO_MANIFEST_PATH) as f:
        yaml_text = f.read()
    _run(["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"], input_text=yaml_text)


def _deploy_gvisor_runtimeclass() -> None:
    """Applies the cluster-scoped RuntimeClass mapping `gvisor` -> the `runsc` containerd handler
    cluster_manager._install_gvisor already installed on the node. container-maker's own manifest
    hardcodes `runtimeClassName: gvisor` unconditionally for every USER pod (see
    container-maker/infra/k8s/deployment/deployment.yaml's USER_POD_RUNTIME_CLASS env) - without
    this object existing, a pod referencing it simply never schedules (stays Pending forever), so
    this must run before any terminal gets created, not merely as a hardening nicety. Applied
    early in deploy() (right after namespace/secrets), matching the project's old
    scripts/setup.k3s.sh (node install) / scripts/deploy.k3s.sh (cluster object) split."""
    if not os.path.isfile(_GVISOR_RUNTIMECLASS_MANIFEST_PATH):
        raise LocalStackError(f"gVisor RuntimeClass manifest not found at {_GVISOR_RUNTIMECLASS_MANIFEST_PATH}")
    with open(_GVISOR_RUNTIMECLASS_MANIFEST_PATH) as f:
        yaml_text = f.read()
    _run(["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"], input_text=yaml_text)


def _trigger_and_wait_cronjob(cronjob_name: str, job_name_prefix: str, timeout: float) -> None:
    job_name = f"{job_name_prefix}-{int(time.time())}"
    _run([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "job", job_name,
        f"--from=cronjob/{cronjob_name}",
    ])
    _run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "wait", f"job/{job_name}",
         "--for=condition=complete", f"--timeout={int(timeout)}s"],
        timeout=timeout + 15,
    )


def _deploy_cert_manager() -> None:
    _make("cert-manager", "prod_setup", NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME)
    # A CronJob doesn't run on `kubectl apply` -- container-maker needs its minted
    # container-maker-service-certs Secret (and now Device Agent too, reading it directly via the
    # k8s API - see browseterm-device-agent/infra/deployment.yaml's Role) to exist before either
    # can start, so trigger + wait now.
    _trigger_and_wait_cronjob(
        CERT_MANAGER_CRON_JOB_NAME, "cert-manager-bootstrap", _CRONJOB_TRIGGER_TIMEOUT_SECONDS,
    )


def _deploy_container_maker(cloud_ingress_host_ip: str) -> None:
    """Calls the setup script directly (see module docstring) - full 11-positional-arg signature
    the script itself declares; there is no gVisor arg to pass at all - container-maker's manifest
    (infra/k8s/deployment/deployment.yaml) hardcodes USER_POD_RUNTIME_CLASS=gvisor unconditionally,
    so every USER pod it creates always requests the `gvisor` RuntimeClass regardless of what this
    function does. That's exactly why `create_cluster` installs runsc on the node
    (cluster_manager._install_gvisor) and `deploy()` applies the RuntimeClass object
    (_deploy_gvisor_runtimeclass) before this step runs - without both, every terminal a user
    creates would hang Pending forever, not merely run less sandboxed."""
    _run_script(
        "container-maker", "scripts/k8s/deployment/k8s-development-setup.sh",
        NAMESPACE, DOCKER_HUB_REPO_NAME, DOCKER_HUB_REPO_PASSWORD, _CONTAINER_MAKER_INGRESS_HOST,
        "minio", "minio-service:9000", "browseterm-snapshots", "false",
        BROWSETERM_CLOUD_API_URL, urlparse(BROWSETERM_CLOUD_API_URL).hostname or "", cloud_ingress_host_ip,
    )


def _build_device_agent_image() -> None:
    """browseterm-device-agent had no build/deploy tooling at all before this phase (its manifest's
    image field was a literal TODO placeholder) - build+push follows the exact same convention
    every other local-stack component already uses (see scripts/deployment/build.sh)."""
    _make("browseterm-device-agent", "prod_build", USER_NAME=DOCKER_HUB_REPO_NAME, REPO_NAME=DOCKER_HUB_REPO_NAME)


def _deploy_device_agent() -> None:
    _make("browseterm-device-agent", "prod_setup", NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME)


def _deploy_socket_ssh() -> None:
    """Migration Part 13: socket-ssh no longer calls Cloud directly at all - it consumes terminal
    tickets via a gRPC call to Device Agent's local API instead, so it needs no Cloud-facing
    config (BROWSETERM_CLOUD_API_URL/CLOUD_INGRESS_HOST(_IP)/DEVICE_TOKEN are all gone from its
    own Makefile/manifest now) and no `cloud_ingress_host_ip` parameter."""
    _make(
        "socket-ssh", "prod_setup",
        NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME,
        SOCKET_SSH_HOST="socketssh.browseterm.local",
        DEVICE_AGENT_LOCAL_API_URL=DEVICE_AGENT_LOCAL_API_URL,
        ALLOWED_ORIGINS_PROD="https://app.browseterm.puhtaeto.com",
    )


def _deploy_status_monitor(device_id: str, cloud_ingress_host_ip: str) -> None:
    _write_env_mk("status_monitor", {
        "NAMESPACE": NAMESPACE, "REPO_NAME": DOCKER_HUB_REPO_NAME,
        "BROWSETERM_CLOUD_API_URL": BROWSETERM_CLOUD_API_URL,
        "CLOUD_INGRESS_HOST": urlparse(BROWSETERM_CLOUD_API_URL).hostname or "",
        "CLOUD_INGRESS_HOST_IP": cloud_ingress_host_ip,
        "DEVICE_AGENT_LOCAL_API_URL": DEVICE_AGENT_LOCAL_API_URL,
        "DEVICE_ID": device_id,
    })
    _make("status_monitor", "dev_setup")


def _deploy_reaper(device_id: str, cloud_ingress_host_ip: str) -> None:
    _write_env_mk("reaper", {
        "NAMESPACE": NAMESPACE, "REPO_NAME": DOCKER_HUB_REPO_NAME,
        "BROWSETERM_CLOUD_API_URL": BROWSETERM_CLOUD_API_URL,
        "CLOUD_INGRESS_HOST": urlparse(BROWSETERM_CLOUD_API_URL).hostname or "",
        "CLOUD_INGRESS_HOST_IP": cloud_ingress_host_ip,
        "DEVICE_ID": device_id, "IDLE_THRESHOLD_SECONDS": str(_IDLE_THRESHOLD_SECONDS),
        "DEVICE_AGENT_LOCAL_API_URL": DEVICE_AGENT_LOCAL_API_URL,
    })
    _make("reaper", "dev_setup")


def deploy(
    device_id: Optional[str], device_token: Optional[str] = None,
    on_step: Optional[StepCallback] = None,
) -> None:
    """Deploys every local-stack workload in the order each one's own manifest requires: the
    internal-api-token and device-credential Secrets before anything that reads them; minio and
    cert-manager are self-contained; container-maker needs both Secrets plus minio; Device Agent
    needs its own device-credential Secret plus container-maker's minted certs (read live via the
    k8s API, not a startup-time dependency, but nothing useful happens before cert-manager has run
    regardless); status_monitor/reaper/socket-ssh all need Device Agent's local API reachable,
    which just means the Service existing (ClusterIP Services resolve immediately on creation,
    there's no real ordering requirement here beyond "created", unlike a Deployment needing to be
    Ready) - deployed after Device Agent regardless, for readability of the sequence, not because
    it's strictly required.

    snapshot_job is deliberately not deployed here: it has no standing Deployment/manifest of its
    own (container-maker spawns it as a one-off Job per save, same as before this rewrite) - its
    image still needs to exist for that to work, tracked as a known gap below.
    """
    check_prerequisites()
    _run_step(on_step, "Applying namespace and secrets", lambda: (
        _run(["kubectl", "config", "use-context", KUBE_CONTEXT]),
        _ensure_namespace(),
        _ensure_internal_api_token_secret(),
        _ensure_device_credential_secret(device_id or "", device_token or ""),
    ))
    _run_step(on_step, "Applying gVisor RuntimeClass", _deploy_gvisor_runtimeclass)
    _run_step(on_step, "Deploying MinIO", _deploy_minio)
    _run_step(on_step, "Deploying cert-manager", _deploy_cert_manager)
    cloud_ingress_host_ip = _resolve_cloud_ingress_host_ip()
    _run_step(on_step, "Deploying Container Maker", lambda: _deploy_container_maker(cloud_ingress_host_ip))
    _run_step(on_step, "Building Device Agent image", _build_device_agent_image)
    _run_step(on_step, "Deploying Device Agent", _deploy_device_agent)
    _run_step(on_step, "Deploying status-monitor", lambda: _deploy_status_monitor(device_id or "", cloud_ingress_host_ip))
    _run_step(on_step, "Deploying reaper", lambda: _deploy_reaper(device_id or "", cloud_ingress_host_ip))
    _run_step(on_step, "Deploying Socket-SSH", _deploy_socket_ssh)

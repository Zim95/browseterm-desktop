"""
Deploys BrowseTerm's real local-stack workloads into the k3d cluster `cluster_manager.py` creates
-- ingress-nginx (replacing k3s's bundled Traefik, SETUP-LOCAL.md step 2 -- see `deploy()`),
container-maker, socket-ssh, browseterm-server-local, status_monitor, cert-manager, reaper, minio
-- so the Cluster section's Setup button stands up a genuinely working Local control plane, not
just a bare cluster. snapshot_job is deliberately absent here: it has no deployment manifest of
its own (only `browseterm_workload/snapshot_job/infra/development/`) -- container-maker spawns it
dynamically as a Job at save-time, so there's nothing for Setup to deploy upfront (it stays in the
pod-monitor's filter list only, see `cluster_manager.MONITORED_WORKLOAD_PREFIXES`).

Reuses each component's OWN, already-tested `make prod_setup`/`dev_setup` target (invoked as
`make <target> VAR=value ...`, which GNU Make lets override values an `include env.mk` would
otherwise supply) rather than re-implementing envsubst/kubectl-apply logic here -- this is
deliberate: SETUP-LOCAL.md's own manual procedure IS this same set of make targets, and reusing
them keeps this module automatically in sync with any future change to those scripts instead of
maintaining a second, parallel implementation that could silently drift from the real one.

Two required exceptions to "just invoke the make target": status_monitor's and reaper's own
`dev_setup` scripts `source env.mk` directly rather than taking values as script arguments
(confirmed by reading both scripts) -- for those two specifically, a real env.mk file is written
into the repo before invoking `make`, since there's no other way to hand them a value. Every repo
here also does `include env.mk` at the top of its Makefile, which errors out if the file doesn't
exist at all -- `_ensure_env_mk_exists` touches an empty placeholder first for the repos that take
their values as make command-line overrides instead (those values take precedence over whatever an
empty/placeholder env.mk would supply).

One value this module CANNOT invent: `BROWSETERM_CLOUD_INTERNAL_API_TOKEN` must be byte-identical
to Cloud's own `CLOUD_INTERNAL_API_TOKEN` (SETUP-LOCAL.md step 5's own warning, confirmed again in
browseterm-server-local/infra/deployment/deployment.yaml's inline comment) -- every internal-token-
gated Local-to-Cloud call silently 401s otherwise. `check_prerequisites()` refuses to deploy
anything at all if it's unset (or if the required /etc/hosts entries are missing), rather than
standing up a stack that fails confusingly later -- checked BEFORE the k3d cluster is even created,
so a doomed config fails in milliseconds, not after a ~90s cluster-create cycle.

Real, deliberate local-only compromise found while researching this: container-maker's prod
manifest hardcodes `USER_POD_RUNTIME_CLASS=gvisor` as a literal (not an envsubst placeholder) --
correct on the single-node k3s PROD host `setup.k3s.sh` installs runsc/registers the RuntimeClass
on, but a k3d-in-Docker local cluster has no gVisor RuntimeClass at all, so every workspace pod
container-maker tried to create would fail to schedule. `_strip_gvisor_runtime_class` patches this
back out post-apply -- container-maker's own `pod_manager.py` already treats an empty value as
"omit the field entirely" (`resource_config.py`: `os.getenv(...) or None`), exactly what its own
dev/docker-desktop manifest already does. Practical consequence: local workspace pods run
unsandboxed (node-default runc), not gVisor-isolated -- acceptable here because the local cluster
is single-tenant (only the machine's own owner ever uses it), unlike the shared PROD cluster this
protection matters for.
"""
import json
import os
import time
from typing import Optional

from desktop.cluster_manager import KUBE_CONTEXT, ClusterError, _run
from desktop.config import (
    BROWSETERM_CLOUD_API_URL,
    BROWSETERM_CLOUD_INTERNAL_API_TOKEN,
    CLOUD_INTERNAL_API_TOKEN_FILE,
    DOCKER_HUB_REPO_NAME,
    DOCKER_HUB_REPO_PASSWORD,
    LOCAL_STACK_REPOS_DIR,
)

NAMESPACE = "browseterm"
INGRESS_HOST = "browseterm.local.com"
SOCKET_SSH_HOST = "socketssh.local"
SOCKET_SSH_WSS_URL = f"ws://{SOCKET_SSH_HOST}"
CLOUD_INGRESS_HOST = "browseterm.cloud.com"
CERT_MANAGER_CRON_JOB_NAME = "cert-manager"
CONTAINER_MAKER_HOST = "container-maker-service"
CONTAINER_MAKER_PORT = "50052"
CONTAINER_MAKER_CERTS_SECRET_NAME = "container-maker-service-certs"
_IDLE_THRESHOLD_SECONDS = 7 * 24 * 3600  # matches reaper/src/config.py's own default

_REPO_DIRS = {
    "container-maker": "container-maker",
    "socket-ssh": "socket-ssh",
    "browseterm-server-local": "browseterm-server-local",
    "status_monitor": "browseterm_workload/status_monitor",
    "cert-manager": "browseterm_workload/cert-manager",
    "reaper": "browseterm_workload/reaper",
}
_MINIO_MANIFEST_PATH = os.path.join(LOCAL_STACK_REPOS_DIR, "browseterm-monorepo", "02_cluster_infra", "minio.yaml")

# SETUP-LOCAL.md step 2, exact same manifest URL/version -- k3s's bundled Traefik squats on the
# same host ports 80/443 every Ingress this project deploys needs (a real, previously-hit gotcha:
# leaving Traefik running means browseterm-server-local/socket-ssh's Ingresses get created but
# nothing ever routes to them, a 404 from Traefik's own default backend, not from the app).
_INGRESS_NGINX_MANIFEST_URL = (
    "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/"
    "deploy/static/provider/cloud/deploy.yaml"
)

_MAKE_TIMEOUT_SECONDS = 60.0
_CRONJOB_TRIGGER_TIMEOUT_SECONDS = 120.0
_INGRESS_NGINX_ROLLOUT_TIMEOUT_SECONDS = 195.0


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


def _create_or_update(create_cmd: list[str]) -> None:
    """`kubectl create ... --dry-run=client -o yaml | kubectl apply -f -` without a shell pipe --
    same idempotent-apply pattern SETUP-LOCAL.md itself uses for namespace creation, so re-running
    Setup against an already-partially-deployed cluster is safe."""
    yaml_text = _run(create_cmd + ["--dry-run=client", "-o", "yaml"])
    _run(["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", "-"], input_text=yaml_text)


def _etc_hosts_maps_to_loopback(hostname: str, path: str = "/etc/hosts") -> bool:
    '''True only if /etc/hosts actually maps `hostname` to 127.0.0.1 - not just that the hostname
    string appears somewhere in the file. A real bug this project hit live: `socketssh.local` had
    a real entry left over from an old cluster (a stale, unrelated IP), which a plain substring
    check would call "present" - every SSH connect attempt silently went nowhere, with no error
    at all, since the browser was dialing a dead address before ever reaching this project's
    current cluster. `path` is overridable only so tests don't need to touch the real file.'''
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) >= 2 and parts[0] == "127.0.0.1" and hostname in parts[1:]:
                    return True
        return False
    except OSError:
        return False


def check_prerequisites() -> None:
    """Called before the k3d cluster is even created (desktop/api.py's setup_cluster) -- fails
    fast on the one thing this module cannot fix for the user (the shared internal API token) and
    the one thing it must never silently do for them (editing /etc/hosts needs sudo)."""
    if not BROWSETERM_CLOUD_INTERNAL_API_TOKEN:
        raise LocalStackError(
            "BROWSETERM_CLOUD_INTERNAL_API_TOKEN is not set. It must be the exact same value as "
            "Cloud's own CLOUD_INTERNAL_API_TOKEN (see SETUP-LOCAL.md step 5) -- put it in "
            f"{CLOUD_INTERNAL_API_TOKEN_FILE} (see desktop/config.py), or set it as an "
            "environment variable to override that for one run."
        )
    wrong = [h for h in (INGRESS_HOST, SOCKET_SSH_HOST) if not _etc_hosts_maps_to_loopback(h)]
    if wrong:
        # Deliberately not "append a correct line" - /etc/hosts resolves top-down, so appending
        # below an existing WRONG entry (e.g. a stale IP left over from an old cluster) would
        # still lose to it, exactly the class of bug this check exists to catch. sed -i replaces
        # any existing line for that hostname in place; if there's no line at all yet it's a
        # plain append via the same command (sed with no match just leaves the file unchanged, so
        # this alone doesn't cover a truly-missing entry - flagged in the message either way).
        fixes = "; ".join(
            f"sudo sed -i '' -E 's/^.*[[:space:]]{h}$/127.0.0.1\\t{h}/' /etc/hosts "
            f"|| echo '127.0.0.1\\t{h}' | sudo tee -a /etc/hosts"
            for h in wrong
        )
        raise LocalStackError(
            "/etc/hosts has no correct (127.0.0.1) entry for: " + ", ".join(wrong) + " - either "
            "missing entirely, or present but pointing at the wrong IP (check for a stale entry "
            "from an old cluster first, since sed won't fix a mismatched line if the pattern "
            f"doesn't match it exactly). Run: {fixes}"
        )


def _ensure_ingress_nginx() -> None:
    '''SETUP-LOCAL.md step 2. Idempotent: if ingress-nginx-controller already exists (a previous
    Setup run already did this), skips straight past rather than re-applying the manifest and
    re-waiting on its rollout on every single Setup click. A cluster created before this check
    existed - Traefik still running, ingress-nginx never installed - self-heals the next time
    Setup runs, without needing a Teardown first.'''
    existing = _run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", "ingress-nginx", "get", "deployment",
         "ingress-nginx-controller", "--ignore-not-found", "-o", "name"],
        timeout=15,
    )
    if existing.strip():
        return
    _run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", "kube-system", "delete", "helmchart", "traefik",
         "--ignore-not-found"],
        timeout=30,
    )
    _run(["kubectl", "--context", KUBE_CONTEXT, "apply", "-f", _INGRESS_NGINX_MANIFEST_URL], timeout=60)
    _run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", "ingress-nginx", "rollout", "status",
         "deploy/ingress-nginx-controller", "--timeout=180s"],
        timeout=_INGRESS_NGINX_ROLLOUT_TIMEOUT_SECONDS,
    )


def _ensure_namespace() -> None:
    _create_or_update(["kubectl", "--context", KUBE_CONTEXT, "create", "namespace", NAMESPACE])


def _ensure_internal_api_token_secret() -> None:
    _create_or_update([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "secret", "generic",
        "browseterm-internal-api-token",
        f"--from-literal=CLOUD_INTERNAL_API_TOKEN={BROWSETERM_CLOUD_INTERNAL_API_TOKEN}",
    ])


def _ensure_db_placeholder_secret() -> None:
    """browseterm-server-local's own P06 note (SETUP-LOCAL.md step 3): these values are never
    actually used for a real Postgres connection in the V2 local-cluster architecture -- the
    Secret just needs to exist with these keys for the pod to start."""
    _create_or_update([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "create", "secret", "generic",
        "browseterm-db-credentials",
        "--from-literal=DB_HOST=unused", "--from-literal=DB_PORT=5432",
        "--from-literal=DB_USERNAME=unused", "--from-literal=DB_PASSWORD=unused",
        "--from-literal=DB_DATABASE=unused",
    ])


def _resolve_cloud_ingress_host_ip() -> str:
    """The host.docker.internal IP two separate k3d clusters on this same Mac need to reach each
    other through -- see SETUP-LOCAL.md's identically-documented manual step."""
    output = _run(["docker", "run", "--rm", "alpine", "getent", "hosts", "host.docker.internal"], timeout=30)
    tokens = output.split()
    if not tokens:
        raise LocalStackError("could not resolve host.docker.internal's IP (docker run alpine getent hosts)")
    return tokens[0]


def _deploy_minio() -> None:
    if not os.path.isfile(_MINIO_MANIFEST_PATH):
        raise LocalStackError(f"minio manifest not found at {_MINIO_MANIFEST_PATH}")
    with open(_MINIO_MANIFEST_PATH) as f:
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
    # container-maker-service-certs Secret to exist before it can start, so trigger + wait now.
    _trigger_and_wait_cronjob(
        CERT_MANAGER_CRON_JOB_NAME, "cert-manager-bootstrap", _CRONJOB_TRIGGER_TIMEOUT_SECONDS,
    )


def _strip_gvisor_runtime_class() -> None:
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": "container-maker", "env": [{"name": "USER_POD_RUNTIME_CLASS", "value": ""}]}
    ]}}}}
    _run([
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "patch", "deployment", "container-maker",
        "--type=strategic", "-p", json.dumps(patch),
    ])


def _deploy_container_maker(cloud_ingress_host_ip: str) -> None:
    _make(
        "container-maker", "prod_setup",
        NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME, REPO_PASSWORD=DOCKER_HUB_REPO_PASSWORD,
        INGRESS_HOST=INGRESS_HOST, STORAGE_LAYER="minio", MINIO_ENDPOINT="minio-service:9000",
        MINIO_BUCKET="browseterm-snapshots", MINIO_SECURE="false",
        BROWSETERM_CLOUD_API_URL=BROWSETERM_CLOUD_API_URL,
        CLOUD_INGRESS_HOST=CLOUD_INGRESS_HOST, CLOUD_INGRESS_HOST_IP=cloud_ingress_host_ip,
    )
    _strip_gvisor_runtime_class()


def _deploy_socket_ssh(cloud_ingress_host_ip: str) -> None:
    _make(
        "socket-ssh", "prod_setup",
        NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME, SOCKET_SSH_HOST=SOCKET_SSH_HOST,
        BROWSETERM_CLOUD_API_URL=BROWSETERM_CLOUD_API_URL, ALLOWED_ORIGINS_PROD=f"http://{INGRESS_HOST}",
        CLOUD_INGRESS_HOST=CLOUD_INGRESS_HOST, CLOUD_INGRESS_HOST_IP=cloud_ingress_host_ip,
    )


def _deploy_browseterm_server_local(cloud_ingress_host_ip: str) -> None:
    _make(
        "browseterm-server-local", "prod_setup",
        NAMESPACE=NAMESPACE, REPO_NAME=DOCKER_HUB_REPO_NAME,
        CONTAINER_MAKER_HOST=CONTAINER_MAKER_HOST, CONTAINER_MAKER_PORT=CONTAINER_MAKER_PORT,
        CONTAINER_MAKER_CERTS_SECRET_NAME=CONTAINER_MAKER_CERTS_SECRET_NAME,
        CERT_MANAGER_CRON_JOB_NAME=CERT_MANAGER_CRON_JOB_NAME,
        BROWSETERM_CLOUD_API_URL=BROWSETERM_CLOUD_API_URL,
        POSTGRES_HOST="unused", POSTGRES_PORT="5432", POSTGRES_USER="unused",
        POSTGRES_PASSWORD="unused", POSTGRES_DB="unused",
        SOCKET_SSH_HOST=SOCKET_SSH_HOST, SOCKET_SSH_WSS_URL=SOCKET_SSH_WSS_URL,
        INGRESS_HOST=INGRESS_HOST, COOKIE_SECURE="false", COOKIE_SAMESITE="lax",
        # payment-gateway isn't deployed locally -- these are plain strings looked up lazily at
        # request time (not secretKeyRefs), so a pod referencing a nonexistent Secret name here is
        # safe; only payment endpoints themselves would fail, same as today's manual dev setup.
        PAYMENT_GATEWAY_HOST="payment-gateway-service", PAYMENT_GATEWAY_PORT="50053",
        PAYMENT_GATEWAY_CERTS_SECRET_NAME="payment-gateway-service-certs",
        CLOUD_INGRESS_HOST=CLOUD_INGRESS_HOST, CLOUD_INGRESS_HOST_IP=cloud_ingress_host_ip,
        EXPECTED_KUBE_CONTEXT=KUBE_CONTEXT,
    )


def _deploy_status_monitor(cloud_ingress_host_ip: str) -> None:
    _write_env_mk("status_monitor", {
        "NAMESPACE": NAMESPACE, "REPO_NAME": DOCKER_HUB_REPO_NAME,
        "BROWSETERM_CLOUD_API_URL": BROWSETERM_CLOUD_API_URL,
        "CLOUD_INGRESS_HOST": CLOUD_INGRESS_HOST, "CLOUD_INGRESS_HOST_IP": cloud_ingress_host_ip,
    })
    _make("status_monitor", "dev_setup")


def _deploy_reaper(device_id: str, cloud_ingress_host_ip: str) -> None:
    _write_env_mk("reaper", {
        "NAMESPACE": NAMESPACE, "REPO_NAME": DOCKER_HUB_REPO_NAME,
        "BROWSETERM_CLOUD_API_URL": BROWSETERM_CLOUD_API_URL,
        "CLOUD_INGRESS_HOST": CLOUD_INGRESS_HOST, "CLOUD_INGRESS_HOST_IP": cloud_ingress_host_ip,
        "DEVICE_ID": device_id, "IDLE_THRESHOLD_SECONDS": str(_IDLE_THRESHOLD_SECONDS),
        "CONTAINER_MAKER_HOST": CONTAINER_MAKER_HOST, "CONTAINER_MAKER_PORT": CONTAINER_MAKER_PORT,
        "CONTAINER_MAKER_CERTS_SECRET_NAME": CONTAINER_MAKER_CERTS_SECRET_NAME,
    })
    _make("reaper", "dev_setup")


def deploy(device_id: Optional[str]) -> None:
    """Deploys every local-stack workload in the order each one's own manifest requires (see
    module docstring): ingress-nginx first (SETUP-LOCAL.md step 2 -- nothing Ingress-routed is
    reachable at all until Traefik is out of the way and ingress-nginx is up); minio and
    cert-manager are self-contained; container-maker needs both of those plus the
    internal-api-token Secret; socket-ssh has no dependencies at all; browseterm-server-local/
    status_monitor/reaper each need only the internal-api-token Secret (reaper additionally needs
    device_id, which is already known post-login by the time this Cluster-section button is
    reachable at all).

    Every workload that calls Cloud's API also needs `cloud_ingress_host_ip` for the same
    hostAliases override browseterm-server-local's own manifest already required -- discovered
    live this session: on a single-Mac two-cluster dev setup, BROWSETERM_CLOUD_API_URL's hostname
    resolves inside browseterm-k3s-local's own pods to whatever the Mac's own /etc/hosts maps it
    to (127.0.0.1, for the developer's browser) rather than the real Cloud cluster, so any Cloud
    API call from a pod without this override gets "Connection refused" against its own loopback.
    This is why status_monitor could never report a container's Running status back to Cloud
    (stuck Pending forever despite a healthy pod), and separately why container-maker's save
    reconciler and socket-ssh's ws_token lookups failed outright -- container-maker's and
    socket-ssh's own `make prod_setup` scripts (positional-arg, not a sourced env.mk) now accept
    it as a trailing optional arg, same fix, just threaded through differently."""
    check_prerequisites()
    _run(["kubectl", "config", "use-context", KUBE_CONTEXT])
    _ensure_ingress_nginx()
    _ensure_namespace()
    _ensure_internal_api_token_secret()
    _ensure_db_placeholder_secret()
    _deploy_minio()
    _deploy_cert_manager()
    cloud_ingress_host_ip = _resolve_cloud_ingress_host_ip()
    _deploy_container_maker(cloud_ingress_host_ip)
    _deploy_socket_ssh(cloud_ingress_host_ip)
    _deploy_browseterm_server_local(cloud_ingress_host_ip)
    _deploy_status_monitor(cloud_ingress_host_ip)
    _deploy_reaper(device_id or "", cloud_ingress_host_ip)

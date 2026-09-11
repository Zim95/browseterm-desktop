"""
Local k3d cluster lifecycle for the Cluster section of the Device page (FINAL_BROWSETERM_V2_
IMPLEMENTATION_PLAN.md section 5: "local VM/k3s startup" is an explicit Desktop responsibility).
Shells out to `k3d`/`docker`/`kubectl` -- this project's existing convention for cluster lifecycle
everywhere else (see `~/browseterm/browseterm-monorepo/SETUP-LOCAL.md`), so no Kubernetes Python
client dependency is needed for the small set of operations here.

Cluster name/port mapping match SETUP-LOCAL.md exactly, so a cluster created by this module is the
same `browseterm-k3s-local` a developer would otherwise create by hand, and the manual
build/deploy steps in that doc still work against it unmodified.

k3d's own CLI has no per-cluster CPU limit flag (only `--servers-memory`/`--agents-memory`, applied
at `cluster create` time) -- CPU is capped after creation via `docker update --cpus` on each node
container, since k3d creates its nodes as plain Docker containers labelled `k3d.cluster=<name>`.
This is best-effort: the cluster is already up by that point regardless of whether the CPU cap
itself succeeds.
"""
import json
import subprocess
from typing import Any

CLUSTER_NAME = "browseterm-k3s-local"
KUBE_CONTEXT = f"k3d-{CLUSTER_NAME}"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_CREATE_TIMEOUT_SECONDS = 150.0
_DELETE_TIMEOUT_SECONDS = 90.0

# The pod monitor shows only the workloads that need to stay continuously running to keep the
# local cluster usable -- verified against each component's actual manifest (not assumed) rather
# than going off a remembered list, since the two categories look superficially similar but need
# opposite treatment here:
#   Deployments (always-on -- monitored): container-maker, socket-ssh, browseterm-server (the
#   Deployment name browseterm-server-local's own manifest actually uses), status-monitor, minio,
#   ingress-nginx-controller.
#   CronJobs/Jobs (come up only when triggered -- deliberately NOT monitored, an "up/down" read
#   doesn't mean anything for a pod that's supposed to complete and disappear): cert-manager
#   (CronJob, mints/renews certs periodically), reaper (CronJob, hourly idle sweep), snapshot-job
#   (no persistent Deployment at all in prod -- container-maker spawns it as a one-off Job per
#   save), minio-createbucket (one-shot Job minio.yaml runs once at deploy time to create the
#   bucket, then exits).
# Matched by pod-name PREFIX rather than a namespace or label selector: container-maker/socket-ssh/
# browseterm-server-local each deploy as EITHER a bare Deployment name (prod-style manifest) or a
# `-development` suffixed one (dev-style manifest) depending on which was actually applied to this
# cluster, and a bare prefix matches both without caring which.
MONITORED_WORKLOAD_PREFIXES = (
    "container-maker",
    "socket-ssh",
    "browseterm-server",
    "status-monitor",
    "minio",
    "ingress-nginx",
)

# minio's own pod name ("minio-<hash>-<hash>") and its one-shot bucket-creation Job's pod name
# ("minio-createbucket-<hash>") both start with "minio-", so the MONITORED_WORKLOAD_PREFIXES
# prefix match alone can't tell them apart -- this is checked first in list_pods() to carve the
# triggered-only Job back out.
_EXCLUDED_WORKLOAD_PREFIXES = (
    "minio-createbucket",
)


class ClusterError(Exception):
    """Raised for a failed/missing CLI tool, a timeout, or a non-zero exit from k3d/docker/kubectl."""


def _run(
    cmd: list[str], timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    cwd: "str | None" = None, input_text: "str | None" = None,
) -> str:
    """Shared low-level runner -- also used by `local_stack.py` (`cwd` to invoke a sibling repo's
    own `make` target in its own directory, `input_text` to pipe rendered YAML into `kubectl apply
    -f -` without a shell pipe)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, input=input_text)
    except FileNotFoundError as e:
        raise ClusterError(f"'{cmd[0]}' not found -- is it installed and on PATH?") from e
    except subprocess.TimeoutExpired as e:
        raise ClusterError(f"'{' '.join(cmd)}' timed out after {timeout:.0f}s") from e
    if result.returncode != 0:
        raise ClusterError(result.stderr.strip() or f"'{' '.join(cmd)}' failed")
    return result.stdout


def cluster_exists() -> bool:
    clusters = json.loads(_run(["k3d", "cluster", "list", "-o", "json"]) or "[]")
    return any(c.get("name") == CLUSTER_NAME for c in clusters)


def create_cluster(cpu_cores: int, memory_gb: float) -> None:
    if cluster_exists():
        return
    memory_arg = f"{memory_gb:g}G"
    _run(
        [
            "k3d", "cluster", "create", CLUSTER_NAME,
            "-p", "80:80@loadbalancer",
            "--servers-memory", memory_arg,
            "--agents-memory", memory_arg,
            "--wait", "--timeout", "90s",
        ],
        timeout=_CREATE_TIMEOUT_SECONDS,
    )
    _apply_cpu_limit(cpu_cores)


def _apply_cpu_limit(cpu_cores: int) -> None:
    try:
        names = _run(
            ["docker", "ps", "--filter", f"label=k3d.cluster={CLUSTER_NAME}", "--format", "{{.Names}}"],
            timeout=15,
        ).split()
        for name in names:
            _run(["docker", "update", "--cpus", str(cpu_cores), name], timeout=15)
    except ClusterError:
        pass  # best-effort -- the cluster itself is already up regardless.


def delete_cluster() -> None:
    _run(["k3d", "cluster", "delete", CLUSTER_NAME], timeout=_DELETE_TIMEOUT_SECONDS)


def is_monitored_pod(name: str) -> bool:
    '''The one place the monitored/excluded prefix lists are actually consulted -- shared by
    list_pods() and this module's own tests, so a test can never drift from what production code
    actually does.'''
    return name.startswith(MONITORED_WORKLOAD_PREFIXES) and not name.startswith(_EXCLUDED_WORKLOAD_PREFIXES)


# Container waiting-state reasons that mean "actually stuck," not "still starting up" -- a fresh
# Deployment's pod normally spends tens of seconds in phase Pending/Running with no
# containerStatuses[].state.waiting at all (or a benign one like ContainerCreating/PodInitializing)
# before settling into Running/Ready on its own. None of that should ever suggest a restart would
# help. These reasons are the ones where it actually would.
_CRASH_WAITING_REASONS = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
})


def _is_crashing(phase: str, statuses: list[dict[str, Any]]) -> bool:
    if phase == "Failed":
        return True
    for status in statuses:
        state = status.get("state", {})
        if state.get("waiting", {}).get("reason") in _CRASH_WAITING_REASONS:
            return True
        if state.get("terminated", {}).get("reason") == "Error":
            return True
    return False


def list_pods() -> list[dict[str, Any]]:
    if not cluster_exists():
        return []
    data = json.loads(_run(["kubectl", "--context", KUBE_CONTEXT, "get", "pods", "-A", "-o", "json"]) or "{}")
    pods = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        if not is_monitored_pod(name):
            continue
        statuses = item.get("status", {}).get("containerStatuses", [])
        phase = item.get("status", {}).get("phase", "Unknown")
        pods.append({
            "namespace": item["metadata"]["namespace"],
            "name": name,
            "phase": phase,
            "ready": f"{sum(1 for s in statuses if s.get('ready'))}/{len(statuses)}",
            "restarts": sum(s.get("restartCount", 0) for s in statuses),
            "crashing": _is_crashing(phase, statuses),
        })
    return pods


def restart_pod(namespace: str, name: str) -> None:
    '''Deletes the named pod outright rather than looking up and rolling its owning Deployment --
    every workload in MONITORED_WORKLOAD_PREFIXES is a Deployment (see that constant's own
    docstring; CronJob-spawned pods were deliberately excluded from monitoring specifically
    because there is no "restart" that makes sense for a pod meant to complete and disappear), so
    the owning ReplicaSet recreates it immediately - the standard, minimal way to force-restart
    exactly one pod without needing to resolve pod -> ReplicaSet -> Deployment ownership first.'''
    _run(["kubectl", "--context", KUBE_CONTEXT, "-n", namespace, "delete", "pod", name])

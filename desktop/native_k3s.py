"""
Native (no VM) k3s cluster lifecycle for headless Linux (Part 18: "Linux: native k3s, installed
and managed by a headless Browseterm CLI/system service" - unlike macOS/Windows, which get a
Browseterm-managed Multipass VM, see cluster_manager.py).

Deliberately mirrors cluster_manager.py's own install steps (same k3s flags, same vendored gVisor
install script - imported directly from there rather than duplicated, since the script itself has
nothing VM-specific in it) with the VM layer removed entirely: k3s installs straight onto this
host via the official install script, no `multipass exec` wrapper, no VM-IP kubeconfig rewrite
(k3s's own kubeconfig already correctly says `https://127.0.0.1:6443` here, since this process
*is* the node).

KUBE_CONTEXT is deliberately the exact same string cluster_manager.py uses ("browseterm") - every
function in local_stack.py imports that constant by value from cluster_manager, not a live
reference, so reusing the identical context name here means local_stack.py needs zero changes to
work against a native install instead of a Multipass one. This is what "reuse local_stack.py
directly" actually requires structurally, not just a convenient coincidence.
"""
import os
import re
import subprocess
from typing import Optional

from desktop.cluster_manager import (
    _GVISOR_INSTALL_SCRIPT,
    ClusterError,
    KUBE_CONTEXT,
    StepCallback,
    _run,
    _run_step,
    is_monitored_pod,
    list_pods,
    restart_pod,
)

K3S_VERSION = os.getenv("BROWSETERM_K3S_VERSION", "v1.31.2+k3s1")
K3S_KUBECONFIG_PATH = "/etc/rancher/k3s/k3s.yaml"
KUBE_CONFIG_PATH = os.path.expanduser(os.getenv("KUBECONFIG", "~/.kube/config"))

_K3S_INSTALL_TIMEOUT_SECONDS = 150.0
_K3S_READY_TIMEOUT_SECONDS = 90.0
_GVISOR_INSTALL_TIMEOUT_SECONDS = 120.0
_UNINSTALL_TIMEOUT_SECONDS = 60.0

K3S_UNINSTALL_SCRIPT = "/usr/local/bin/k3s-uninstall.sh"


def require_root() -> None:
    """k3s install/uninstall and reading root-owned k3s.yaml both need root - fail fast with a
    clear message rather than a confusing permission-denied halfway through."""
    if os.geteuid() != 0:
        raise ClusterError("this command must be run as root (sudo browseterm ...) - k3s install/uninstall needs it")


def cluster_exists() -> bool:
    return os.path.isfile(K3S_KUBECONFIG_PATH)


def _install_k3s() -> None:
    """Idempotent - see cluster_manager._install_k3s's own docstring for why the same flags
    (bundled Traefik/servicelb disabled, local-path kept) apply here unchanged."""
    install_cmd = (
        f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={K3S_VERSION} "
        "INSTALL_K3S_EXEC='--write-kubeconfig-mode 644 --disable traefik --disable servicelb' sh -"
    )
    _run(["bash", "-c", install_cmd], timeout=_K3S_INSTALL_TIMEOUT_SECONDS)
    _wait_for_node_ready()


def _wait_for_node_ready() -> None:
    _run(
        ["k3s", "kubectl", "wait", "--for=condition=Ready", "node", "--all", "--timeout=90s"],
        timeout=_K3S_READY_TIMEOUT_SECONDS,
    )


def _install_gvisor() -> None:
    """Same vendored script cluster_manager._install_gvisor runs inside the Multipass VM - see
    that function's own docstring. Run directly here, no VM to shell into."""
    _run(["bash", "-c", _GVISOR_INSTALL_SCRIPT], timeout=_GVISOR_INSTALL_TIMEOUT_SECONDS)
    _wait_for_node_ready()


def _merge_kubeconfig() -> None:
    """Same "default" -> "browseterm" renaming cluster_manager._fetch_and_merge_kubeconfig does
    (see its own docstring for why) - just reading k3s.yaml directly off this host's filesystem
    instead of catting it out of a VM, and with no server-IP rewrite needed (127.0.0.1 is already
    correct - this process *is* the node)."""
    with open(K3S_KUBECONFIG_PATH) as f:
        raw = f.read()

    rewritten = raw
    for pattern in (r"^(\s*name:\s*)default\s*$", r"^(\s*cluster:\s*)default\s*$",
                    r"^(\s*user:\s*)default\s*$", r"^(current-context:\s*)default\s*$"):
        rewritten = re.sub(pattern, rf"\g<1>{KUBE_CONTEXT}", rewritten, flags=re.MULTILINE)

    fetched_path = os.path.join(os.path.dirname(KUBE_CONFIG_PATH) or ".", ".browseterm-kubeconfig.tmp")
    os.makedirs(os.path.dirname(fetched_path) or ".", exist_ok=True)
    with open(fetched_path, "w") as f:
        f.write(rewritten)
    try:
        os.makedirs(os.path.dirname(KUBE_CONFIG_PATH) or ".", exist_ok=True)
        if not os.path.exists(KUBE_CONFIG_PATH):
            open(KUBE_CONFIG_PATH, "a").close()
        env_kubeconfig = f"{KUBE_CONFIG_PATH}:{fetched_path}"
        result = subprocess.run(
            ["kubectl", "config", "view", "--flatten"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "KUBECONFIG": env_kubeconfig},
        )
        if result.returncode != 0:
            raise ClusterError(result.stderr.strip() or "kubectl config view --flatten failed")
        with open(KUBE_CONFIG_PATH, "w") as f:
            f.write(result.stdout)
        _run(["kubectl", "config", "use-context", KUBE_CONTEXT], timeout=15)
    finally:
        if os.path.exists(fetched_path):
            os.remove(fetched_path)


def create_cluster(
    cpu_cores: int, memory_gb: float, storage_gb: float = 40.0,
    on_step: Optional[StepCallback] = None,
) -> None:
    """cpu_cores/memory_gb/storage_gb are accepted for interface parity with
    cluster_manager.create_cluster (local_stack.deploy's own callers don't care which backend they
    got), but are not used to size anything here - there is no VM to allocate; k3s runs directly on
    whatever resources this host already has. Real resource limiting on native Linux is a systemd
    slice / cgroup concern, not this module's job."""
    require_root()
    _run_step(on_step, "Installing k3s", _install_k3s)
    _run_step(on_step, "Installing gVisor sandbox runtime", _install_gvisor)
    _run_step(on_step, "Configuring kubectl access", _merge_kubeconfig)


def start_service() -> None:
    require_root()
    _run(["systemctl", "start", "k3s"])


def stop_service() -> None:
    require_root()
    _run(["systemctl", "stop", "k3s"])


def service_logs(lines: int = 200) -> str:
    return _run(["journalctl", "-u", "k3s", "-n", str(lines), "--no-pager"])


def pod_logs(namespace: str, pod: str, tail: int = 200) -> str:
    return _run(["kubectl", "--context", KUBE_CONTEXT, "-n", namespace, "logs", pod, f"--tail={tail}"])


def delete_cluster() -> None:
    require_root()
    if os.path.isfile(K3S_UNINSTALL_SCRIPT):
        _run([K3S_UNINSTALL_SCRIPT], timeout=_UNINSTALL_TIMEOUT_SECONDS)
    for args in (
        ["kubectl", "config", "delete-context", KUBE_CONTEXT],
        ["kubectl", "config", "delete-cluster", KUBE_CONTEXT],
        ["kubectl", "config", "delete-user", KUBE_CONTEXT],
    ):
        try:
            _run(args, timeout=10)
        except ClusterError:
            pass


__all__ = [
    "KUBE_CONTEXT", "cluster_exists", "create_cluster", "delete_cluster",
    "list_pods", "restart_pod", "is_monitored_pod",
    "start_service", "stop_service", "service_logs", "pod_logs",
]

"""
Report-only local runtime/k3s/Local-server health. Explicitly NOT lifecycle management
(no start/stop/bootstrap) - that belongs to a later task if the plan ever calls for it; p06.md
requires Desktop to report health only for now.
"""
import shutil
import subprocess

import httpx


def check_local_server_health(base_url: str, timeout: float = 3.0) -> bool:
    """Best-effort reachability check for the Local browseterm-server at `base_url`."""
    try:
        response = httpx.get(base_url, timeout=timeout)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def check_local_k3s_health(timeout: float = 5.0) -> bool:
    """Best-effort local k3s health via `kubectl cluster-info`, if kubectl is on PATH. Returns
    False (never raises) when kubectl is missing or the cluster is unreachable."""
    if shutil.which("kubectl") is None:
        return False
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info"], capture_output=True, timeout=timeout
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False

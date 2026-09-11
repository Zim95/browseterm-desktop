"""
Mac-only hardware detection for the Device page, via `sysctl`/`shutil`/`platform` -- no extra
dependency (`psutil` etc.) needed for the handful of totals the Device Cloud API wants.

Only physical totals live here. The user-configured `allocated_cpu`/`allocated_memory_bytes`/
`allocated_storage_bytes` (FINAL_BROWSETERM_V2_IMPLEMENTATION_PLAN.md section 9 -- "User
configures... Cloud validates allocation <= physical capacity") are a stateful preference, not a
hardware-detection concern, so they're read from `DesktopState` and assembled in `desktop/api.py`
instead.
"""
import platform
import shutil
import subprocess
from typing import Any

BYTES_PER_GB = 1024 ** 3


def default_allocation(hardware: dict[str, Any]) -> tuple[int, float, float]:
    """Half of detected capacity, leaving headroom for the host OS -- used both as the
    `allocated_*` values Cloud's `POST /devices` registration call requires up front (before the
    user has ever touched the Cluster section's sliders, e.g. on first login on a new machine) and
    as the Cluster section's own first-ever-read default (desktop/api.py)."""
    cpu = max(1, hardware["total_cpu"] // 2)
    memory_gb = max(1.0, round(hardware["total_memory_bytes"] / BYTES_PER_GB / 2))
    storage_gb = max(5.0, round(hardware["total_storage_bytes"] / BYTES_PER_GB / 2))
    return cpu, memory_gb, storage_gb


def _sysctl_int(name: str) -> int:
    output = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=True).stdout
    return int(output.strip())


def detect_hardware() -> dict[str, Any]:
    total_cpu = _sysctl_int("hw.ncpu")
    total_memory_bytes = _sysctl_int("hw.memsize")
    total_storage_bytes = shutil.disk_usage("/").total

    return {
        "device_name": platform.node(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "runtime_version": platform.mac_ver()[0] or platform.release(),
        "total_cpu": total_cpu,
        "total_memory_bytes": total_memory_bytes,
        "total_storage_bytes": total_storage_bytes,
        "gpu_info": None,
    }

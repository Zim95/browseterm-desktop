"""
Mac-only hardware detection for the Device page, via `sysctl`/`shutil`/`platform` -- no extra
dependency (`psutil` etc.) needed for the handful of totals the Device Cloud API wants.

Allocation defaults to 100% of detected totals: no resource-allocation UI exists yet (out of
scope for this app's current spec), so the only value that doesn't invent an arbitrary number is
offering the machine's full detected capacity.
"""
import platform
import shutil
import subprocess
from typing import Any


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
        "allocated_cpu": total_cpu,
        "allocated_memory_bytes": total_memory_bytes,
        "allocated_storage_bytes": total_storage_bytes,
        "gpu_info": None,
    }

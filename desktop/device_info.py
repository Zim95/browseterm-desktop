"""
Hardware detection for the Device page (macOS/Windows) and the headless Linux CLI (Part 18), via
`sysctl`/`/proc`/`GlobalMemoryStatusEx`/`shutil`/`platform` -- no extra dependency (`psutil` etc.)
needed for the handful of totals the Device Cloud API wants.

Only physical totals live here. The user-configured `allocated_cpu`/`allocated_memory_bytes`/
`allocated_storage_bytes` (FINAL_BROWSETERM_V2_IMPLEMENTATION_PLAN.md section 9 -- "User
configures... Cloud validates allocation <= physical capacity") are a stateful preference, not a
hardware-detection concern, so they're read from `DesktopState` and assembled in `desktop/api.py`
instead.
"""
import ctypes
import os
import platform
import shutil
import subprocess
import sys
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


def _linux_total_memory_bytes() -> int:
    """/proc/meminfo's MemTotal is in kB (despite the "kB" label actually meaning KiB, the
    long-standing kernel convention) - no sysctl/psutil dependency needed for this one value."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("MemTotal not found in /proc/meminfo")


def _detect_hardware_linux() -> dict[str, Any]:
    return {
        "device_name": platform.node(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "runtime_version": platform.release(),
        "total_cpu": os.cpu_count() or 1,
        "total_memory_bytes": _linux_total_memory_bytes(),
        "total_storage_bytes": shutil.disk_usage("/").total,
        "gpu_info": None,
    }


def _detect_hardware_macos() -> dict[str, Any]:
    return {
        "device_name": platform.node(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "runtime_version": platform.mac_ver()[0] or platform.release(),
        "total_cpu": _sysctl_int("hw.ncpu"),
        "total_memory_bytes": _sysctl_int("hw.memsize"),
        "total_storage_bytes": shutil.disk_usage("/").total,
        "gpu_info": None,
    }


class _MemoryStatusEx(ctypes.Structure):
    """Mirrors the Win32 MEMORYSTATUSEX struct (sysinfoapi.h) - the only portion GlobalMemoryStatusEx
    actually fills in that this needs is ullTotalPhys, but the struct must be declared and sized in
    full or the call corrupts adjacent memory."""
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_total_memory_bytes() -> int:
    """No sysctl/proc equivalent on Windows and no psutil dependency (see module docstring) - the
    stdlib-only way to get total physical memory is the Win32 GlobalMemoryStatusEx API via
    ctypes.windll (only present on Windows, which is why this isn't imported at module level)."""
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
    return status.ullTotalPhys


def _detect_hardware_windows() -> dict[str, Any]:
    system_drive = os.environ.get("SystemDrive", "C:") + "\\"
    return {
        "device_name": platform.node(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "runtime_version": platform.release(),
        "total_cpu": os.cpu_count() or 1,
        "total_memory_bytes": _windows_total_memory_bytes(),
        "total_storage_bytes": shutil.disk_usage(system_drive).total,
        "gpu_info": None,
    }


def detect_hardware() -> dict[str, Any]:
    if sys.platform == "linux":
        return _detect_hardware_linux()
    if sys.platform == "win32":
        return _detect_hardware_windows()
    return _detect_hardware_macos()

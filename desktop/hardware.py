"""
Detect actual host hardware values (never hardcoded), using only stable OS APIs/stdlib -
no brittle GUI-output parsing (`system_profiler` text scraping, etc).
"""
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HostResources:
    """Field names deliberately mirror the P01 Device schema's total_* columns
    (browseterm-db's `Device` model) so callers can pass this straight into a register/update
    payload without renaming."""

    os_name: str
    os_version: str
    architecture: str
    total_cpu: int
    total_memory_bytes: int
    total_storage_bytes: int


def detect_architecture() -> str:
    """e.g. 'arm64' or 'x86_64'. platform.machine() - stdlib, stable across macOS versions."""
    return platform.machine()


def detect_macos_version() -> str:
    """e.g. '14.5'. platform.mac_ver() - stdlib, backed by the Gestalt/sysctl APIs, not GUI text."""
    version, _, _ = platform.mac_ver()
    if not version:
        raise RuntimeError("Unable to detect macOS version (platform.mac_ver() returned empty)")
    return version


def detect_cpu_count() -> int:
    """Logical CPU count. os.cpu_count() - stdlib wrapper over sysconf(_SC_NPROCESSORS_ONLN)."""
    count = os.cpu_count()
    if not count:
        raise RuntimeError("Unable to detect CPU count")
    return count


def detect_total_memory_bytes() -> int:
    """Physical RAM in bytes, via `sysctl -n hw.memsize` - the standard, documented macOS sysctl
    OID for this value (a single integer on stdout, not parsed GUI output)."""
    result = subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5, check=True
    )
    return int(result.stdout.strip())


def detect_total_storage_bytes(path: str = "/") -> int:
    """Total capacity of the volume at `path`, via shutil.disk_usage() - stdlib, wraps statvfs."""
    return shutil.disk_usage(path).total


def detect_host_resources() -> HostResources:
    return HostResources(
        os_name="macOS",
        os_version=detect_macos_version(),
        architecture=detect_architecture(),
        total_cpu=detect_cpu_count(),
        total_memory_bytes=detect_total_memory_bytes(),
        total_storage_bytes=detect_total_storage_bytes(),
    )

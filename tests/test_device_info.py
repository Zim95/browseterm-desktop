"""
Linux hardware detection (Part 18) - the macOS sysctl path is unchanged and already exercised
indirectly by every test that monkeypatches detect_hardware() wholesale (test_desktop.py,
test_api_cluster.py); this covers device_info.py's own Linux-specific parsing logic directly.
"""
import builtins

from desktop import device_info


_MEMINFO_SAMPLE = (
    "MemTotal:       16384000 kB\n"
    "MemFree:         2048000 kB\n"
    "MemAvailable:    4096000 kB\n"
)


def test_linux_total_memory_bytes_parses_meminfo(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(_MEMINFO_SAMPLE)
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/meminfo":
            return real_open(meminfo, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert device_info._linux_total_memory_bytes() == 16384000 * 1024


def test_detect_hardware_linux_uses_proc_and_cpu_count(monkeypatch):
    monkeypatch.setattr(device_info.sys, "platform", "linux")
    monkeypatch.setattr(device_info.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(device_info, "_linux_total_memory_bytes", lambda: 16 * device_info.BYTES_PER_GB)
    monkeypatch.setattr(device_info.shutil, "disk_usage", lambda _path: type("D", (), {"total": 100 * device_info.BYTES_PER_GB})())

    hardware = device_info.detect_hardware()

    assert hardware["total_cpu"] == 8
    assert hardware["total_memory_bytes"] == 16 * device_info.BYTES_PER_GB
    assert hardware["total_storage_bytes"] == 100 * device_info.BYTES_PER_GB
    assert hardware["gpu_info"] is None


def test_detect_hardware_dispatches_to_macos_when_not_linux(monkeypatch):
    monkeypatch.setattr(device_info.sys, "platform", "darwin")
    called = {"macos": False, "linux": False}
    monkeypatch.setattr(device_info, "_detect_hardware_macos", lambda: called.__setitem__("macos", True) or {})
    monkeypatch.setattr(device_info, "_detect_hardware_linux", lambda: called.__setitem__("linux", True) or {})

    device_info.detect_hardware()

    assert called == {"macos": True, "linux": False}

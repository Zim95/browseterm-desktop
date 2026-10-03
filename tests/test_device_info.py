"""
Linux/Windows hardware detection (Part 18, and the Windows-login-hang fix) - the macOS sysctl path
is unchanged and already exercised indirectly by every test that monkeypatches detect_hardware()
wholesale (test_desktop.py, test_api_cluster.py); this covers device_info.py's own Linux- and
Windows-specific parsing logic directly.
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


def test_detect_hardware_dispatches_to_macos_when_darwin(monkeypatch):
    monkeypatch.setattr(device_info.sys, "platform", "darwin")
    called = {"macos": False, "linux": False, "windows": False}
    monkeypatch.setattr(device_info, "_detect_hardware_macos", lambda: called.__setitem__("macos", True) or {})
    monkeypatch.setattr(device_info, "_detect_hardware_linux", lambda: called.__setitem__("linux", True) or {})
    monkeypatch.setattr(device_info, "_detect_hardware_windows", lambda: called.__setitem__("windows", True) or {})

    device_info.detect_hardware()

    assert called == {"macos": True, "linux": False, "windows": False}


def test_detect_hardware_dispatches_to_windows_when_win32(monkeypatch):
    """The actual bug this guards against: detect_hardware() used to treat any non-Linux platform
    as macOS, so on win32 it called sysctl (a command that doesn't exist on Windows) and raised
    FileNotFoundError - uncaught in the login flow's background thread, which silently killed the
    login poll right after showing the device code, leaving the user stuck on that screen forever
    with no error surfaced (a --windowed PyInstaller build has no visible console/stderr)."""
    monkeypatch.setattr(device_info.sys, "platform", "win32")
    called = {"macos": False, "linux": False, "windows": False}
    monkeypatch.setattr(device_info, "_detect_hardware_macos", lambda: called.__setitem__("macos", True) or {})
    monkeypatch.setattr(device_info, "_detect_hardware_linux", lambda: called.__setitem__("linux", True) or {})
    monkeypatch.setattr(device_info, "_detect_hardware_windows", lambda: called.__setitem__("windows", True) or {})

    device_info.detect_hardware()

    assert called == {"macos": False, "linux": False, "windows": True}


def test_detect_hardware_windows_uses_global_memory_status_and_cpu_count(monkeypatch):
    monkeypatch.setattr(device_info.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(device_info, "_windows_total_memory_bytes", lambda: 32 * device_info.BYTES_PER_GB)
    monkeypatch.setattr(device_info.shutil, "disk_usage", lambda _path: type("D", (), {"total": 500 * device_info.BYTES_PER_GB})())
    monkeypatch.setattr(device_info.os, "environ", {"SystemDrive": "C:"})

    hardware = device_info._detect_hardware_windows()

    assert hardware["total_cpu"] == 12
    assert hardware["total_memory_bytes"] == 32 * device_info.BYTES_PER_GB
    assert hardware["total_storage_bytes"] == 500 * device_info.BYTES_PER_GB
    assert hardware["gpu_info"] is None

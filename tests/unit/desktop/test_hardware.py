import unittest
from unittest.mock import patch, MagicMock

from desktop.hardware import (
    detect_architecture,
    detect_cpu_count,
    detect_host_resources,
    detect_macos_version,
    detect_total_memory_bytes,
    detect_total_storage_bytes,
)


class TestHardwareDetection(unittest.TestCase):
    """Every detection function crosses an OS boundary - mocked here per p06.md's instruction
    ("Tests should mock these boundaries")."""

    @patch("desktop.hardware.platform.machine", return_value="arm64")
    def test_detect_architecture(self, _):
        self.assertEqual(detect_architecture(), "arm64")

    @patch("desktop.hardware.platform.mac_ver", return_value=("14.5", ("", "", ""), "arm64"))
    def test_detect_macos_version(self, _):
        self.assertEqual(detect_macos_version(), "14.5")

    @patch("desktop.hardware.platform.mac_ver", return_value=("", ("", "", ""), ""))
    def test_detect_macos_version_empty_raises(self, _):
        with self.assertRaises(RuntimeError):
            detect_macos_version()

    @patch("desktop.hardware.os.cpu_count", return_value=12)
    def test_detect_cpu_count(self, _):
        self.assertEqual(detect_cpu_count(), 12)

    @patch("desktop.hardware.os.cpu_count", return_value=None)
    def test_detect_cpu_count_none_raises(self, _):
        with self.assertRaises(RuntimeError):
            detect_cpu_count()

    @patch("desktop.hardware.subprocess.run")
    def test_detect_total_memory_bytes(self, mock_run):
        mock_run.return_value = MagicMock(stdout="34359738368\n")
        self.assertEqual(detect_total_memory_bytes(), 34359738368)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["sysctl", "-n", "hw.memsize"])

    @patch("desktop.hardware.shutil.disk_usage")
    def test_detect_total_storage_bytes(self, mock_disk_usage):
        mock_disk_usage.return_value = MagicMock(total=500_000_000_000)
        self.assertEqual(detect_total_storage_bytes(), 500_000_000_000)

    @patch("desktop.hardware.detect_total_storage_bytes", return_value=500_000_000_000)
    @patch("desktop.hardware.detect_total_memory_bytes", return_value=34359738368)
    @patch("desktop.hardware.detect_cpu_count", return_value=12)
    @patch("desktop.hardware.detect_macos_version", return_value="14.5")
    @patch("desktop.hardware.detect_architecture", return_value="arm64")
    def test_detect_host_resources_aggregates_all(self, *_):
        host = detect_host_resources()
        self.assertEqual(host.os_name, "macOS")
        self.assertEqual(host.os_version, "14.5")
        self.assertEqual(host.architecture, "arm64")
        self.assertEqual(host.total_cpu, 12)
        self.assertEqual(host.total_memory_bytes, 34359738368)
        self.assertEqual(host.total_storage_bytes, 500_000_000_000)


if __name__ == "__main__":
    unittest.main()

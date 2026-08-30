import subprocess
import unittest
from unittest.mock import MagicMock, patch

import httpx

from desktop.runtime_health import check_local_k3s_health, check_local_server_health


class TestCheckLocalServerHealth(unittest.TestCase):
    @patch("desktop.runtime_health.httpx.get")
    def test_reachable_2xx_is_healthy(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        self.assertTrue(check_local_server_health("http://browseterm.local.com:9999"))

    @patch("desktop.runtime_health.httpx.get")
    def test_server_error_is_unhealthy(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500)
        self.assertFalse(check_local_server_health("http://browseterm.local.com:9999"))

    @patch("desktop.runtime_health.httpx.get", side_effect=httpx.ConnectError("refused"))
    def test_unreachable_is_unhealthy_not_raised(self, _):
        self.assertFalse(check_local_server_health("http://browseterm.local.com:9999"))


class TestCheckLocalK3sHealth(unittest.TestCase):
    @patch("desktop.runtime_health.shutil.which", return_value=None)
    def test_no_kubectl_is_unhealthy(self, _):
        self.assertFalse(check_local_k3s_health())

    @patch("desktop.runtime_health.subprocess.run")
    @patch("desktop.runtime_health.shutil.which", return_value="/usr/local/bin/kubectl")
    def test_kubectl_success_is_healthy(self, _, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(check_local_k3s_health())

    @patch("desktop.runtime_health.subprocess.run")
    @patch("desktop.runtime_health.shutil.which", return_value="/usr/local/bin/kubectl")
    def test_kubectl_nonzero_exit_is_unhealthy(self, _, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(check_local_k3s_health())

    @patch("desktop.runtime_health.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=5))
    @patch("desktop.runtime_health.shutil.which", return_value="/usr/local/bin/kubectl")
    def test_kubectl_timeout_is_unhealthy_not_raised(self, _, __):
        self.assertFalse(check_local_k3s_health())


if __name__ == "__main__":
    unittest.main()

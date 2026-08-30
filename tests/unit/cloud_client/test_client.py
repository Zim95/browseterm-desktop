import unittest
from unittest.mock import patch, MagicMock

from src.cloud_client.client import CloudClient, CloudClientError


def _mock_response(status_code: int, json_body: dict):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    response.text = str(json_body)
    return response


class TestCloudClientBoundary(unittest.TestCase):
    """The CloudClient must be the only place cookie/HTTP wiring happens, and must never touch
    browseterm_db/DB_CONFIG/POSTGRES_*/REDIS_* - verified statically (no such import exists in
    src/cloud_client/*) and behaviorally here (every request goes through httpx.request)."""

    @patch("src.cloud_client.client.httpx.request")
    def test_register_device_sends_session_cookie(self, mock_request):
        mock_request.return_value = _mock_response(201, {"device": {"id": "d1"}})
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        result = client.register_device({"device_name": "mac-1"})

        self.assertEqual(result, {"id": "d1"})
        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "http://cloud.test/devices")
        self.assertEqual(kwargs["cookies"], {"session": "abc123"})
        self.assertEqual(kwargs["json"], {"device_name": "mac-1"})

    @patch("src.cloud_client.client.httpx.request")
    def test_no_cookie_sends_no_cookie_header(self, mock_request):
        mock_request.return_value = _mock_response(200, {"devices": []})
        client = CloudClient(base_url="http://cloud.test")

        client.list_devices()

        self.assertEqual(mock_request.call_args.kwargs["cookies"], {})

    @patch("src.cloud_client.client.httpx.request")
    def test_duplicate_device_raises_409_not_swallowed(self, mock_request):
        mock_request.return_value = _mock_response(
            409, {"error": "User not found or device name already registered for this user"}
        )
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        with self.assertRaises(CloudClientError) as ctx:
            client.register_device({"device_name": "mac-1"})
        self.assertEqual(ctx.exception.status_code, 409)

    @patch("src.cloud_client.client.httpx.request")
    def test_get_device_404_raises(self, mock_request):
        mock_request.return_value = _mock_response(404, {"error": "Device not found"})
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        with self.assertRaises(CloudClientError) as ctx:
            client.get_device("nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("src.cloud_client.client.httpx.request")
    def test_update_device_posts_only_given_fields(self, mock_request):
        mock_request.return_value = _mock_response(200, {"device": {"id": "d1", "allocated_cpu": 4}})
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        client.update_device("d1", {"allocated_cpu": 4})

        self.assertEqual(mock_request.call_args.kwargs["json"], {"allocated_cpu": 4})
        self.assertEqual(mock_request.call_args.args[1], "http://cloud.test/devices/d1")

    @patch("src.cloud_client.client.httpx.request")
    def test_heartbeat_hits_heartbeat_path(self, mock_request):
        mock_request.return_value = _mock_response(200, {"device": {"id": "d1"}})
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        client.heartbeat("d1")

        self.assertEqual(mock_request.call_args.args[1], "http://cloud.test/devices/d1/heartbeat")

    @patch("src.cloud_client.client.httpx.request")
    def test_transport_failure_raises_cloud_client_error_status_zero(self, mock_request):
        import httpx

        mock_request.side_effect = httpx.ConnectError("connection refused")
        client = CloudClient(base_url="http://cloud.test", session_cookie="abc123")

        with self.assertRaises(CloudClientError) as ctx:
            client.list_devices()
        self.assertEqual(ctx.exception.status_code, 0)


if __name__ == "__main__":
    unittest.main()

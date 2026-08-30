import unittest
from unittest.mock import MagicMock

from desktop.allocation import Allocation, AllocationValidationError
from desktop.device_registration import (
    build_registration_payload,
    register_or_update_device,
    send_heartbeat,
)
from desktop.hardware import HostResources
from src.cloud_client.client import CloudClientError


def _host() -> HostResources:
    return HostResources(
        os_name="macOS",
        os_version="14.5",
        architecture="arm64",
        total_cpu=12,
        total_memory_bytes=32 * 1024**3,
        total_storage_bytes=500 * 1024**3,
    )


def _allocation() -> Allocation:
    return Allocation(allocated_cpu=6, allocated_memory_bytes=12 * 1024**3, allocated_storage_bytes=80 * 1024**3)


class TestBuildRegistrationPayload(unittest.TestCase):
    def test_payload_matches_host_and_allocation(self):
        payload = build_registration_payload(_host(), _allocation(), device_name="mac-1")
        self.assertEqual(payload["device_name"], "mac-1")
        self.assertEqual(payload["os"], "macOS")
        self.assertEqual(payload["architecture"], "arm64")
        self.assertEqual(payload["total_cpu"], 12)
        self.assertEqual(payload["allocated_cpu"], 6)

    def test_invalid_allocation_rejected_before_any_network_call(self):
        bad_allocation = Allocation(allocated_cpu=999, allocated_memory_bytes=0, allocated_storage_bytes=0)
        with self.assertRaises(AllocationValidationError):
            build_registration_payload(_host(), bad_allocation, device_name="mac-1")


class TestRegisterOrUpdateDevice(unittest.TestCase):
    def test_fresh_registration_calls_register_only(self):
        client = MagicMock()
        client.register_device.return_value = {"id": "d1"}

        result = register_or_update_device(client, _host(), _allocation(), device_name="mac-1")

        self.assertEqual(result, {"id": "d1"})
        client.register_device.assert_called_once()
        client.update_device.assert_not_called()

    def test_duplicate_409_falls_back_to_find_and_update(self):
        client = MagicMock()
        client.register_device.side_effect = CloudClientError(409, "duplicate")
        client.list_devices.return_value = [{"id": "existing-id", "device_name": "mac-1"}]
        client.update_device.return_value = {"id": "existing-id", "allocated_cpu": 6}

        result = register_or_update_device(client, _host(), _allocation(), device_name="mac-1")

        client.update_device.assert_called_once()
        called_id, called_fields = client.update_device.call_args.args
        self.assertEqual(called_id, "existing-id")
        self.assertNotIn("device_name", called_fields)
        self.assertEqual(result, {"id": "existing-id", "allocated_cpu": 6})

    def test_duplicate_409_but_device_not_found_reraises(self):
        client = MagicMock()
        client.register_device.side_effect = CloudClientError(409, "duplicate")
        client.list_devices.return_value = []

        with self.assertRaises(CloudClientError):
            register_or_update_device(client, _host(), _allocation(), device_name="mac-1")
        client.update_device.assert_not_called()

    def test_non_409_error_propagates_without_fallback(self):
        client = MagicMock()
        client.register_device.side_effect = CloudClientError(500, "server error")

        with self.assertRaises(CloudClientError):
            register_or_update_device(client, _host(), _allocation(), device_name="mac-1")
        client.list_devices.assert_not_called()
        client.update_device.assert_not_called()


class TestHeartbeat(unittest.TestCase):
    def test_send_heartbeat_delegates_to_client(self):
        client = MagicMock()
        client.heartbeat.return_value = {"id": "d1", "status": "active"}

        result = send_heartbeat(client, "d1")

        client.heartbeat.assert_called_once_with("d1")
        self.assertEqual(result, {"id": "d1", "status": "active"})


if __name__ == "__main__":
    unittest.main()

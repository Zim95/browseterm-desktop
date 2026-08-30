"""
Orchestrates hardware detection + allocation -> Cloud Device API (P05), through CloudClient
only. See p06.md's "DEVICE REGISTRATION FLOW": P05 registration is NOT idempotent (409 on a
duplicate (user_id, device_name)), so a re-run finds and updates the existing device instead of
pretending POST /devices is upsert-safe.
"""
from typing import Optional

from desktop.allocation import Allocation, validate_allocation
from desktop.config import DEFAULT_DEVICE_NAME, DESKTOP_APP_VERSION
from desktop.hardware import HostResources
from src.cloud_client.client import CloudClient, CloudClientError


def build_registration_payload(
    host: HostResources, allocation: Allocation, device_name: str = DEFAULT_DEVICE_NAME
) -> dict:
    validate_allocation(allocation, host)
    return {
        "device_name": device_name,
        "os": host.os_name,
        "architecture": host.architecture,
        "runtime_version": DESKTOP_APP_VERSION,
        "total_cpu": host.total_cpu,
        "total_memory_bytes": host.total_memory_bytes,
        "total_storage_bytes": host.total_storage_bytes,
        "allocated_cpu": allocation.allocated_cpu,
        "allocated_memory_bytes": allocation.allocated_memory_bytes,
        "allocated_storage_bytes": allocation.allocated_storage_bytes,
    }


def _find_device_by_name(client: CloudClient, device_name: str) -> Optional[dict]:
    for device in client.list_devices():
        if device.get("device_name") == device_name:
            return device
    return None


def register_or_update_device(
    client: CloudClient,
    host: HostResources,
    allocation: Allocation,
    device_name: str = DEFAULT_DEVICE_NAME,
) -> dict:
    """Register this device, or update it if POST /devices reports it already exists (409).
    Any other CloudClientError propagates unchanged - duplicate semantics are not disguised."""
    payload = build_registration_payload(host, allocation, device_name)
    try:
        return client.register_device(payload)
    except CloudClientError as e:
        if e.status_code != 409:
            raise
        existing = _find_device_by_name(client, device_name)
        if existing is None:
            raise
        update_fields = {k: v for k, v in payload.items() if k != "device_name"}
        return client.update_device(existing["id"], update_fields)


def send_heartbeat(client: CloudClient, device_id: str) -> dict:
    return client.heartbeat(device_id)

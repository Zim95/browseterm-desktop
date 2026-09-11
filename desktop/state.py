"""
Persisted desktop state: the last-activated device id/name (not secret - an identifier, not a
credential), plus the user's chosen Browseterm resource allocation for the local cluster (Cluster
section of the Device page). The device Bearer credential itself lives only in macOS Keychain
(desktop/keychain.py) - see p07.md section 22, never a plaintext file. This file existing with a
device_id is not what "logged in" means any more (that's "Keychain has a valid device token");
it just lets the Device page show something immediately on startup before that validates.

Allocation fields survive `clear()`/logout deliberately: they're a per-machine preference for how
much of this Mac to give Browseterm, independent of which account is currently logged in, and
whether the local k3d cluster itself is up (that's checked live via `cluster_manager.cluster_exists()`,
never cached here, so a cluster deleted outside the app is never misreported as still running).
"""
import json
import os
import stat
from dataclasses import asdict, dataclass
from typing import Any, Optional

from desktop.config import STATE_DIR, STATE_FILE
from desktop.device_info import default_allocation


@dataclass
class DesktopState:
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    allocated_cpu: Optional[int] = None
    allocated_memory_gb: Optional[float] = None
    allocated_storage_gb: Optional[float] = None

    def ensure_allocation_defaults(self, hardware: dict[str, Any]) -> None:
        '''Defaults `allocated_*` to half of `hardware`'s detected totals (see
        `device_info.default_allocation`) the first time this device's allocation is ever needed
        -- both Cloud's device-registration payload (desktop/app.py) and the Cluster section's
        first-ever read (desktop/api.py) call this, so both always land on the same value.
        A no-op, no write, once a value has been chosen (by either path, or by the user).'''
        if self.allocated_cpu is not None:
            return
        self.allocated_cpu, self.allocated_memory_gb, self.allocated_storage_gb = default_allocation(hardware)
        self.save()

    def save(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f)
        os.chmod(STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        self.device_id = None
        self.device_name = None
        self.save()


def load_state() -> DesktopState:
    if not os.path.exists(STATE_FILE):
        return DesktopState()
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return DesktopState()
    return DesktopState(
        device_id=data.get("device_id"),
        device_name=data.get("device_name"),
        allocated_cpu=data.get("allocated_cpu"),
        allocated_memory_gb=data.get("allocated_memory_gb"),
        allocated_storage_gb=data.get("allocated_storage_gb"),
    )

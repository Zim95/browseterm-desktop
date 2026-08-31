"""
Persisted desktop state: just the last-activated device id/name (not secret - an identifier, not
a credential). The device Bearer credential itself lives only in macOS Keychain
(desktop/keychain.py) - see p07.md section 22, never a plaintext file. This file existing with a
device_id is not what "logged in" means any more (that's "Keychain has a valid device token");
it just lets the Device page show something immediately on startup before that validates.
"""
import json
import os
import stat
from dataclasses import asdict, dataclass
from typing import Optional

from desktop.config import STATE_DIR, STATE_FILE


@dataclass
class DesktopState:
    device_id: Optional[str] = None
    device_name: Optional[str] = None

    def save(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f)
        os.chmod(STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        self.device_id = None
        self.device_name = None
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)


def load_state() -> DesktopState:
    if not os.path.exists(STATE_FILE):
        return DesktopState()
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return DesktopState()
    return DesktopState(device_id=data.get("device_id"), device_name=data.get("device_name"))

"""
Headless-Linux device credential storage (Part 18/4: "Linux: root-owned 0600 file, preferably
encrypted/secret-service when available; document the fallback").

`keyring` (desktop/keychain.py's own backend) targets the Secret Service D-Bus API on Linux
(GNOME Keyring/KWallet) - that requires a running desktop session and D-Bus daemon, neither of
which exist on a headless server the CLI (Part 18) targets. There is no reliable
secret-service-when-available fallback to attempt here without one, so this documents and
implements only the plain, root-owned 0600 file the doc explicitly sanctions as the fallback -
the device token never has any other on-disk representation.

Same interface as KeychainStorage (desktop/keychain.py) so callers (the CLI) can select between
them purely by platform, with no other code branching needed.
"""
import os
import stat
from typing import Optional

from desktop.config import STATE_DIR

_TOKEN_FILE = os.path.join(STATE_DIR, "device_token")


class LinuxFileCredentialStore:
    def get_device_token(self) -> Optional[str]:
        try:
            with open(_TOKEN_FILE) as f:
                token = f.read().strip()
        except FileNotFoundError:
            return None
        return token or None

    def set_device_token(self, token: str) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_TOKEN_FILE, "w") as f:
            f.write(token)
        os.chmod(_TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)

    def delete_device_token(self) -> None:
        try:
            os.remove(_TOKEN_FILE)
        except FileNotFoundError:
            pass  # already absent - logout must be idempotent, same convention as KeychainStorage

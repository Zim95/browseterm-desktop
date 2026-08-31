'''
P07 - macOS Keychain storage for the device Bearer credential (p07.md section 22: "Store Desktop
device credential in macOS Keychain... use an abstraction so unit tests can mock storage").

`keyring` targets the real macOS Keychain via its Cocoa/Security-framework backend on darwin with
no extra configuration. Never store the device token anywhere else (env var, JSON, YAML, source,
LocalStorage, a Kubernetes manifest, or a log line) - see p07.md section 22/40.
'''
from typing import Optional

import keyring

_SERVICE_NAME = "com.browseterm.desktop"
_ACCOUNT_NAME = "device_token"


class KeychainStorage:
    def get_device_token(self) -> Optional[str]:
        return keyring.get_password(_SERVICE_NAME, _ACCOUNT_NAME)

    def set_device_token(self, token: str) -> None:
        keyring.set_password(_SERVICE_NAME, _ACCOUNT_NAME, token)

    def delete_device_token(self) -> None:
        try:
            keyring.delete_password(_SERVICE_NAME, _ACCOUNT_NAME)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent - logout must be idempotent

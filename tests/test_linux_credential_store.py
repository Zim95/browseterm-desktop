"""Root-owned 0600 device-token file storage for headless Linux (Part 18/4)."""
import stat

from desktop.linux_credential_store import LinuxFileCredentialStore


def test_get_device_token_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("desktop.linux_credential_store.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.linux_credential_store._TOKEN_FILE", str(tmp_path / "device_token"))
    assert LinuxFileCredentialStore().get_device_token() is None


def test_set_then_get_round_trips_and_chmods_0600(monkeypatch, tmp_path):
    token_file = tmp_path / "device_token"
    monkeypatch.setattr("desktop.linux_credential_store.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.linux_credential_store._TOKEN_FILE", str(token_file))

    store = LinuxFileCredentialStore()
    store.set_device_token("bst_device_abc123")

    assert store.get_device_token() == "bst_device_abc123"
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_delete_is_idempotent(monkeypatch, tmp_path):
    token_file = tmp_path / "device_token"
    monkeypatch.setattr("desktop.linux_credential_store.STATE_DIR", str(tmp_path))
    monkeypatch.setattr("desktop.linux_credential_store._TOKEN_FILE", str(token_file))

    store = LinuxFileCredentialStore()
    store.delete_device_token()  # must not raise when nothing exists yet
    store.set_device_token("tok")
    store.delete_device_token()
    store.delete_device_token()  # second delete must also not raise
    assert store.get_device_token() is None

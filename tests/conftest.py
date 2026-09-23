"""
Shared fixtures for this test suite - autouse fixtures here apply to every test automatically,
with no per-file import needed.
"""
import pytest

from desktop import state as state_module


@pytest.fixture(autouse=True)
def _isolate_desktop_state(tmp_path, monkeypatch):
    """`DesktopState.save()`/`load_state()` write to `STATE_FILE`
    (`~/.browseterm/desktop_state.json` by default) - dozens of tests across this suite construct
    a bare `DesktopState()` with no isolation of their own, and at least one
    (`test_full_desktop_login_flow_round_trip` in test_desktop.py) calls `.save()` for real, via
    the login flow it's exercising. Without this fixture, running the test suite silently
    overwrites the real user's own local application state with test fixture values - caught for
    real: a live `~/.browseterm/desktop_state.json` ended up clobbered with "device-9"/"test-mac"
    after a routine test run. Autouse (not opt-in per test) so no future test can reintroduce the
    same gap by forgetting to ask for it - `desktop.state` imports `STATE_DIR`/`STATE_FILE` by
    value from `desktop.config`, so the patch target has to be the names bound in `state`'s own
    module namespace, not `desktop.config`'s.
    """
    monkeypatch.setattr(state_module, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(state_module, "STATE_FILE", str(tmp_path / "desktop_state.json"))

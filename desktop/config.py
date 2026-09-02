"""
Configuration for the Desktop app.

Mirrors the same env-var-overridable, DNS-convention-based defaults used by
`browseterm-server-local`'s `src/cloud_client/config.py`: `browseterm.cloud.com` for Cloud,
`browseterm.local.com` for Local. Override both for local development against instances running
on this machine (e.g. `http://localhost:9999` / `http://localhost:8000`).
"""
import os

# Local control plane -- serves the real browser UI, including the actual login flow this app's
# WebView shares (P07: no separate Desktop OAuth implementation, see p07.md section 19).
BROWSETERM_LOCAL_URL: str = os.getenv("BROWSETERM_LOCAL_URL", "http://browseterm.local.com").rstrip("/")

# Cloud control plane -- owns the Device API and issues the device Bearer credential this app
# authenticates with. Matches `browseterm-server-local`'s own default.
BROWSETERM_CLOUD_API_URL: str = os.getenv("BROWSETERM_CLOUD_API_URL", "http://browseterm.cloud.com:9999").rstrip("/")

# The browser/WebView session cookie name (P07: used only transiently, to make the one
# `/device/bootstrap` call right after login -- never persisted, never used as the ongoing
# device credential; see desktop/app.py and p07.md section 20).
SESSION_COOKIE_NAME: str = "session"
CSRF_COOKIE_NAME: str = "csrf_token"

# How often the background thread heartbeats this device (using its own long-lived device
# credential, independent of the browser session -- see desktop/app.py's module docstring).
DEVICE_HEARTBEAT_INTERVAL_SECONDS: int = 25 * 60

# How long to wait for the system-browser login (OAuth + Local login + device bootstrap) to
# complete and redirect back to the local loopback server before giving up (desktop/app.py,
# desktop/loopback_server.py). Generous -- this covers actual human time spent on a Google/GitHub
# consent screen, not just network latency.
DESKTOP_LOGIN_TIMEOUT_SECONDS: float = 5 * 60

# Where the last-activated device id (not secret - just an identifier) is persisted across app
# restarts, so the Device page can show it immediately. The device credential itself lives only
# in macOS Keychain (desktop/keychain.py), never here.
STATE_DIR: str = os.path.expanduser("~/.browseterm")
STATE_FILE: str = os.path.join(STATE_DIR, "desktop_state.json")

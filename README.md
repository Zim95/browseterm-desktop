# browseterm-desktop

Mac-only desktop app: login (shares the real `browseterm-server-local` login page and OAuth flow
- Google/GitHub OAuth is entirely Cloud's job now, see P07 below), a Device page (hardware
detection + activation against Cloud's Device API), and a background device heartbeat. Built with
`pywebview` -- see `desktop/app.py`'s module docstring for why (the login step needs a real
browser engine for OAuth either way, and reusing the actual login page keeps it pixel-identical
to the web UI instead of a hand-maintained copy).

## Run it

```
poetry install
poetry run python main.py
```

By default this points at `http://browseterm.local.com` (Local) and
`http://browseterm.cloud.com:9999` (Cloud) -- override with the `BROWSETERM_LOCAL_URL` /
`BROWSETERM_CLOUD_API_URL` env vars for local development, matching the same convention
`browseterm-server-local`'s `CloudClient` uses.

## P07 - device credential, not the browser session

Before P07, this app used the browser/WebView's own session cookie as its ongoing Device API
credential (`BROWSETERM_SESSION_COOKIE`, interim/P06). That's gone. Per
`~/browseterm/p07.md`, this app now:

1. Loads Local's real `/login` page in the WebView -- exactly the same OAuth flow a browser tab
   gets (Cloud is the sole OAuth authority; this app contains no Google/GitHub client id/secret
   and never will).
2. Once that flow lands back on Local's home page, reads the `session` + `csrf_token` cookies out
   of the webview's native cookie store (`window.get_cookies()` -- works for HttpOnly cookies,
   unlike `document.cookie`) and uses them **exactly once**, immediately
   (`DesktopApp._bootstrap_device`), to call Local's session-cookie-protected
   `POST /device/bootstrap`, which hands back a one-time bootstrap code.
3. Redeems that code against Cloud's public `POST /auth/device-bootstrap/redeem`
   (`desktop/cloud_client.py:redeem_device_bootstrap`), along with this machine's detected
   hardware -- Cloud registers (or re-activates) the device and returns a long-lived, per-device
   Bearer token.
4. Stores that token in the **macOS Keychain** (`desktop/keychain.py`, never a file, env var, or
   log line) and uses `Authorization: Bearer <device_token>` for every subsequent Device API call.
   The browser session cookie is never held onto past that one bootstrap call.

Consequence: "am I logged in" is now "does Keychain hold a valid device token" - independent of
whether the browser/WebView session is still valid. A valid Keychain token on startup skips the
WebView login entirely and goes straight to the Device page.

## How it works

- **Login**: the window loads Local's real `/login` page directly (not a reimplementation), so
  Google/GitHub OAuth behaves exactly like the web app.
- **Device bootstrap**: see above -- happens automatically right after login, not behind a
  button click (the WebView session cookie it needs is only available at that moment).
- **Device page**: `desktop/device_info.py` detects this Mac's hardware (`sysctl`/`shutil`).
  "Activate" (`desktop/api.py:Api.activate_device`) heartbeats the device using the existing
  Keychain token (marks it ACTIVE, demotes any other of this user's active devices) - it does
  **not** re-run bootstrap, since that needs a live WebView session this shell no longer has once
  swapped away from it. If the token is genuinely missing (e.g. after logout), the answer is "log
  out and log back in", not a hidden second bootstrap path here (p07.md: "do not unnecessarily
  expand P07 into device-management UI").
- **Heartbeat**: a background thread heartbeats the device (its own long-lived credential,
  independent of the browser session) every 25 minutes (`desktop/config.py`,
  `DEVICE_HEARTBEAT_INTERVAL_SECONDS`) so its `status`/`last_seen_at` stay fresh.
- **State**: `~/.browseterm/desktop_state.json` (0600) persists only the last-activated device
  id/name (not secret - an identifier, not a credential) across restarts, so the Device page can
  show something immediately. The device credential itself lives only in Keychain.
- **Logout**: clears the Keychain token + local state and returns to the login page. Does *not*
  call Local's `/logout` -- by the time app.html is showing, this app no longer holds Local's
  session cookie (used only transiently for bootstrap), so there's nothing to send that endpoint
  with. Any lingering browser session is left to expire on its own TTL rather than being
  explicitly revoked - a documented trade-off, not an oversight.

## Tests

```
poetry install
poetry run pytest tests/ -v
```

`tests/test_desktop.py` spins up a tiny in-process HTTP stub standing in for Local's
`/device/bootstrap` and Cloud's `/auth/device-bootstrap/redeem` + Bearer-gated Device API, and
covers: bootstrap end-to-end, missing-cookie/wrong-CSRF rejection, second-redemption-of-a-code
failing, device-token-scoped calls (wrong token rejected), the Keychain storage abstraction being
swappable (a `_FakeKeychain` stands in, matching p07.md section 22's "use an abstraction so unit
tests can mock storage"), and `Api.activate_device()`'s friendly error when no credential exists
yet.

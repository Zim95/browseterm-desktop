# browseterm-desktop

Mac-only desktop app: login (an OAuth Device Authorization Grant against Cloud directly - see
"Login" below; Google/GitHub OAuth is entirely Cloud's job, this app never holds a provider
secret), a Cluster section (stands up/tears down a real **Multipass VM running k3s** + the current
local-stack workloads - `desktop/cluster_manager.py`/`desktop/local_stack.py`), a Device page
(hardware detection + activation against Cloud's Device API), and a background device heartbeat.
Built with `pywebview`.

## Run it

```
poetry install
poetry run python main.py
```

By default this points at `https://app.browseterm.puhtaeto.com` (real, live Cloud) -- override
with the `BROWSETERM_CLOUD_API_URL` env var for local development against an instance running on
this machine instead.

## Cluster section

`desktop/cluster_manager.py` provisions a dedicated `browseterm` Multipass Ubuntu VM and installs
a pinned k3s version inside it (`multipass exec`), then merges its kubeconfig into
`~/.kube/config` as context `browseterm` (k3s's own kubeconfig always names everything "default" -
this rewrites those identifiers so it can never collide with another tool's own "default" context)
- this is the real Part 15/16 target runtime, not the k3d dev-only shortcut this module used to be
built on. `desktop/local_stack.py` then deploys the current stack onto it: MinIO, cert-manager,
Container Maker, **browseterm-device-agent** (the sole local-to-Cloud communication boundary -
Migration Part 7), status-monitor, reaper, and Socket-SSH - each via that repo's own already-tested
deploy script, same principle as before. The old `browseterm-server-local` (a full local browser-UI
server) is no longer deployed at all - migration Part 3 moved the browser UI to Cloud entirely.

Both `create_cluster()` and `deploy()` accept an optional `on_step(name, status, detail)` callback
reporting each real step ("Creating Multipass VM", "Installing k3s", "Deploying Container Maker",
...) as started/succeeded/failed - not yet consumed by this app's own UI (a later phase's job), but
the interface exists now for that phase to build on.

The Setup button additionally needs `BROWSETERM_CLOUD_INTERNAL_API_TOKEN` (byte-identical to
Cloud's own `CLOUD_INTERNAL_API_TOKEN`) - container-maker, status_monitor, reaper, and snapshot_job
all still call Cloud directly with this one shared credential for a few things that don't have a
Device Agent RPC equivalent yet (a documented, out-of-scope gap - see
`BROWSETERM_MIGRATION_PROGRESS.md`'s Part 12 section). This is **not** something you export by hand
before every launch: `desktop/config.py` reads it from `~/.browseterm/cloud_internal_api_token`
(0600, outside any git repo -- this app never writes this file itself, it's set up once by hand,
the same way Cloud's own `env.mk` secrets are), falling back to the
`BROWSETERM_CLOUD_INTERNAL_API_TOKEN` env var only if you want to override it for one run. If
Cloud's own token is ever regenerated, update that file to match -- until then,
`local_stack.check_prerequisites()` still refuses to deploy anything at all (fails in milliseconds,
not after a multi-minute VM-create + k3s-install cycle) if the value it reads doesn't match
Cloud's.

Device Agent itself needs this specific device's own Bearer credential (not the shared internal
token above) - `local_stack.deploy()` copies it from this app's own Keychain storage
(`desktop/keychain.py`) into a `browseterm-device-credential` Secret at deploy time, the one place
it's ever written to disk/the cluster, and only as a Secret.

## Login: OAuth Device Authorization Grant (RFC 8628), not a WebView OAuth page

Google (and increasingly GitHub) actively block or challenge OAuth sign-in attempted from an
embedded WebView - exactly what pywebview's window is - as a long-standing anti-phishing policy.
Earlier designs worked around this by opening Local's real `/login` page in the system browser
and bouncing back through a local loopback server; that still needed `browseterm-server-local`
reachable to serve `/login` at all, which created a real chicken-and-egg with the Cluster
section's own Setup button (no cluster -> Local unreachable -> can't log in -> can't reach the
button that creates the cluster). This app now uses the device grant instead, which needs nothing
but Cloud:

1. The WebView shows a small local "Log in" page (`desktop/web/login_start.html`) with two
   buttons, Google and GitHub. Clicking one calls Cloud's `POST /auth/device/start`
   (`desktop/cloud_client.py:start_device_login`) for that provider, which returns a short
   `user_code` and a `verification_uri` -- Cloud holds the provider's device-flow credentials
   server-side; this app never does (only "google" actually works today -- GitHub is wired up end
   to end but the live GitHub OAuth App hasn't had "Enable Device Flow" turned on yet, a manual
   console step tracked separately, so it currently 502s at this step).
2. This app shows the code in the WebView and opens `verification_uri` in the user's SYSTEM
   browser (`webbrowser.open()`). The user approves it there, in their own time; this process
   isn't involved in that step at all.
3. Meanwhile a background thread polls Cloud's `POST /auth/device/poll`
   (`desktop/cloud_client.py:poll_device_login`) on the provider's own interval. Once approved,
   Cloud has already run the same find-or-create-user + register-device + issue-token pipeline the
   old bootstrap-redeem path used, so the response is handled identically to before: the device
   Bearer token goes into macOS Keychain, and every Device API call after that uses
   `Authorization: Bearer <device_token>`.

`desktop/cloud_client.py:redeem_device_bootstrap` (trading a one-time bootstrap code for a device
token) still exists and Cloud's endpoint still serves it, but nothing in this app's own login flow
calls it any more -- it predates the device grant and is kept only because removing Cloud's side
of it wasn't in scope here.

Consequence: "am I logged in" is still "does Keychain hold a valid device token" - independent of
anything in the system browser or of `browseterm-server-local` being reachable. A valid Keychain
token on startup skips the login page entirely and goes straight to the Device page.

## How it works

- **Login**: see above -- an OAuth Device Authorization Grant against Cloud, not a WebView OAuth
  page.
- **Device bootstrap**: happens automatically as part of the device-grant poll succeeding (see
  above) -- there's no separate bootstrap step or button any more.
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
- **Logout**: clears the Keychain token + local state and returns to the login page. This app
  never holds a Local session cookie at all any more (login talks only to Cloud - see above), so
  there's nothing Local-side to revoke; any approved-but-unused device grant on the provider's
  side is left to expire on its own.

## Tests

```
poetry install
poetry run pytest tests/ -v
```

`tests/test_desktop.py` covers the device-grant login flow end to end (mocked
`start_device_login`/`poll_device_login`, a pending-then-complete poll sequence, the clicked
provider threading through to both calls) plus the still-existing `redeem_device_bootstrap`
Cloud-client function against a tiny in-process HTTP stub (bootstrap end-to-end, missing-cookie/
wrong-CSRF rejection, second-redemption-of-a-code failing, device-token-scoped calls), the
Keychain storage abstraction being swappable (a `_FakeKeychain` stands in, matching p07.md section
22's "use an abstraction so unit tests can mock storage"), and `Api.activate_device()`'s friendly
error when no credential exists yet. `tests/test_cluster_manager.py`/`tests/test_api_cluster.py`/
`tests/test_local_stack.py` cover the Cluster section separately.

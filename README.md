# browseterm-desktop

Mac-only menu-bar app that manages **machine/runtime/resources**, not the Browseterm product
itself. It is not the workspace UI, not a terminal host, and does not duplicate ContainerMaker —
the browser stays the Browseterm UI (plan section 1.2).

Split out of `browseterm-server-local` (where it was originally built as part of P06) into its
own repository so it follows the same one-component-per-repo convention as every other
Browseterm piece (`browseterm-server`, `browseterm-db`, `container-maker`, `socket-ssh`, ...).

## Responsibilities

- Authenticate the user (currently an interim mechanism — see "Auth" below; a real device-scoped
  credential flow is planned, not yet built).
- Detect this machine's real hardware: architecture, macOS version, CPU count, total memory,
  total storage (`desktop/hardware.py` — stdlib/`sysctl`/`shutil` only, no GUI-output parsing).
- Let the user set how much CPU/memory/storage (and, later, GPU) of this machine to allocate to
  Browseterm, validated client-side as defense-in-depth (`desktop/allocation.py` — the Cloud API
  is still the authoritative validator).
- Register/update this device and send heartbeats to Cloud through `src/cloud_client/`
  (`desktop/device_registration.py`), which is the *only* thing in this repo allowed to talk to
  Cloud, and only over HTTPS — never a direct DB/Redis connection.
- Report (not manage) local server/k3s health (`desktop/runtime_health.py`).
- Open Browseterm in the system browser.

## `src/cloud_client/` — the boundary

```
Desktop -> CloudClient -> HTTPS -> Cloud browseterm-server
```

Wraps the Cloud Device API (register/list/get/update/heartbeat) only. This package is
deliberately duplicated (not shared via a path dependency) with the copy in
`browseterm-server-local`, which needs the same boundary for its own calls to Cloud - matching
this project's existing precedent of duplicating shared code across the Cloud/Local boundary
(see `browseterm-server-local/README.md`'s "Trust boundary" section) rather than introducing a
cross-repo Python package dependency for a few hundred lines of HTTP client code.

## Auth (currently interim, being redesigned)

There is no real device-scoped credential flow yet. `CloudClient` currently takes the same
opaque `session` Redis-session-cookie value the browser holds after logging in via Local's
existing OAuth flow (`BROWSETERM_SESSION_COOKIE` env var) — a placeholder, not a final design.
The real design needs the Desktop app and the browser login to agree on "this browser session is
running on this device," so Local can resolve device identity at login time and gate terminal
creation by that device's actual current resource availability. That does not exist yet and is
the next real design gap to close, not a solved problem.

## Run it

```
poetry install
export BROWSETERM_CLOUD_API_URL=http://localhost:9999   # or wherever Cloud is reachable
export BROWSETERM_SESSION_COOKIE=<value of the "session" cookie after logging in via the browser>
poetry run python -m desktop.app
```

## Tests

Every OS/network boundary mocked - no live Postgres/Redis/Cloud instance needed:
```
poetry install
poetry run python -m pytest tests/unit/ -v
```

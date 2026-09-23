"""
Configuration for the Desktop app.

Mirrors the same env-var-overridable, DNS-convention-based default used by
`browseterm-server-local`'s `src/cloud_client/config.py` for Cloud: `browseterm.cloud.com`.
Override it for local development against an instance running on this machine (e.g.
`http://localhost:9999`).
"""
import os

# Cloud control plane -- owns the Device API, the OAuth Device Authorization Grant login flow
# (desktop/app.py, desktop/cloud_client.py), the browser UI (migration Part 3), and issues the
# device Bearer credential this app authenticates with. Real, live production Cloud is the
# default now (app.browseterm.puhtaeto.com went live during the Cloud Control Plane migration) --
# override with BROWSETERM_CLOUD_API_URL for local development against an instance running on
# this machine instead.
BROWSETERM_CLOUD_API_URL: str = os.getenv("BROWSETERM_CLOUD_API_URL", "https://app.browseterm.puhtaeto.com").rstrip("/")

# How often the background thread heartbeats this device (using its own long-lived device
# credential -- see desktop/daemon.py's module docstring; heartbeat ownership moved here from
# desktop/app.py so the GUI and the daemon never race each other heartbeating independently).
DEVICE_HEARTBEAT_INTERVAL_SECONDS: int = 25 * 60

# How often desktop/daemon.py's health-check loop checks the Multipass VM's own state and
# restarts any crashing monitored pod. Deliberately much shorter than the heartbeat interval --
# this is the thing that notices "the Mac slept and multipass stopped the VM" and brings it back,
# so it needs to run often enough that a real outage doesn't sit unnoticed for 25 minutes.
DAEMON_HEALTH_CHECK_INTERVAL_SECONDS: int = 60

# How long to keep polling Cloud's /auth/device/poll for the user to approve the device code on
# the provider's verification page (desktop/app.py) before giving up. Generous -- this covers
# actual human time spent on a Google/GitHub consent screen, not just network latency. Overridden
# per-attempt by the provider's own `expires_in` when /auth/device/start returns one.
DESKTOP_LOGIN_TIMEOUT_SECONDS: float = 5 * 60

# Where the last-activated device id (not secret - just an identifier) is persisted across app
# restarts, so the Device page can show it immediately. The device credential itself lives only
# in macOS Keychain (desktop/keychain.py), never here.
STATE_DIR: str = os.path.expanduser("~/.browseterm")
STATE_FILE: str = os.path.join(STATE_DIR, "desktop_state.json")

# Cluster section (desktop/local_stack.py): where the sibling repo checkouts for the local-stack
# workloads (container-maker, browseterm-device-agent, socket-ssh, status_monitor, cert-manager,
# reaper) live, so Setup can invoke each one's own `make prod_setup`/`dev_setup` target. Defaults
# to this project's own convention of cloning every repo flat under one directory (~/browseterm).
LOCAL_STACK_REPOS_DIR: str = os.path.expanduser(os.getenv("LOCAL_STACK_REPOS_DIR", "~/browseterm"))

# Must be byte-identical to Cloud's own CLOUD_INTERNAL_API_TOKEN (SETUP-LOCAL.md step 5) -- every
# internal-token-gated Local-to-Cloud call (session validate, container CRUD, catalog, sse-tokens)
# silently 401s otherwise. An env var still overrides (e.g. after Cloud's token is regenerated),
# but the day-to-day value lives in this file instead of needing to be exported by hand before
# every launch -- same `~/.browseterm` local-machine-config directory STATE_FILE already uses
# (never inside a git repo, so there's no `.gitignore` to rely on getting right), same 0600
# permissions convention. local_stack.check_prerequisites() still refuses to deploy anything at
# all if this ends up empty either way, rather than standing up a stack that fails confusingly
# later.
CLOUD_INTERNAL_API_TOKEN_FILE: str = os.path.join(STATE_DIR, "cloud_internal_api_token")


def _read_local_token_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


BROWSETERM_CLOUD_INTERNAL_API_TOKEN: str = (
    os.getenv("BROWSETERM_CLOUD_INTERNAL_API_TOKEN") or _read_local_token_file(CLOUD_INTERNAL_API_TOKEN_FILE)
)

# Docker Hub account the local-stack images are pulled from (docker.io/<name>/<component>:latest).
# REPO_PASSWORD is only ever used by container-maker for `docker login` during a save/snapshot
# build+push -- left blank by default (matching this project's own established local-dev default),
# meaning the terminal itself works fine but Save will fail until a real value is supplied.
DOCKER_HUB_REPO_NAME: str = os.getenv("DOCKER_HUB_REPO_NAME", "zim95")
DOCKER_HUB_REPO_PASSWORD: str = os.getenv("DOCKER_HUB_REPO_PASSWORD", "")

# socket-ssh's `ngrok-agent` sidecar (infra/deployment/deployment.yaml) reads NGROK_AUTHTOKEN from
# a `ngrok-credentials` Secret local_stack.py creates from this value - same file-or-env-var,
# 0600-in-~/.browseterm convention as CLOUD_INTERNAL_API_TOKEN_FILE above. Left blank by default:
# the rest of the local stack (terminal creation, container-maker, etc.) works fine without it, but
# socket-ssh's ngrok-agent container can't start without a real token (CreateContainerConfigError),
# so remote tunnel access specifically won't work until one is supplied.
NGROK_AUTHTOKEN_FILE: str = os.path.join(STATE_DIR, "ngrok_authtoken")
NGROK_AUTHTOKEN: str = os.getenv("NGROK_AUTHTOKEN") or _read_local_token_file(NGROK_AUTHTOKEN_FILE)

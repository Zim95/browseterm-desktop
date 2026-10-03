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

# This app's own secrets (Cloud's internal API token, the Docker Hub push/pull credential, the
# ngrok authtoken) live in one gitignored `env.mk` at this repo's root - the same `KEY=value`-
# per-line convention every other Browseterm repo's own env.mk already uses, rather than a
# separate ad-hoc dotfile per secret. See env.mk.example for the full list of keys and what each
# one gates. An env var still overrides any individual value for one run (e.g. CI, or testing a
# rotated token) without touching the file.
ENV_MK_PATH: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "env.mk")


def _load_env_mk(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


_ENV_MK: dict[str, str] = _load_env_mk(ENV_MK_PATH)

# Must be byte-identical to Cloud's own CLOUD_INTERNAL_API_TOKEN to be useful at all. Finishing
# Part 12 removed every local-stack component's own hard dependency on this (the device-command
# paths all authenticate as the device itself now) - the only remaining reader is
# container-maker's own save_reconciler.py (a genuinely cluster-wide sweep that can't be scoped to
# a per-device credential), so an empty value here is a soft failure (just that one sweep 401s)
# rather than something Setup refuses to proceed without - see
# local_stack._ensure_internal_api_token_secret's own docstring.
BROWSETERM_CLOUD_INTERNAL_API_TOKEN: str = (
    os.getenv("BROWSETERM_CLOUD_INTERNAL_API_TOKEN") or _ENV_MK.get("BROWSETERM_CLOUD_INTERNAL_API_TOKEN", "")
)

# Docker Hub account the local-stack images are pulled from (docker.io/<name>/<component>:latest).
# REPO_PASSWORD is used by container-maker both for `docker login` during a save/snapshot
# build+push AND to build the per-user-namespace image-pull secret every CREATE/RESUME needs to
# pull a private saved snapshot (see container-maker's NamespaceManager._apply_image_pull_secret) --
# so a from-scratch Setup (e.g. after deleting and recreating the Multipass VM) doesn't silently
# recreate that Secret with an empty password again. Left blank if env.mk was never filled in: the
# terminal itself works fine but Save, and Resuming/Creating anything from a saved snapshot, will
# fail until a real value is supplied.
DOCKER_HUB_REPO_NAME: str = os.getenv("DOCKER_HUB_REPO_NAME") or _ENV_MK.get("DOCKER_HUB_REPO_NAME", "zim95")
DOCKER_HUB_REPO_PASSWORD: str = os.getenv("DOCKER_HUB_REPO_PASSWORD") or _ENV_MK.get("DOCKER_HUB_REPO_PASSWORD", "")

# socket-ssh's `ngrok-agent` sidecar (infra/deployment/deployment.yaml) reads NGROK_AUTHTOKEN from
# a `ngrok-credentials` Secret local_stack.py creates from this value. Left blank if env.mk was
# never filled in: the rest of the local stack (terminal creation, container-maker, etc.) works
# fine without it, but socket-ssh's ngrok-agent container can't start without a real token
# (CreateContainerConfigError), so remote tunnel access specifically won't work until one is set.
NGROK_AUTHTOKEN: str = os.getenv("NGROK_AUTHTOKEN") or _ENV_MK.get("NGROK_AUTHTOKEN", "")

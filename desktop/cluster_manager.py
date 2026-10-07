"""
Local Multipass VM + k3s cluster lifecycle for the Cluster section of the Device page
(BROWSETERM_CLOUD_CONTROL_PLANE_MIGRATION.md Part 15/16: "a Browseterm-managed Multipass Ubuntu VM
running k3s" is the real target macOS runtime - not k3d, which was always the local-dev-only
shortcut this module previously used).

Shells out to `multipass`/`kubectl` - this project's existing convention for cluster lifecycle
everywhere else, so no Kubernetes Python client dependency is needed for the small set of
operations here.

Architecture: `multipass launch` creates a dedicated `browseterm` Ubuntu VM; k3s installs INSIDE
it via the official install script over `multipass exec`. k3s's own kubeconfig
(/etc/rancher/k3s/k3s.yaml) is not directly usable from the host as-is - two reasons: (1) it points
at `127.0.0.1:6443`, which resolves to the HOST's own loopback, not the VM's, so it has to be
rewritten to the VM's real IP; (2) k3s always names its single cluster/user/context "default",
which would silently collide with (overwrite) any other tool's own "default" context already in
~/.kube/config (Docker Desktop, Colima, etc. all use that same name) if merged in as-is - so every
"default" identifier is renamed to "browseterm" before merging. This keeps every downstream
`kubectl --context browseterm ...` call working exactly like every sibling local-stack repo's own
deploy scripts already expect (see local_stack.py), without needing a PyYAML dependency this
project doesn't otherwise have - k3s's own kubeconfig output is a small, fixed-shape file (exactly
one cluster/user/context, all always literally named "default"), so targeted text substitution on
that known shape is precise enough and avoids adding a new dependency for it.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from typing import Any, Callable, Optional

MULTIPASS_DOWNLOAD_URL = "https://multipass.run/install"

# This app is a PyInstaller windowed build (console=False) - launched via Finder/double-click (or
# the Windows Start Menu shortcut the installer creates), not from a login shell, it inherits only
# the OS's bare-minimum default PATH (`/usr/bin:/bin:/usr/sbin:/sbin` on macOS), never whatever a
# .zprofile/.bashrc would normally export. Homebrew's own install location - /opt/homebrew/bin on
# Apple Silicon - is routinely missing from that minimal PATH even though `brew` (and anything
# brew installed, like Multipass itself) is genuinely present on the machine. Caught for real:
# this made `_install_multipass_macos` below treat a perfectly good Homebrew install as "not
# available" and fall straight to the download-page fallback, even though running the exact same
# check from a Terminal found `brew` fine. Fixing this once, process-wide, at import time - rather
# than only around the one `shutil.which("brew")` check - also fixes every other bare `multipass`/
# `kubectl` call in this module (and local_stack.py's) that had the exact same latent gap.
_EXTRA_PATH_DIRS: dict[str, tuple[str, ...]] = {
    "darwin": ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"),
    "win32": (
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"),
        os.path.expandvars(r"%ProgramFiles%\Multipass\bin"),
    ),
}


def _ensure_tool_dirs_on_path() -> None:
    """Idempotent and safe to call repeatedly - only ever appends directories that (a) actually
    exist on disk and (b) aren't already on PATH, so it can't shadow/reorder anything the process
    already had. Module-level call below runs this once, at import time, rather than requiring
    every call site in this file (and local_stack.py's) to remember to call it first."""
    extra = _EXTRA_PATH_DIRS.get(sys.platform, ())
    current_dirs = os.environ.get("PATH", "").split(os.pathsep)
    missing = [d for d in extra if d and os.path.isdir(d) and d not in current_dirs]
    if missing:
        os.environ["PATH"] = os.pathsep.join(current_dirs + missing)


_ensure_tool_dirs_on_path()

VM_NAME = "browseterm"
KUBE_CONTEXT = "browseterm"
# Pinned, not "latest" - same convention every other component in this project pins its runtime
# version (k3s on the Contabo prod host, poetry.lock, etc.). Override via env var if a newer
# channel release is deliberately wanted for a specific machine.
K3S_VERSION = os.getenv("BROWSETERM_K3S_VERSION", "v1.31.2+k3s1")
UBUNTU_IMAGE = os.getenv("BROWSETERM_MULTIPASS_IMAGE", "22.04")
KUBE_CONFIG_PATH = os.path.expanduser(os.getenv("KUBECONFIG", "~/.kube/config"))

_DEFAULT_TIMEOUT_SECONDS = 30.0
# `multipass launch` includes a cold-cache full image download, not just VM boot - on a fresh
# machine (or right after a corrupted-cache purge) that's a gigabyte-scale fetch over the
# ubuntu-22.04-server-cloudimg archive, which alone can take several minutes on an ordinary home
# connection, before cloud-init even starts. 180s was only ever enough for a warm-cache relaunch;
# measured against a real ~2.5MB/s connection, a cold download plus boot needs headroom well past
# that.
_LAUNCH_TIMEOUT_SECONDS = 900.0
# Same real-world headroom as native_k3s.py's own _K3S_INSTALL_TIMEOUT_SECONDS (the k3s install
# script downloads the k3s binary from GitHub releases at install time) - this constant was missed
# in that earlier pass since it lives in this module, not native_k3s.py, and caught the exact same
# way: a re-run against an already-running VM under host load timed out at 150s here too.
_K3S_INSTALL_TIMEOUT_SECONDS = 300.0
_K3S_READY_TIMEOUT_SECONDS = 90.0
_DELETE_TIMEOUT_SECONDS = 60.0
_GVISOR_INSTALL_TIMEOUT_SECONDS = 120.0
# Same headroom as _LAUNCH_TIMEOUT_SECONDS - a cold Multipass install is itself a real VM
# hypervisor package (QEMU/Hyper-V driver bits included), not just a small CLI binary, and on top
# of the download this has to leave room for a human actually responding to the native
# password/UAC prompt the installer raises (see _install_multipass_macos/_windows docstrings).
_MULTIPASS_INSTALL_TIMEOUT_SECONDS = 900.0
_DISM_QUERY_TIMEOUT_SECONDS = 30.0
# DISM itself is quick, but it has to wait on the UAC prompt PowerShell's `Start-Process -Verb
# RunAs` raises for it first - same human-response headroom as _MULTIPASS_INSTALL_TIMEOUT_SECONDS.
_HYPERV_ENABLE_TIMEOUT_SECONDS = 900.0

# The pod monitor shows only the workloads that need to stay continuously running to keep the
# local execution plane usable - verified against each component's actual current manifest
# (browseterm-device-agent/infra/deployment.yaml, container-maker/infra/k8s/deployment/
# deployment.yaml, socket-ssh/infra/deployment/deployment.yaml, browseterm_workload's own
# per-component manifests), not a remembered/assumed list:
#   Deployments (always-on - monitored): container-maker, browseterm-device-agent, socket-ssh
#   (whose Pod also carries the tunnel-registrar sidecar - no separate pod to list), status-monitor,
#   minio.
#   CronJobs/Jobs (come up only when triggered - deliberately NOT monitored): reaper (CronJob,
#   hourly idle sweep), snapshot-job (no persistent Deployment - container-maker spawns it as a
#   one-off Job per save), minio-createbucket (one-shot Job, exits after creating the bucket).
# `browseterm-server` (the old Local browser UI) is gone from this list entirely - migration Part 3
# moved the browser UI to Cloud, so nothing with that name is deployed locally any more.
MONITORED_WORKLOAD_PREFIXES = (
    "container-maker",
    "browseterm-device-agent",
    "socket-ssh",
    "status-monitor",
    "minio",
)

_EXCLUDED_WORKLOAD_PREFIXES = (
    "minio-createbucket",
)

# A step-progress callback: on_step(step_name, status, detail) where status is one of
# "started"/"succeeded"/"failed". `detail` carries extra context (e.g. an error message on
# failure), empty string otherwise. Optional everywhere it's accepted - defaults to a no-op so
# existing callers (and every test not concerned with step reporting) don't need to change.
StepCallback = Callable[[str, str, str], None]


def _noop_step(_name: str, _status: str, _detail: str) -> None:
    pass


class ClusterError(Exception):
    """Raised for a failed/missing CLI tool, a timeout, or a non-zero exit from multipass/kubectl."""


def _run(
    cmd: list[str], timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    cwd: "str | None" = None, input_text: "str | None" = None,
) -> str:
    """Shared low-level runner - also used by `local_stack.py` (`cwd` to invoke a sibling repo's
    own `make` target in its own directory, `input_text` to pipe rendered YAML into `kubectl apply
    -f -` without a shell pipe)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, input=input_text)
    except FileNotFoundError as e:
        raise ClusterError(f"'{cmd[0]}' not found -- is it installed and on PATH?") from e
    except subprocess.TimeoutExpired as e:
        raise ClusterError(f"'{' '.join(cmd)}' timed out after {timeout:.0f}s") from e
    if result.returncode != 0:
        raise ClusterError(result.stderr.strip() or f"'{' '.join(cmd)}' failed")
    return result.stdout


def _step(on_step: Optional[StepCallback], name: str, status: str, detail: str = "") -> None:
    (on_step or _noop_step)(name, status, detail)


def _run_step(on_step: Optional[StepCallback], name: str, fn: Callable[[], None]) -> None:
    """Runs `fn`, reporting started/succeeded/failed around it. Re-raises on failure after
    reporting, so the caller's own try/except (desktop/api.py's setup_cluster) still works
    unchanged - this only adds visibility, it doesn't change control flow."""
    _step(on_step, name, "started")
    try:
        fn()
    except ClusterError as e:
        _step(on_step, name, "failed", str(e))
        raise
    _step(on_step, name, "succeeded")


def cluster_exists() -> bool:
    try:
        info = json.loads(_run(["multipass", "info", VM_NAME, "--format", "json"]))
    except ClusterError:
        return False
    return VM_NAME in info.get("info", {})


def vm_state() -> str:
    '''"Running"/"Stopped"/... as multipass itself reports it, or "Missing" if the VM doesn't
    exist at all - used by the daemon's health-check loop to notice a VM that stopped (e.g. after
    the Mac slept) without needing its own info-parsing logic. Never raises - a multipass error
    here just means "can't tell," reported as "Unknown" rather than propagating, since a health
    check should never crash the daemon over a transient CLI hiccup.'''
    try:
        info = json.loads(_run(["multipass", "info", VM_NAME, "--format", "json"]))
    except ClusterError:
        return "Unknown"
    if VM_NAME not in info.get("info", {}):
        return "Missing"
    return _vm_state(info)


def start_vm() -> None:
    '''Starts an existing, stopped VM back up - the daemon's own recovery action for "the Mac
    slept and multipass stopped the VM." A no-op error from multipass if the VM is already
    running (idempotent, matches every other lifecycle op in this module).'''
    _run(["multipass", "start", VM_NAME], timeout=_LAUNCH_TIMEOUT_SECONDS)


def _vm_state(info: dict[str, Any]) -> str:
    return info.get("info", {}).get(VM_NAME, {}).get("state", "Unknown")


def _vm_ip(info: dict[str, Any]) -> str:
    ips = info.get("info", {}).get(VM_NAME, {}).get("ipv4", [])
    if not ips:
        raise ClusterError(f"multipass VM '{VM_NAME}' has no IPv4 address yet")
    return ips[0]


def _multipass_exec(args: list[str], timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> str:
    return _run(["multipass", "exec", VM_NAME, "--"] + args, timeout=timeout)


def _multipass_on_path() -> bool:
    return shutil.which("multipass") is not None


def _install_multipass_macos() -> None:
    """Prefers Homebrew (ubiquitous on dev Macs, and the same cask Canonical itself publishes:
    `brew install --cask multipass`). This cask's installer needs to run a privileged .pkg (it
    installs `multipassd` as a root LaunchDaemon) - Homebrew has, since well before this was
    written, handled the "no TTY available" case a GUI app's subprocess hits here by shelling out
    to `osascript ... with administrator privileges`, which raises a native macOS password dialog
    instead of hanging on a console `sudo` prompt that can never be answered. If Homebrew itself
    isn't present, there's no safe unattended path left (installing Homebrew is itself a
    sudo-gated, multi-minute operation this flow has no business triggering on its own) - open the
    official installer page and ask the user to finish this one step by hand."""
    if shutil.which("brew") is None:
        webbrowser.open(MULTIPASS_DOWNLOAD_URL)
        raise ClusterError(
            "Multipass isn't installed, and Homebrew isn't available to install it automatically "
            "-- opened the Multipass download page. Install it, then click Setup again."
        )
    _run(["brew", "install", "--cask", "multipass"], timeout=_MULTIPASS_INSTALL_TIMEOUT_SECONDS)


def _install_multipass_windows() -> None:
    """winget ships built-in on Windows 10 1709+/11, so it's the safe default here (unlike
    Homebrew on macOS, nothing extra needs installing first). `Canonical.Multipass` is the real,
    published winget package id. Multipass's own installer requires elevation (it sets up a
    Hyper-V-backed VM backend) - winget-launched installers that declare an elevation requirement
    trigger the normal Windows UAC consent dialog, a secure-desktop OS prompt that works
    regardless of the calling process's own console/TTY state, so this is safe to invoke from a
    GUI app's subprocess the same way the macOS path is."""
    if shutil.which("winget") is None:
        webbrowser.open(MULTIPASS_DOWNLOAD_URL)
        raise ClusterError(
            "Multipass isn't installed, and winget isn't available to install it automatically "
            "-- opened the Multipass download page. Install it, then click Setup again."
        )
    _run(
        [
            "winget", "install", "--id", "Canonical.Multipass", "-e", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
        ],
        timeout=_MULTIPASS_INSTALL_TIMEOUT_SECONDS,
    )


def _ensure_multipass_installed() -> None:
    """First real step of Setup now (see create_cluster below) - previously, a missing `multipass`
    binary surfaced as a bare "'multipass' not found -- is it installed and on PATH?" failure deep
    inside the "Creating Multipass VM" step, with no attempt to actually install it even though
    the owner's own expectation (and this app's whole pitch) is that clicking Setup is enough.

    No-ops instantly if multipass is already on PATH - every other call site here still re-checks
    via plain `shutil.which`/`_run`, never caching this result, so a user who installs Multipass
    mid-session and retries Setup is picked up correctly without a restart.

    After a successful install, the freshly-installed binary still might not resolve on THIS
    already-running process's PATH (Homebrew/winget update the shell-level or registry PATH, which
    an already-running GUI process never re-reads) - that failure mode gets its own explicit,
    actionable message rather than a second confusing "not found"."""
    if _multipass_on_path():
        return
    if sys.platform == "darwin":
        _install_multipass_macos()
    elif sys.platform == "win32":
        _install_multipass_windows()
    else:
        webbrowser.open(MULTIPASS_DOWNLOAD_URL)
        raise ClusterError(
            f"Multipass isn't installed, and this platform ({sys.platform}) has no automatic "
            "install path here -- opened the Multipass download page. Install it, then click "
            "Setup again."
        )
    if not _multipass_on_path():
        raise ClusterError(
            "Multipass was installed, but this already-running app can't see it on PATH yet -- "
            "please restart Browseterm Desktop, then click Setup again."
        )


def _hyperv_supported() -> bool:
    """Hyper-V is a Windows Pro/Enterprise/Education feature - Home editions can't run it at all,
    no amount of DISM toggling changes that. `platform.win32_edition()` (Windows-only stdlib,
    exists only when sys.platform == "win32") reports the Home-equivalent SKUs as some variant of
    "Core" (`Core`, `CoreN`, `CoreSingleLanguage`, `CoreCountrySpecific`, ...) - every
    Pro/Enterprise/Education SKU's own id is a different word entirely, so this one substring
    check is reliable without needing a hardcoded enum of every real SKU name."""
    return "core" not in platform.win32_edition().lower()


def _hyperv_enabled() -> bool:
    out = _run(
        ["dism", "/online", "/get-featureinfo", "/featurename:Microsoft-Hyper-V"],
        timeout=_DISM_QUERY_TIMEOUT_SECONDS,
    )
    return any(line.strip().lower() == "state : enabled" for line in out.splitlines())


def _enable_hyperv() -> None:
    """Unlike the Multipass installer winget just ran (which has its own `requireAdministrator`
    manifest Windows auto-elevates via a UAC prompt even when launched non-elevated), DISM has no
    such manifest - run non-elevated it just fails immediately with "Error: 740, Elevated
    permissions are required to run DISM," no prompt raised at all. Needs an explicit elevation
    request: PowerShell's `Start-Process -Verb RunAs -Wait` is the standard idiom for that (same
    native UAC consent dialog as everywhere else here, just asked for up front instead of relying
    on DISM to ask for it itself, which it never does). `/norestart` keeps DISM from triggering its
    own automatic reboot - see _ensure_hyperv_enabled's own docstring for why that has to stay a
    choice this flow surfaces to the user, never one it makes for them."""
    _run(
        [
            "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
            "Start-Process -FilePath dism.exe -ArgumentList "
            "'/online','/enable-feature','/featurename:Microsoft-Hyper-V','/all','/norestart' "
            "-Verb RunAs -Wait",
        ],
        timeout=_HYPERV_ENABLE_TIMEOUT_SECONDS,
    )


def _ensure_hyperv_enabled() -> None:
    """Windows-only - a no-op everywhere else (macOS's Multipass backend is QEMU, not Hyper-V).
    The exact next failure a fresh Multipass install on Windows hits, caught for real: "launch
    failed: The Hyper-V service does not exist. Ensure Hyper-V is installed correctly." Hyper-V is
    a Windows OPTIONAL FEATURE, off by default on a clean install - neither Multipass's own
    installer nor winget turns it on; something has to, and nothing did before this.

    Enabling it (on success) ALWAYS requires a full Windows restart before it actually takes
    effect - DISM can't do anything about that, and this flow has no business silently restarting
    the user's machine out from under them (unsaved work, anything else they have open). So this
    always ends in a ClusterError on the path that just ran the enable - either "go restart
    Windows" on success, or the DISM failure itself - never a silent success; create_cluster's
    caller surfaces this exactly like the "Multipass installed, restart the app" case above."""
    if sys.platform != "win32":
        return
    if not _hyperv_supported():
        raise ClusterError(
            "Your Windows edition doesn't support Hyper-V (Home editions can't run it) -- "
            "Multipass needs VirtualBox instead on Windows Home. Install VirtualBox from "
            "https://www.virtualbox.org/, then click Setup again."
        )
    if _hyperv_enabled():
        return
    _enable_hyperv()
    raise ClusterError(
        "Hyper-V has been enabled, but Windows needs a restart before it actually takes effect -- "
        "please restart your computer, then click Setup again."
    )


def _create_vm(cpu_cores: int, memory_gb: float, storage_gb: float) -> None:
    if cluster_exists():
        return
    # cluster_exists() itself can false-negative under host load - it runs `multipass info` with
    # only _DEFAULT_TIMEOUT_SECONDS (30s) and treats ANY failure there (including a timeout) as
    # "doesn't exist" (see its own body), since a genuinely-missing VM and a slow/failed check are
    # indistinguishable from a plain ClusterError alone. Caught for real: with the host under load
    # (a concurrent docker build, in this project's own case), `multipass info` timed out, this
    # function concluded the VM was missing, and `multipass launch` below hit multipass's own
    # authoritative "instance already exists" rejection - so that specific failure is treated as
    # success here rather than propagated, instead of tightening yet another timeout that would
    # just move the same race somewhere else under sufficiently bad load.
    try:
        _run(
            [
                "multipass", "launch", UBUNTU_IMAGE, "--name", VM_NAME,
                "--cpus", str(cpu_cores),
                "--memory", f"{memory_gb:g}G",
                "--disk", f"{storage_gb:g}G",
            ],
            timeout=_LAUNCH_TIMEOUT_SECONDS,
        )
    except ClusterError as e:
        if "already exists" not in str(e):
            raise


def _install_k3s() -> None:
    """Idempotent - `curl | sh` re-run against an already-installed k3s just re-applies the same
    version and restarts the service, matching how every other pinned-version install in this
    project (e.g. the Contabo prod host) already behaves.

    Bundled Traefik and servicelb are disabled (same flags the project's old single-node
    scripts/setup.k3s.sh used) - this single-tenant local cluster has no LoadBalancer-type Service
    and no Ingress a controller actually needs to serve (socket-ssh's own Ingress object is inert,
    real terminal exposure goes through ngrok - see local_stack.py's own docstring), so running
    those controllers here would only be unused attack surface and resource cost, not a
    functional requirement. `local-path` (the default StorageClass) is deliberately NOT disabled -
    MinIO/Postgres/Redis PVCs need it for dynamic provisioning."""
    install_cmd = (
        f"curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION={K3S_VERSION} "
        "INSTALL_K3S_EXEC='--write-kubeconfig-mode 644 --disable traefik --disable servicelb' sh -"
    )
    _multipass_exec(["sudo", "bash", "-c", install_cmd], timeout=_K3S_INSTALL_TIMEOUT_SECONDS)
    _wait_for_node_ready()


def _wait_for_node_ready() -> None:
    _multipass_exec(
        ["sudo", "k3s", "kubectl", "wait", "--for=condition=Ready", "node", "--all", "--timeout=90s"],
        timeout=_K3S_READY_TIMEOUT_SECONDS,
    )


# Sandboxes the untrusted-root-shell USER pods container-maker creates (its manifest hardcodes
# `runtimeClassName: gvisor` unconditionally - see container-maker/infra/k8s/deployment/
# deployment.yaml's own USER_POD_RUNTIME_CLASS env, added migration-independently back when this
# project first solved tenant isolation, see 00_docs/k3s_single_node.md/scripts/setup.k3s.sh, the
# project's old single-node-k3s reference setup). Without runsc installed and this RuntimeClass
# registered, a pod referencing it simply never schedules (stays Pending forever) - so on THIS
# Multipass VM specifically, every terminal a user creates would hang if this step were skipped;
# this is a hard prerequisite, not a hardening nicety.
_GVISOR_INSTALL_SCRIPT = r"""
set -euo pipefail
ARCH="$(uname -m)"   # aarch64 on Apple Silicon, x86_64 on Intel - gVisor publishes both
TMPL=/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl

if command -v runsc >/dev/null 2>&1 && [ -x /usr/local/bin/gvisor-bin/gvisor_sentry ]; then
  echo "  runsc already installed ($(runsc --version | head -1))"
else
  # gVisor stopped publishing standalone runsc/containerd-shim-runsc-v1 binaries at their old
  # per-file URLs (${URL}/runsc etc. now 404) - the release is a single bundled archive now, with
  # one sha512 for the whole archive rather than one per binary. runsc/containerd-shim-runsc-v1
  # sit at the archive's own root; a gvisor-bin/ subdirectory holds extra tools alongside them
  # (checkpointgofer, gvisor-sentry-prewarmer, gvisor_sentry, runsc-fd-parking,
  # runsc-metric-server). gvisor_sentry was assumed unused here and left out, but the runsc shim
  # actually requires it at container-creation time now (it runs the sandboxed process as a
  # separate "sidecar" it execs into via --sidecar-usage-policy=STRICT) - without it every pod's
  # sandbox creation fails outright ("sidecar gvisor_sentry not usable ... no such file or
  # directory"), which stays Pending/ContainerCreating forever from Kubernetes' side. zstd (not
  # bzip2) is what Ubuntu 22.04's cloud image actually ships, so that's the archive variant
  # fetched (`gvisor.tar.zstd`, not the also-published `.tar.bz2`).
  echo "  downloading gvisor release archive (${ARCH})"
  URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"
  workdir="$(mktemp -d)"; cd "$workdir"
  wget -q "${URL}/gvisor.tar.zstd" "${URL}/gvisor.tar.zstd.sha512"
  sha512sum -c gvisor.tar.zstd.sha512
  tar --zstd -xf gvisor.tar.zstd runsc containerd-shim-runsc-v1 gvisor-bin/gvisor_sentry
  chmod a+rx runsc containerd-shim-runsc-v1 gvisor-bin/gvisor_sentry
  mv runsc containerd-shim-runsc-v1 /usr/local/bin/
  mkdir -p /usr/local/bin/gvisor-bin
  mv gvisor-bin/gvisor_sentry /usr/local/bin/gvisor-bin/gvisor_sentry
  cd /; rm -rf "$workdir"
  echo "  installed $(runsc --version | head -1)"
fi

# Register a `runsc` runtime with k3s's bundled containerd via a config template.
# `{{ template "base" . }}` pulls in everything k3s would normally generate; we only append the
# runsc runtime handler.
if [ -f "$TMPL" ] && grep -q 'runtimes.runsc' "$TMPL"; then
  echo "  containerd template already has the runsc runtime - leaving k3s untouched"
else
  echo "  writing $TMPL with a runsc runtime block"
  mkdir -p "$(dirname "$TMPL")"
  cat > "$TMPL" <<'TOML'
{{ template "base" . }}

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
TOML
  echo "  restarting k3s to pick up the new containerd config (brief blip; it comes back)"
  systemctl restart k3s
fi
""".strip()


def _install_gvisor() -> None:
    """Idempotent (mirrors scripts/setup.k3s.sh's own GVISOR block verbatim) - skips the download
    only when both runsc AND gvisor_sentry are already installed, only (re)writes the containerd
    template + restarts k3s when the runsc runtime block is missing. Re-running Setup against a VM
    that already has runsc but is missing gvisor_sentry (every VM set up before that binary was
    added here) re-downloads the archive and installs just the missing piece - runsc itself is
    left untouched. The RuntimeClass object itself (a cluster resource, not a node one) is applied
    by local_stack.py's deploy(), not here - same split scripts/setup.k3s.sh and
    scripts/deploy.k3s.sh already used."""
    _multipass_exec(["sudo", "bash", "-c", _GVISOR_INSTALL_SCRIPT], timeout=_GVISOR_INSTALL_TIMEOUT_SECONDS)
    _wait_for_node_ready()


def _fetch_and_merge_kubeconfig() -> None:
    """Rewrites k3s's own kubeconfig (server IP, and every "default"-named identifier - see module
    docstring for why both are necessary) and merges it into the host's ~/.kube/config as context
    `browseterm`, so every downstream `kubectl --context browseterm ...` call (this module, and
    every sibling repo's own deploy script via local_stack.py) just works."""
    info = json.loads(_run(["multipass", "info", VM_NAME, "--format", "json"]))
    vm_ip = _vm_ip(info)
    raw = _multipass_exec(["sudo", "cat", "/etc/rancher/k3s/k3s.yaml"])

    rewritten = raw.replace("https://127.0.0.1:6443", f"https://{vm_ip}:6443")
    # k3s's default kubeconfig names its one cluster/user/context, and current-context, all
    # literally "default" - rename every one of those four fixed fields to "browseterm" so merging
    # this into ~/.kube/config can never silently collide with another tool's own "default"
    # context. Matches on the exact field patterns k3s's fixed-shape output always uses, not a
    # blanket string replace, so this stays correct even if "default" ever appeared as a namespace
    # or other unrelated value elsewhere in the file.
    # `users:`' own list item writes "name:" as its FIRST field, right after the "- " list marker
    # (`- name: default`), unlike `clusters:`/`contexts:` where "name:" is a later sibling key on
    # its own indented line (`  name: default`) - a line starting with a literal "-" doesn't match
    # a plain `^\s*name:` prefix, so the "name:" pattern below has to allow an optional leading
    # "- " (not just whitespace) to catch both shapes, or the users: entry silently keeps its old
    # "default" name while cluster/context/current-context all get renamed around it - exactly the
    # dangling "context references nonexistent user" break this fixes.
    for pattern in (r"^(\s*(?:-\s+)?name:\s*)default\s*$", r"^(\s*cluster:\s*)default\s*$",
                    r"^(\s*user:\s*)default\s*$", r"^(current-context:\s*)default\s*$"):
        rewritten = re.sub(pattern, rf"\g<1>{KUBE_CONTEXT}", rewritten, flags=re.MULTILINE)

    fetched_path = os.path.join(os.path.dirname(KUBE_CONFIG_PATH) or ".", ".browseterm-kubeconfig.tmp")
    os.makedirs(os.path.dirname(fetched_path), exist_ok=True)
    with open(fetched_path, "w") as f:
        f.write(rewritten)
    try:
        os.makedirs(os.path.dirname(KUBE_CONFIG_PATH) or ".", exist_ok=True)
        if not os.path.exists(KUBE_CONFIG_PATH):
            open(KUBE_CONFIG_PATH, "a").close()
        env_kubeconfig = f"{KUBE_CONFIG_PATH}:{fetched_path}"
        result = subprocess.run(
            ["kubectl", "config", "view", "--flatten"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "KUBECONFIG": env_kubeconfig},
        )
        if result.returncode != 0:
            raise ClusterError(result.stderr.strip() or "kubectl config view --flatten failed")
        with open(KUBE_CONFIG_PATH, "w") as f:
            f.write(result.stdout)
        _run(["kubectl", "config", "use-context", KUBE_CONTEXT], timeout=15)
    finally:
        if os.path.exists(fetched_path):
            os.remove(fetched_path)


def create_cluster(
    cpu_cores: int, memory_gb: float, storage_gb: float = 40.0,
    on_step: Optional[StepCallback] = None,
) -> None:
    _run_step(on_step, "Installing Multipass", _ensure_multipass_installed)
    _run_step(on_step, "Enabling Hyper-V", _ensure_hyperv_enabled)
    _run_step(on_step, "Creating Multipass VM", lambda: _create_vm(cpu_cores, memory_gb, storage_gb))
    _run_step(on_step, "Installing k3s", _install_k3s)
    _run_step(on_step, "Installing gVisor sandbox runtime", _install_gvisor)
    _run_step(on_step, "Configuring kubectl access", _fetch_and_merge_kubeconfig)


def delete_cluster() -> None:
    _run(["multipass", "delete", VM_NAME, "--purge"], timeout=_DELETE_TIMEOUT_SECONDS)
    # Best-effort context cleanup - not fatal if it fails (e.g. context was never merged in this
    # session), the VM being gone is what actually matters.
    for args in (
        ["kubectl", "config", "delete-context", KUBE_CONTEXT],
        ["kubectl", "config", "delete-cluster", KUBE_CONTEXT],
        ["kubectl", "config", "delete-user", KUBE_CONTEXT],
    ):
        try:
            _run(args, timeout=10)
        except ClusterError:
            pass


def is_monitored_pod(name: str) -> bool:
    '''The one place the monitored/excluded prefix lists are actually consulted - shared by
    list_pods() and this module's own tests, so a test can never drift from what production code
    actually does.'''
    return name.startswith(MONITORED_WORKLOAD_PREFIXES) and not name.startswith(_EXCLUDED_WORKLOAD_PREFIXES)


_CRASH_WAITING_REASONS = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
})


def _is_crashing(phase: str, statuses: list[dict[str, Any]]) -> bool:
    if phase == "Failed":
        return True
    for status in statuses:
        state = status.get("state", {})
        if state.get("waiting", {}).get("reason") in _CRASH_WAITING_REASONS:
            return True
        if state.get("terminated", {}).get("reason") == "Error":
            return True
    return False


def list_pods() -> list[dict[str, Any]]:
    if not cluster_exists():
        return []
    data = json.loads(_run(["kubectl", "--context", KUBE_CONTEXT, "get", "pods", "-A", "-o", "json"]) or "{}")
    pods = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        if not is_monitored_pod(name):
            continue
        statuses = item.get("status", {}).get("containerStatuses", [])
        phase = item.get("status", {}).get("phase", "Unknown")
        pods.append({
            "namespace": item["metadata"]["namespace"],
            "name": name,
            "phase": phase,
            "ready": f"{sum(1 for s in statuses if s.get('ready'))}/{len(statuses)}",
            "restarts": sum(s.get("restartCount", 0) for s in statuses),
            "crashing": _is_crashing(phase, statuses),
        })
    return pods


def restart_pod(namespace: str, name: str) -> None:
    '''Deletes the named pod outright rather than looking up and rolling its owning Deployment -
    every workload in MONITORED_WORKLOAD_PREFIXES is a Deployment (CronJob-spawned pods were
    deliberately excluded from monitoring, see that constant's own docstring), so the owning
    ReplicaSet recreates it immediately.'''
    _run(["kubectl", "--context", KUBE_CONTEXT, "-n", namespace, "delete", "pod", name])

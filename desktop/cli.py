"""
Headless Linux CLI (migration Part 18): `browseterm setup/start/stop/status/logs/configure/
activate/repair/diagnostics/uninstall`, running native (no VM) k3s via desktop/native_k3s.py.

Deliberately reuses local_stack.py's deploy()/check_prerequisites() and cluster_manager.py's pure,
already-portable pod-listing helpers unchanged (see native_k3s.py's own module docstring for the
KUBE_CONTEXT-identity trick that makes this possible) - the only genuinely new code here is the
native (non-Multipass) k3s lifecycle, Linux hardware detection, headless device-linking, and the
CLI plumbing itself. No GUI dependency (pywebview) anywhere in this module - it must import and
run on a machine with no desktop session at all.

Every subcommand returns an int exit code rather than calling sys.exit() directly, so tests can
call them and assert on the return value without a SystemExit escaping the test.
"""
import argparse
import sys
import time
from typing import Optional

from desktop import local_stack, native_k3s
from desktop.cloud_client import CloudClient, CloudClientError, poll_device_login, start_device_login
from desktop.cluster_manager import ClusterError
from desktop.config import DESKTOP_LOGIN_TIMEOUT_SECONDS
from desktop.device_info import BYTES_PER_GB, default_allocation, detect_hardware
from desktop.linux_credential_store import LinuxFileCredentialStore
from desktop.local_stack import LocalStackError
from desktop.state import DesktopState, load_state

_DEFAULT_POLL_INTERVAL_SECONDS = 5
_POLL_TERMINAL_ERROR_MESSAGES = {
    "expired": "The login code expired. Please try again.",
    "denied": "Login was denied.",
}


class CliError(Exception):
    """Raised for any subcommand failure this module wants reported as a clean message, not a
    traceback - main() catches this at the top level, everything else propagates (a real bug)."""


def _print_step(name: str, status: str, detail: str = "") -> None:
    line = f"  [{status}] {name}"
    if detail:
        line += f" - {detail}"
    print(line)


def ensure_login(state: DesktopState, credential_store: LinuxFileCredentialStore, provider: str = "google") -> str:
    """Returns the device token, running the headless OAuth Device Authorization Grant flow
    (Part 4/18: "print verification URL and human-readable code... poll securely") if none is
    already stored. Reuses cloud_client.start_device_login/poll_device_login exactly as the macOS
    GUI's own login flow does (desktop/app.py) - this is the same RFC 8628 flow, just driven by
    stdout/blocking sleep instead of a WebView and a background thread."""
    token = credential_store.get_device_token()
    if token:
        return token

    hardware = detect_hardware()
    state.ensure_allocation_defaults(hardware)
    device_payload = {
        **hardware,
        "allocated_cpu": state.allocated_cpu,
        "allocated_memory_bytes": int(state.allocated_memory_gb * BYTES_PER_GB),
        "allocated_storage_bytes": int(state.allocated_storage_gb * BYTES_PER_GB),
    }

    try:
        start = start_device_login(provider)
    except CloudClientError as e:
        raise CliError(f"Could not start login: {e.message}") from e

    verification_uri = start.get("verification_uri_complete") or start.get("verification_uri")
    print(f"To link this device, visit: {verification_uri}")
    print(f"And enter code: {start['user_code']}")

    device_code = start["device_code"]
    interval = max(start.get("interval") or _DEFAULT_POLL_INTERVAL_SECONDS, _DEFAULT_POLL_INTERVAL_SECONDS)
    deadline = time.monotonic() + (start.get("expires_in") or DESKTOP_LOGIN_TIMEOUT_SECONDS)

    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            poll = poll_device_login(device_code, device_payload, provider)
        except CloudClientError as e:
            raise CliError(f"Login failed: {e.message}") from e
        status = poll.get("status")
        if status == "complete":
            credential_store.set_device_token(poll["device_token"])
            state.device_id = poll["device"]["id"]
            state.device_name = poll["device"]["device_name"]
            state.save()
            print("Device linked successfully.")
            return poll["device_token"]
        if status == "pending":
            if poll.get("slow_down"):
                interval += _DEFAULT_POLL_INTERVAL_SECONDS
            continue
        raise CliError(_POLL_TERMINAL_ERROR_MESSAGES.get(status, poll.get("error") or "Login failed. Please try again."))

    raise CliError("Login timed out. Please try again.")


def _parse_size(value: str) -> float:
    """Accepts the doc's own example format ("8Gi"/"50Gi") or a bare number (GB assumed) -
    Kubernetes-style Mi/Ti suffixes are deliberately not supported, matching every other size input
    in this project (memory_gb/storage_gb are always plain GB floats, e.g. DesktopState)."""
    text = value.strip()
    for suffix in ("Gi", "G", "gi", "g"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return float(text)


def _validate_allocation(cpu: int, memory_gb: float, storage_gb: float, hardware: dict) -> None:
    total_cpu = hardware["total_cpu"]
    total_memory_gb = hardware["total_memory_bytes"] / BYTES_PER_GB
    total_storage_gb = hardware["total_storage_bytes"] / BYTES_PER_GB
    if cpu < 1 or cpu > total_cpu:
        raise CliError(f"--cpus must be between 1 and {total_cpu} (detected)")
    if memory_gb < 1 or memory_gb > total_memory_gb:
        raise CliError(f"--memory must be between 1Gi and {total_memory_gb:.0f}Gi (detected)")
    if storage_gb < 5 or storage_gb > total_storage_gb:
        raise CliError(f"--storage must be between 5Gi and {total_storage_gb:.0f}Gi (detected)")


def _prompt_allocation(hardware: dict) -> tuple[int, float, float]:
    default_cpu, default_memory_gb, default_storage_gb = default_allocation(hardware)
    total_cpu = hardware["total_cpu"]
    total_memory_gb = hardware["total_memory_bytes"] / BYTES_PER_GB
    total_storage_gb = hardware["total_storage_bytes"] / BYTES_PER_GB

    print(f"Detected: {total_cpu} CPUs, {total_memory_gb:.0f}Gi memory, {total_storage_gb:.0f}Gi storage.")
    cpu_in = input(f"CPUs to allocate [{default_cpu}]: ").strip()
    memory_in = input(f"Memory to allocate (Gi) [{default_memory_gb:.0f}Gi]: ").strip()
    storage_in = input(f"Storage to allocate (Gi) [{default_storage_gb:.0f}Gi]: ").strip()

    cpu = int(cpu_in) if cpu_in else default_cpu
    memory_gb = _parse_size(memory_in) if memory_in else default_memory_gb
    storage_gb = _parse_size(storage_in) if storage_in else default_storage_gb
    _validate_allocation(cpu, memory_gb, storage_gb, hardware)
    return cpu, memory_gb, storage_gb


def _sync_allocation_to_cloud(state: DesktopState, credential_store: LinuxFileCredentialStore) -> None:
    token = credential_store.get_device_token()
    if not token or not state.device_id:
        return
    try:
        CloudClient(device_token=token).update_device(state.device_id, {
            "allocated_cpu": state.allocated_cpu,
            "allocated_memory_bytes": int(state.allocated_memory_gb * BYTES_PER_GB),
            "allocated_storage_bytes": int(state.allocated_storage_gb * BYTES_PER_GB),
        })
    except CloudClientError:
        pass  # local allocation already saved regardless - Cloud sync is best-effort, same as the GUI's own Api._save_allocation.


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        native_k3s.require_root()
        local_stack.check_prerequisites()
    except (ClusterError, LocalStackError) as e:
        raise CliError(str(e))

    state = load_state()
    credential_store = LinuxFileCredentialStore()
    token = ensure_login(state, credential_store, provider=args.provider)

    hardware = detect_hardware()
    if args.non_interactive:
        if args.cpus is None or args.memory is None or args.storage is None:
            raise CliError("--non-interactive requires --cpus, --memory, and --storage")
        cpu = args.cpus
        memory_gb = _parse_size(args.memory)
        storage_gb = _parse_size(args.storage)
        _validate_allocation(cpu, memory_gb, storage_gb, hardware)
    else:
        cpu, memory_gb, storage_gb = _prompt_allocation(hardware)

    state.allocated_cpu, state.allocated_memory_gb, state.allocated_storage_gb = cpu, memory_gb, storage_gb
    state.save()
    _sync_allocation_to_cloud(state, credential_store)

    print("Setting up the local cluster...")
    try:
        native_k3s.create_cluster(cpu, memory_gb, storage_gb, on_step=_print_step)
        local_stack.deploy(state.device_id, token, on_step=_print_step)
    except (ClusterError, LocalStackError) as e:
        raise CliError(str(e))

    print("Setup complete.")
    return 0


def cmd_start(_args: argparse.Namespace) -> int:
    try:
        native_k3s.start_service()
    except ClusterError as e:
        raise CliError(str(e))
    print("k3s started.")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    try:
        native_k3s.stop_service()
    except ClusterError as e:
        raise CliError(str(e))
    print("k3s stopped.")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    exists = native_k3s.cluster_exists()
    print(f"Cluster installed: {exists}")
    if not exists:
        return 0
    try:
        pods = native_k3s.list_pods()
    except ClusterError as e:
        raise CliError(str(e))
    if not pods:
        print("No monitored pods found.")
        return 0
    for pod in pods:
        marker = "CRASHING" if pod["crashing"] else pod["phase"]
        print(f"  {pod['namespace']}/{pod['name']}: {marker} (ready {pod['ready']}, restarts {pod['restarts']})")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    try:
        output = native_k3s.pod_logs(args.namespace, args.pod) if args.pod else native_k3s.service_logs()
    except ClusterError as e:
        raise CliError(str(e))
    print(output)
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    state = load_state()
    credential_store = LinuxFileCredentialStore()
    hardware = detect_hardware()
    if args.non_interactive:
        if args.cpus is None or args.memory is None or args.storage is None:
            raise CliError("--non-interactive requires --cpus, --memory, and --storage")
        cpu, memory_gb, storage_gb = args.cpus, _parse_size(args.memory), _parse_size(args.storage)
        _validate_allocation(cpu, memory_gb, storage_gb, hardware)
    else:
        cpu, memory_gb, storage_gb = _prompt_allocation(hardware)
    state.allocated_cpu, state.allocated_memory_gb, state.allocated_storage_gb = cpu, memory_gb, storage_gb
    state.save()
    _sync_allocation_to_cloud(state, credential_store)
    print(f"Allocation updated: {cpu} CPUs, {memory_gb:.0f}Gi memory, {storage_gb:.0f}Gi storage.")
    return 0


def cmd_activate(_args: argparse.Namespace) -> int:
    """Mirrors Api.activate_device (desktop/api.py) - a heartbeat with the existing device token,
    demoting any other of this user's active devices. Part 14's own rule applies here unchanged:
    a real CLI startup may request activation once; this command IS that one request, run
    explicitly (there is no daemon-driven automatic reconnect-triggered activation here the way
    Device Agent's own gRPC stream has for Part 14's other half)."""
    state = load_state()
    credential_store = LinuxFileCredentialStore()
    token = credential_store.get_device_token()
    if not token or not state.device_id:
        raise CliError("No device credential yet - run 'browseterm setup' first.")
    try:
        device = CloudClient(device_token=token).heartbeat(state.device_id)
    except CloudClientError as e:
        raise CliError(e.message)
    print(f"Device '{device.get('device_name')}' is now active.")
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Re-runs cluster creation (idempotent - see native_k3s.create_cluster's own steps) and
    redeploys every local-stack workload (idempotent - local_stack.deploy applies each manifest
    via `kubectl apply`/`--dry-run=client -o yaml | kubectl apply -f -`, never a destructive
    recreate). The safe, repeatable "something's wrong, put it back to a known-good state" action
    - never destroys data, unlike uninstall."""
    state = load_state()
    credential_store = LinuxFileCredentialStore()
    token = credential_store.get_device_token()
    if not token or state.allocated_cpu is None:
        raise CliError("Nothing to repair - run 'browseterm setup' first.")
    try:
        native_k3s.require_root()
        native_k3s.create_cluster(state.allocated_cpu, state.allocated_memory_gb, state.allocated_storage_gb, on_step=_print_step)
        local_stack.deploy(state.device_id, token, on_step=_print_step)
    except (ClusterError, LocalStackError) as e:
        raise CliError(str(e))
    print("Repair complete.")
    return 0


def cmd_diagnostics(_args: argparse.Namespace) -> int:
    """Versions, health, sanitized logs - never device secrets or terminal filesystem content
    (Part 24's own rule, applied here too even though this is a CLI, not the observability stack)."""
    hardware = detect_hardware()
    print(f"OS: {hardware['os']} {hardware['runtime_version']} ({hardware['architecture']})")
    print(f"Cluster installed: {native_k3s.cluster_exists()}")
    state = load_state()
    print(f"Device ID: {state.device_id or '(none)'}")
    credential_store = LinuxFileCredentialStore()
    print(f"Device credential present: {bool(credential_store.get_device_token())}")
    if native_k3s.cluster_exists():
        try:
            for pod in native_k3s.list_pods():
                marker = "CRASHING" if pod["crashing"] else pod["phase"]
                print(f"  {pod['namespace']}/{pod['name']}: {marker}")
        except ClusterError as e:
            print(f"  (could not list pods: {e})")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        native_k3s.require_root()
        native_k3s.delete_cluster()
    except ClusterError as e:
        raise CliError(str(e))
    if args.purge_state:
        state = load_state()
        state.clear()
        LinuxFileCredentialStore().delete_device_token()
        print("Cluster removed, local state purged.")
    else:
        print("Cluster removed. Local state preserved (device credential/allocation) - pass --purge-state to remove it too.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browseterm", description="Browseterm headless Linux CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    setup_p = sub.add_parser("setup", help="Install k3s and deploy the local Browseterm stack")
    setup_p.add_argument("--cpus", type=int)
    setup_p.add_argument("--memory")
    setup_p.add_argument("--storage")
    setup_p.add_argument("--non-interactive", action="store_true")
    setup_p.add_argument("--provider", default="google")
    setup_p.set_defaults(func=cmd_setup)

    sub.add_parser("start", help="Start the k3s service").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="Stop the k3s service").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="Show cluster and pod status").set_defaults(func=cmd_status)

    logs_p = sub.add_parser("logs", help="Show k3s service logs, or a specific pod's logs")
    logs_p.add_argument("pod", nargs="?", default=None)
    logs_p.add_argument("--namespace", default="browseterm")
    logs_p.set_defaults(func=cmd_logs)

    configure_p = sub.add_parser("configure", help="Change the allocated CPU/memory/storage")
    configure_p.add_argument("--cpus", type=int)
    configure_p.add_argument("--memory")
    configure_p.add_argument("--storage")
    configure_p.add_argument("--non-interactive", action="store_true")
    configure_p.set_defaults(func=cmd_configure)

    sub.add_parser("activate", help="Activate this device (heartbeat)").set_defaults(func=cmd_activate)
    sub.add_parser("repair", help="Re-apply the cluster and local-stack deployment").set_defaults(func=cmd_repair)
    sub.add_parser("diagnostics", help="Print a sanitized diagnostics summary").set_defaults(func=cmd_diagnostics)

    uninstall_p = sub.add_parser("uninstall", help="Remove k3s")
    uninstall_p.add_argument("--purge-state", action="store_true")
    uninstall_p.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ClusterError) as e:
        # ClusterError also catches unwrapped native_k3s.require_root()/create_cluster()/etc
        # failures that a subcommand didn't itself re-raise as CliError - same "clean message, no
        # traceback" contract either way (LocalStackError is a ClusterError subclass already).
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

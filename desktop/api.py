"""
The JS <-> Python bridge exposed to the app shell (desktop/web/app.html) as
`window.pywebview.api`. Holds no window reference itself.

P07: device bootstrap (trading a fresh WebView session cookie for the long-lived device token)
happens automatically in DesktopApp right after login (desktop/app.py) - by the time app.html is
showing, a device token normally already exists in Keychain. `activate_device` here is the
lighter-weight "re-activate this device" action (a heartbeat with the existing token, demoting
any other of this user's active devices) - it deliberately does NOT re-run bootstrap, since that
needs a live WebView session this shell no longer has once swapped away from it. If the token is
genuinely missing/invalid, the answer is "log out and log back in", not a hidden second bootstrap
path here (p07.md: "do not unnecessarily expand P07 into device-management UI").
"""
import webbrowser
from typing import Any, Callable, Optional

from desktop import cluster_manager, local_stack
from desktop.cloud_client import CloudClient, CloudClientError
from desktop.cluster_manager import ClusterError
from desktop.device_info import BYTES_PER_GB, detect_hardware
from desktop.keychain import KeychainStorage
from desktop.state import DesktopState


def _device_status(device: Optional[dict]) -> str:
    if device is None:
        return "not_registered"
    return device.get("status", "unknown").lower()


class Api:
    """`on_logout` is called after local/Keychain state is cleared, so `DesktopApp` can swap the
    window back to the login page. `on_retry_login` backs the "Retry" button on the
    connection-error page (desktop/app.py) shown when Local can't be reached. `on_start_login`
    backs the "Log in with Google"/"Log in with GitHub" buttons on the login-start page (see
    desktop/app.py's module docstring) - this just kicks off the device-grant flow for whichever
    provider was clicked; `Api` itself holds no window/browser reference of its own, same as the
    other two callbacks."""

    def __init__(
        self, state: DesktopState, keychain: KeychainStorage,
        on_logout: Callable[[], None], on_retry_login: Callable[[], None],
        on_start_login: Callable[[str], None],
    ):
        self._state = state
        self._keychain = keychain
        self._on_logout = on_logout
        self._on_retry_login = on_retry_login
        self._on_start_login = on_start_login

    def device_info(self) -> dict[str, Any]:
        hardware = detect_hardware()
        device = None
        error = None
        token = self._keychain.get_device_token()
        if token and self._state.device_id:
            try:
                client = CloudClient(device_token=token)
                device = client.get_device(self._state.device_id)
            except CloudClientError as e:
                if e.status_code == 404:
                    self._state.device_id = None
                    self._state.save()
                elif not e.is_auth_failure:
                    error = e.message
                # an auth failure (401) here just means "not activated yet" - not an error to
                # surface, the Activate button / a fresh login handles it.
        return {"hardware": hardware, "device": device, "status": _device_status(device), "error": error}

    def cluster_status(self) -> dict[str, Any]:
        """Current allocation (defaulting on first-ever read, see
        `DesktopState.ensure_allocation_defaults`), the detected totals it's bounded by, whether
        the local k3d cluster is actually up (checked live, never cached), and its pods if so."""
        hardware = detect_hardware()
        self._state.ensure_allocation_defaults(hardware)
        try:
            exists = cluster_manager.cluster_exists()
            error = None
        except ClusterError as e:
            exists = False
            error = str(e)
        return {
            "allocated_cpu": self._state.allocated_cpu,
            "allocated_memory_gb": self._state.allocated_memory_gb,
            "allocated_storage_gb": self._state.allocated_storage_gb,
            "total_cpu": hardware["total_cpu"],
            "total_memory_gb": round(hardware["total_memory_bytes"] / BYTES_PER_GB),
            "total_storage_gb": round(hardware["total_storage_bytes"] / BYTES_PER_GB),
            "cluster_exists": exists,
            "error": error,
        }

    def _save_allocation(self, cpu: int, memory_gb: float, storage_gb: float) -> None:
        self._state.allocated_cpu = cpu
        self._state.allocated_memory_gb = memory_gb
        self._state.allocated_storage_gb = storage_gb
        self._state.save()
        token = self._keychain.get_device_token()
        if not token or not self._state.device_id:
            return
        try:
            CloudClient(device_token=token).update_device(self._state.device_id, {
                "allocated_cpu": cpu,
                "allocated_memory_bytes": int(memory_gb * BYTES_PER_GB),
                "allocated_storage_bytes": int(storage_gb * BYTES_PER_GB),
            })
        except CloudClientError:
            pass  # local allocation is saved regardless -- Cloud sync is best-effort here.

    def setup_cluster(self, cpu: int, memory_gb: float, storage_gb: float) -> dict[str, Any]:
        self._save_allocation(cpu, memory_gb, storage_gb)
        try:
            # Checked before the cluster is even created: a doomed config (missing internal API
            # token, missing /etc/hosts entries) fails in milliseconds, not after a ~90s
            # cluster-create cycle only to fail deploying the actual workloads onto it.
            local_stack.check_prerequisites()
            cluster_manager.create_cluster(cpu, memory_gb)
            local_stack.deploy(self._state.device_id)
        except ClusterError as e:
            status = self.cluster_status()
            status["error"] = str(e)
            return status
        return self.cluster_status()

    def teardown_cluster(self) -> dict[str, Any]:
        try:
            cluster_manager.delete_cluster()
        except ClusterError as e:
            status = self.cluster_status()
            status["error"] = str(e)
            return status
        return self.cluster_status()

    def open_browser(self) -> None:
        '''Opens the real web UI (the same Ingress host local_stack.py deploys
        browseterm-server-local behind) in the system browser - a shortcut next to Teardown so
        the user doesn't have to remember/type browseterm.local.com themselves once Setup has
        actually finished. Only shown by the UI once cluster_exists is true (desktop/web/static/
        js/app.js), but this method itself doesn't re-check that - opening a URL that isn't up
        yet just fails in the browser tab, same as typing it in by hand would.'''
        webbrowser.open(f"http://{local_stack.INGRESS_HOST}")

    def list_cluster_pods(self) -> dict[str, Any]:
        try:
            return {"pods": cluster_manager.list_pods(), "error": None}
        except ClusterError as e:
            return {"pods": [], "error": str(e)}

    def restart_workload_pod(self, namespace: str, name: str) -> dict[str, Any]:
        '''Deletes one monitored pod so its Deployment's ReplicaSet recreates it - the pod table's
        per-row Restart button. Returns the same shape as list_cluster_pods so the UI can just
        re-render from the response instead of making a second round trip.'''
        try:
            cluster_manager.restart_pod(namespace, name)
        except ClusterError as e:
            result = self.list_cluster_pods()
            result["error"] = str(e)
            return result
        return self.list_cluster_pods()

    def activate_device(self) -> dict[str, Any]:
        token = self._keychain.get_device_token()
        if not token or not self._state.device_id:
            return {
                "hardware": detect_hardware(), "device": None, "status": "not_registered",
                "error": "No device credential yet -- please log out and log back in to activate this device.",
            }
        try:
            client = CloudClient(device_token=token)
            device = client.heartbeat(self._state.device_id)
        except CloudClientError as e:
            return {"hardware": detect_hardware(), "device": None, "status": "unknown", "error": e.message}
        return {"hardware": detect_hardware(), "device": device, "status": _device_status(device), "error": None}

    def logout(self) -> None:
        self._on_logout()

    def retry_login(self) -> None:
        self._on_retry_login()

    def start_login(self, provider: str = "google") -> None:
        self._on_start_login(provider)

"""
Headless background daemon: no pywebview window, just two loops that keep this device's
connection to Cloud alive - "something that keeps running and makes sure the k3s Device Agent
keeps the connection active" (the owner's own framing). Meant to run under macOS launchd (see
`packaging/com.browseterm.daemon.plist`, installed separately - this module itself never touches
launchd), started at login and kept alive independently of whether the GUI app (`main.py`) is
open at all.

Two responsibilities, deliberately split from the GUI app rather than duplicated by it:

1. Device heartbeat - moved here from `desktop/app.py` (which used to run its own
   `_heartbeat_loop` in a background thread whenever the window was open). Two processes
   heartbeating the same device independently would just be racing each other for no benefit, so
   the GUI no longer heartbeats at all - this daemon is now the one and only heartbeat owner.
   `desktop/app.py` still handles "am I logged in" the same way it always did (a valid Keychain
   token), so the GUI keeps working correctly whether or not this daemon happens to be running -
   it just means heartbeats stop happening if it isn't.

2. Cluster health check - Device Agent's own gRPC stream already reconnects on its own with
   capped exponential backoff once its pod is actually running again (migration Part 7) - that
   part needs nothing from this daemon. What's genuinely missing without this daemon: nothing
   brings the Multipass VM itself back after the Mac sleeps and multipass stops it, and nothing
   notices a monitored pod stuck crash-looping until a human happens to open the GUI and look at
   the pod table. This loop polls `cluster_manager.vm_state()` and starts the VM back up if it's
   not Running, then `cluster_manager.list_pods()`/`restart_pod()` for anything actually crashing
   (never touches a pod that's merely still starting up - see `_is_crashing`'s own reason list).

Both loops are no-ops until there's actually something to do: no stored device credential yet, or
no cluster ever set up (`cluster_manager.cluster_exists()` is False) - this daemon does not
perform first-ever Setup itself, that stays a deliberate GUI action (the user has to choose a
resource allocation first; see `desktop/api.py::Api.setup_cluster`). It only keeps an
already-set-up cluster and an already-logged-in device alive.
"""
import logging
import signal
import threading
import time

from desktop import cluster_manager
from desktop.cloud_client import CloudClient, CloudClientError
from desktop.cluster_manager import ClusterError
from desktop.config import DAEMON_HEALTH_CHECK_INTERVAL_SECONDS, DEVICE_HEARTBEAT_INTERVAL_SECONDS
from desktop.keychain import KeychainStorage
from desktop.state import load_state

logger = logging.getLogger("browseterm.daemon")


class Daemon:
    def __init__(self) -> None:
        self._keychain = KeychainStorage()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def run(self) -> None:
        logger.info("browseterm daemon starting")
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._threads = [
            threading.Thread(target=self._heartbeat_loop, daemon=True),
            threading.Thread(target=self._health_check_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

        # The two loops do the actual work on their own schedules - this just blocks the main
        # thread until a signal sets `_stop`, so launchd sees this process stay alive the whole
        # time rather than exiting immediately after spawning daemon threads.
        self._stop.wait()
        logger.info("browseterm daemon stopping")

    def _handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("received signal %s, shutting down", signum)
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(DEVICE_HEARTBEAT_INTERVAL_SECONDS):
            self._heartbeat_once()

    def _heartbeat_once(self) -> None:
        state = load_state()
        token = self._keychain.get_device_token()
        if not token or not state.device_id:
            return  # not logged in yet (or logged out) - nothing to heartbeat
        try:
            CloudClient(device_token=token).heartbeat(state.device_id)
        except CloudClientError as e:
            if e.is_auth_failure:
                # Credential is dead (revoked/expired) - clear it so the next heartbeat tick
                # doesn't keep retrying a call that can only ever fail, and so the GUI's own
                # `_device_token_is_valid()` check correctly falls back to the login page next
                # time it's opened. No window to redirect here (headless) - that's fine, the GUI
                # re-checks Keychain fresh on every launch regardless of whether this daemon ran.
                logger.warning("device credential rejected by Cloud, clearing it")
                self._keychain.delete_device_token()
                state.clear()
            # else: transient/network failure - try again next interval, same as the GUI's old
            # heartbeat loop already did.

    def _health_check_loop(self) -> None:
        while not self._stop.wait(DAEMON_HEALTH_CHECK_INTERVAL_SECONDS):
            self._health_check_once()

    def _health_check_once(self) -> None:
        state = load_state()
        if not self._keychain.get_device_token() or not state.device_id:
            return  # not logged in - no device credential to have set a cluster up with
        try:
            if not cluster_manager.cluster_exists():
                return  # no cluster ever set up - this daemon doesn't create one itself
            state_str = cluster_manager.vm_state()
            if state_str not in ("Running", "Missing"):
                logger.warning("VM state is %s, starting it back up", state_str)
                cluster_manager.start_vm()
                return  # give pods a moment to come back up before checking them below
            for pod in cluster_manager.list_pods():
                if pod["crashing"]:
                    logger.warning("restarting crashing pod %s/%s", pod["namespace"], pod["name"])
                    cluster_manager.restart_pod(pod["namespace"], pod["name"])
        except ClusterError as e:
            logger.warning("health check failed: %s", e)  # try again next interval


def run_daemon() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Daemon().run()

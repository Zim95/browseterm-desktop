"""
Browseterm Desktop Resource MVP - macOS menu-bar app (P06). Mac-only, deliberately no
cross-platform framework (Electron/Tauri/etc) - see README.md for the technology decision.

Run: `poetry run python -m desktop.app`

Scope (p06.md "DESKTOP RESPONSIBILITIES" / plan section 8): detect macOS/arch/CPU/memory/
storage, show/edit Browseterm allocation, register/update this device + heartbeat through the
Cloud Device API, report local runtime health, open Browseterm in the system browser. It does
NOT list/create/render workspaces or terminals - that stays in the browser.
"""
import webbrowser

import rumps

from desktop.allocation import Allocation, AllocationValidationError
from desktop.config import (
    BROWSETERM_LOCAL_URL,
    BROWSETERM_SESSION_COOKIE,
    DEFAULT_DEVICE_NAME,
)
from desktop.device_registration import register_or_update_device, send_heartbeat
from desktop.hardware import detect_host_resources
from desktop.runtime_health import check_local_k3s_health, check_local_server_health
from src.cloud_client.client import CloudClient, CloudClientError

GIBIBYTE = 1024**3


class BrowsetermDesktopApp(rumps.App):
    def __init__(self):
        super().__init__("Browseterm", quit_button="Quit")
        self.host = None
        self.allocation = None
        self.device_id = None
        self.client = CloudClient(session_cookie=BROWSETERM_SESSION_COOKIE)
        self.menu = [
            "Detect Hardware",
            "Set Allocation",
            "Register / Update Device",
            "Heartbeat Now",
            None,
            "Check Runtime Health",
            None,
            "Open Browseterm",
        ]

    @rumps.clicked("Detect Hardware")
    def detect_hardware(self, _):
        try:
            self.host = detect_host_resources()
        except Exception as e:
            rumps.alert("Hardware detection failed", str(e))
            return
        rumps.alert(
            "Host detected",
            f"{self.host.os_name} {self.host.os_version} ({self.host.architecture})\n"
            f"CPU: {self.host.total_cpu}\n"
            f"Memory: {self.host.total_memory_bytes / GIBIBYTE:.1f} GB\n"
            f"Storage: {self.host.total_storage_bytes / GIBIBYTE:.1f} GB",
        )

    @rumps.clicked("Set Allocation")
    def set_allocation(self, _):
        if self.host is None:
            rumps.alert("Detect hardware first", 'Run "Detect Hardware" before setting an allocation.')
            return

        cpu_default = str(self.allocation.allocated_cpu if self.allocation else self.host.total_cpu // 2)
        cpu_resp = rumps.Window(f"Allocated CPU cores (host has {self.host.total_cpu})", default_text=cpu_default).run()
        if not cpu_resp.clicked:
            return

        mem_default_gb = str(
            round((self.allocation.allocated_memory_bytes if self.allocation else self.host.total_memory_bytes // 2) / GIBIBYTE, 1)
        )
        mem_resp = rumps.Window(
            f"Allocated memory in GB (host has {self.host.total_memory_bytes / GIBIBYTE:.1f} GB)",
            default_text=mem_default_gb,
        ).run()
        if not mem_resp.clicked:
            return

        storage_default_gb = str(
            round((self.allocation.allocated_storage_bytes if self.allocation else self.host.total_storage_bytes // 4) / GIBIBYTE, 1)
        )
        storage_resp = rumps.Window(
            f"Allocated storage in GB (host has {self.host.total_storage_bytes / GIBIBYTE:.1f} GB)",
            default_text=storage_default_gb,
        ).run()
        if not storage_resp.clicked:
            return

        try:
            self.allocation = Allocation(
                allocated_cpu=int(cpu_resp.text),
                allocated_memory_bytes=int(float(mem_resp.text) * GIBIBYTE),
                allocated_storage_bytes=int(float(storage_resp.text) * GIBIBYTE),
            )
        except ValueError:
            rumps.alert("Invalid input", "CPU/memory/storage must be numbers.")
            return

        rumps.alert("Allocation set", "Use \"Register / Update Device\" to save it to Cloud.")

    @rumps.clicked("Register / Update Device")
    def register_device(self, _):
        if self.host is None or self.allocation is None:
            rumps.alert("Missing info", 'Run "Detect Hardware" and "Set Allocation" first.')
            return
        if not BROWSETERM_SESSION_COOKIE:
            rumps.alert(
                "Not signed in",
                f"Log in via the browser at {BROWSETERM_LOCAL_URL}, then set the "
                "BROWSETERM_SESSION_COOKIE environment variable to your session cookie value "
                "and restart Browseterm Desktop. (Interim P06 auth - see README.md.)",
            )
            return
        try:
            device = register_or_update_device(self.client, self.host, self.allocation, DEFAULT_DEVICE_NAME)
        except (CloudClientError, AllocationValidationError) as e:
            rumps.alert("Registration failed", str(e))
            return
        self.device_id = device["id"]
        rumps.alert("Device registered", f"Device id: {self.device_id}")

    @rumps.clicked("Heartbeat Now")
    def heartbeat(self, _):
        if not self.device_id:
            rumps.alert("Not registered", 'Run "Register / Update Device" first.')
            return
        try:
            send_heartbeat(self.client, self.device_id)
        except CloudClientError as e:
            rumps.alert("Heartbeat failed", str(e))
            return
        rumps.alert("Heartbeat sent", "")

    @rumps.clicked("Check Runtime Health")
    def runtime_health(self, _):
        server_ok = check_local_server_health(BROWSETERM_LOCAL_URL)
        k3s_ok = check_local_k3s_health()
        rumps.alert(
            "Runtime health",
            f"Local browseterm-server: {'reachable' if server_ok else 'unreachable'}\n"
            f"Local k3s: {'healthy' if k3s_ok else 'unreachable/kubectl not found'}",
        )

    @rumps.clicked("Open Browseterm")
    def open_browseterm(self, _):
        webbrowser.open(BROWSETERM_LOCAL_URL)


if __name__ == "__main__":
    BrowsetermDesktopApp().run()

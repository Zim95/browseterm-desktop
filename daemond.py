"""
BrowseTerm background daemon - no window, just the heartbeat + cluster health-check loops.

Run directly: python daemond.py
Run under launchd: see packaging/com.browseterm.daemon.plist and README.md's "Daemon" section
for the exact install/uninstall commands - this script itself never touches launchd.
"""
from desktop.daemon import run_daemon

if __name__ == "__main__":
    run_daemon()

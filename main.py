"""
Browseterm Desktop.

Run: python main.py

Opens the BrowseTerm desktop window: an OAuth Device Authorization Grant login page (Google or
GitHub) against Cloud directly if not already logged in, then the Device page. See desktop/app.py
for how the window is driven and desktop/config.py for the Cloud URL/tokens it needs.
"""
from desktop.app import run

if __name__ == "__main__":
    run()

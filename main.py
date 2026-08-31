"""
Browseterm Desktop.

Run: python main.py

Opens the BrowseTerm desktop window: the real browseterm-server-local login page (Google/GitHub
OAuth) if not already logged in, then the Device page. See desktop/app.py for how the window is
driven and desktop/config.py for the Local/Cloud URLs it talks to.
"""
from desktop.app import run

if __name__ == "__main__":
    run()

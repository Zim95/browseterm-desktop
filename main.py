"""
Browseterm Desktop - hello world.

Run: python main.py
Pops up a native macOS alert dialog saying "Hello World". That's it - this is the reset
baseline the real Desktop app (auth, hardware detection, resource allocation, device
registration) gets rebuilt on top of, incrementally.
"""
import rumps

if __name__ == "__main__":
    rumps.alert(title="Browseterm", message="Hello World")

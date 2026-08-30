# browseterm-desktop

Reset to a hello-world baseline. Mac-only.

## Run it

```
poetry install
poetry run python main.py
```

Pops up a native macOS alert dialog saying "Hello World". That's it.

This is the starting point for rebuilding the real Desktop app (auth, hardware detection,
CPU/memory/storage allocation, device registration against Cloud's Device API) incrementally -
see `browseterm-server-local`'s README for the Cloud API surface this app will eventually talk
to via a `CloudClient`.

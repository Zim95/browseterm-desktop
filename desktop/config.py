"""
Desktop-only configuration. Deliberately never imports `src.common.config`
(POSTGRES_*/REDIS_*) - Desktop must never possess Cloud DB/Redis credentials (p06.md, "LOCAL
REPO FORBIDDEN DEPENDENCIES" / "Do not copy Cloud credentials into Desktop config").
"""
import os
import platform

# Matches the actual authoritative hostname already used by AUTH_REDIRECT_BASE_URI/INGRESS_HOST
# in browseterm-monorepo/env.mk.example and infra/development|deployment's Ingress `host` -
# not invented from memory.
BROWSETERM_LOCAL_URL: str = os.getenv("BROWSETERM_LOCAL_URL", "http://browseterm.local.com:9999")

# Interim pre-P07 auth - see src/cloud_client/client.py's module docstring for the full
# rationale. The user obtains this by logging in via the browser at BROWSETERM_LOCAL_URL and
# copying the `session` cookie value; replaced by P07's real device-scoped credential flow.
BROWSETERM_SESSION_COOKIE: str | None = os.getenv("BROWSETERM_SESSION_COOKIE")

# Not the local k3s/runtime stack's own version (no such versioning scheme exists yet anywhere
# in these repos) - this is the Desktop MVP application's own version.
DESKTOP_APP_VERSION: str = "0.1.0"

DEFAULT_DEVICE_NAME: str = os.getenv("BROWSETERM_DEVICE_NAME") or platform.node()

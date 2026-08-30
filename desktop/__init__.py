"""
Browseterm Desktop Resource MVP (P06). Mac-only.

Machine/runtime/resource manager - NOT the Browseterm workspace UI. See README.md's
"Desktop Resource MVP" section for the full scope statement and what this deliberately does
not do (no workspace list/create/terminal UI, no container-maker duplication).

Talks to Cloud exclusively through `src.cloud_client.CloudClient` - never imports
`browseterm_db`, `src.common.config.DB_CONFIG`, or any POSTGRES_*/REDIS_* setting.
"""

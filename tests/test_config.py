"""
`desktop/config.py`'s local-file fallback for BROWSETERM_CLOUD_INTERNAL_API_TOKEN -- the module
constant itself is computed once at import time (env var, else the local file), so what's tested
here is the underlying `_read_local_cloud_internal_api_token` function in isolation rather than
re-importing the module under different env/filesystem states.
"""
from desktop.config import _read_local_cloud_internal_api_token


def test_reads_and_strips_token_from_file(tmp_path):
    token_file = tmp_path / "cloud_internal_api_token"
    token_file.write_text("abc123\n")
    assert _read_local_cloud_internal_api_token(str(token_file)) == "abc123"


def test_returns_empty_string_when_file_does_not_exist(tmp_path):
    assert _read_local_cloud_internal_api_token(str(tmp_path / "does-not-exist")) == ""

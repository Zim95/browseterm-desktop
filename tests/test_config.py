"""
`desktop/config.py`'s `env.mk` loader, used as a fallback (behind an env var override) for every
secret this app needs (BROWSETERM_CLOUD_INTERNAL_API_TOKEN, DOCKER_HUB_REPO_PASSWORD,
NGROK_AUTHTOKEN, ...) -- the module constants themselves are computed once at import time, so
what's tested here is the underlying `_load_env_mk` parser in isolation rather than re-importing
the module under different env/filesystem states.
"""
from desktop.config import _load_env_mk


def test_parses_key_value_pairs(tmp_path):
    env_mk = tmp_path / "env.mk"
    env_mk.write_text("FOO=bar\nBAZ=qux\n")
    assert _load_env_mk(str(env_mk)) == {"FOO": "bar", "BAZ": "qux"}


def test_skips_comments_and_blank_lines(tmp_path):
    env_mk = tmp_path / "env.mk"
    env_mk.write_text("# a comment\n\nFOO=bar\n   \n# another\nBAZ=qux\n")
    assert _load_env_mk(str(env_mk)) == {"FOO": "bar", "BAZ": "qux"}


def test_strips_surrounding_whitespace(tmp_path):
    env_mk = tmp_path / "env.mk"
    env_mk.write_text("  FOO = bar  \n")
    assert _load_env_mk(str(env_mk)) == {"FOO": "bar"}


def test_returns_empty_dict_when_file_does_not_exist(tmp_path):
    assert _load_env_mk(str(tmp_path / "does-not-exist")) == {}

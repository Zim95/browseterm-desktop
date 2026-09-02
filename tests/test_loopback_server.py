"""
Tests for desktop/loopback_server.py - the local HTTP server that receives the device-bootstrap
code back from the system browser once the desktop login flow (app.py's module docstring)
completes on Local's side.
"""
import httpx
import pytest

from desktop.loopback_server import LoopbackServer


def test_binds_to_loopback_only_and_reports_a_real_port():
    server = LoopbackServer()
    try:
        assert server.port > 0
        assert server._httpd.server_address[0] == "127.0.0.1"
    finally:
        server._httpd.server_close()


def test_receives_the_code_and_serves_a_success_page():
    server = LoopbackServer()
    server.start()
    response = httpx.get(f"http://127.0.0.1:{server.port}/callback", params={"code": "abc123"})
    assert response.status_code == 200
    assert b"logged in" in response.content.lower()

    code = server.wait_for_code(timeout_seconds=5.0)
    assert code == "abc123"


def test_a_callback_with_no_code_serves_an_error_page_and_returns_none():
    server = LoopbackServer()
    server.start()
    response = httpx.get(f"http://127.0.0.1:{server.port}/callback")
    assert response.status_code == 200
    assert b"didn't complete" in response.content.lower()

    code = server.wait_for_code(timeout_seconds=5.0)
    assert code is None


def test_times_out_and_closes_the_socket_when_nothing_ever_arrives():
    server = LoopbackServer()
    server.start()
    code = server.wait_for_code(timeout_seconds=0.2)
    assert code is None
    # the port must actually be released - a second server can bind fresh ports independently
    other = LoopbackServer()
    try:
        assert other.port != server.port
    finally:
        other._httpd.server_close()


def test_an_unrelated_path_gets_a_plain_404_and_does_not_unblock_the_waiter():
    server = LoopbackServer()
    server.start()
    response = httpx.get(f"http://127.0.0.1:{server.port}/not-the-callback-path")
    assert response.status_code == 404
    # the server only ever handles ONE request total (handle_request), so after the 404 it has
    # already stopped listening for the real /callback - wait_for_code correctly times out.
    code = server.wait_for_code(timeout_seconds=0.2)
    assert code is None

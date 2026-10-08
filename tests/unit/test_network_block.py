import socket

import httpx
import pytest

from tests.conftest import NetworkBlockedError


def test_socket_connect_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


def test_httpx_request_fails_without_network() -> None:
    with pytest.raises(httpx.HTTPError):
        httpx.get("https://example.com", timeout=1)

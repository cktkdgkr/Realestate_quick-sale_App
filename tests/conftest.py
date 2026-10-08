"""공용 pytest 설정.

- 모든 테스트에서 외부 네트워크 접속을 막는다 (CLAUDE.md §8, 검증 C0-2).
  외부 호출이 필요한 코드는 respx 등으로 mock 해야 한다.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class NetworkBlockedError(OSError):
    """OSError 하위 클래스라 httpx에서는 ConnectError로 보인다 (실제 접속 실패와 같은 경로)."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkBlockedError(f"테스트에서 외부 네트워크 접속 금지: {address!r}")
        return real_connect(self, address)

    def blocked_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NetworkBlockedError(f"테스트에서 외부 네트워크 접속 금지: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "history.sqlite3"

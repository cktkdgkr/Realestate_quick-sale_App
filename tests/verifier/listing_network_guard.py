"""검증용: test_default_client_without_network_fails_loudly 가 실제 외부 연결을 시도하는지 확인.
socket 연결을 가로채 기록만 하고 실패시킨다 (외부로 나가지 않음).
실행: .venv/bin/python tests/verifier/listing_network_guard.py
"""
import socket, sys
sys.path.insert(0, ".")
attempts = []
_orig = socket.socket.connect
def guard(self, addr):
    attempts.append(addr)
    raise OSError("verifier: network blocked")
socket.socket.connect = guard
_orig_gai = socket.getaddrinfo
def gai(host, *a, **k):
    attempts.append(("getaddrinfo", host))
    raise socket.gaierror("verifier: dns blocked")
socket.getaddrinfo = gai

from tests.unit import test_naver_listings as t
t.test_default_client_without_network_fails_loudly()
print("test passed under guard; connection attempts recorded:", attempts)

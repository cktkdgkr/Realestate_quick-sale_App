"""네이버 실거래(교차검증용) 수집 테스트. fixture는 합성(synthetic)이다."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.collectors.errors import CollectorError
from app.collectors.naver_listings import NaverClient
from app.collectors.naver_trades import fetch_naver_trades, fetch_naver_trades_or_warning
from app.domain.models import AreaType, Complex

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "naver"
CX = Complex("3009", "합성잠원", "11650", "서울시 서초구 잠원동 60-1", 15, "11650-100")
AREAS = [AreaType("3009", 59.99, 59.99, 80.5, 24, "80"), AreaType("3009", 84.97, 84.97, 112.4, 34, "112")]


def _load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeNaver:
    def __init__(self, real: dict[str, str] | None = None, status: int = 200) -> None:
        self.real = real if real is not None else {
            "1": _load("trades_real_3009_area1_synthetic.json"),
            "2": _load("trades_real_3009_area2_synthetic.json"),
        }
        self.status = status
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="")
        hdr = {"content-type": "application/json"}
        if request.url.path == "/api/complexes/3009":
            return httpx.Response(200, text=_load("trades_complex_3009_synthetic.json"), headers=hdr)
        if request.url.path == "/api/complexes/3009/prices/real":
            return httpx.Response(200, text=self.real[request.url.params["areaNo"]], headers=hdr)
        return httpx.Response(404)


def _client(fake: FakeNaver, sleeps: list[float] | None = None) -> NaverClient:
    rec = sleeps if sleeps is not None else []
    return NaverClient(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=rec.append, rng=lambda: 0.5)


def test_fetch_naver_trades_parses_and_maps_area() -> None:
    fake = FakeNaver()
    sleeps: list[float] = []
    client = _client(fake, sleeps)
    trades = fetch_naver_trades(client, CX, AREAS)

    params = [(r.url.path, r.url.params.get("areaNo"), r.url.params.get("tradeType")) for r in fake.calls]
    assert params == [("/api/complexes/3009", None, None),
                      ("/api/complexes/3009/prices/real", "1", "A1"),
                      ("/api/complexes/3009/prices/real", "2", "A1")]
    assert len(sleeps) == 2 and all(2.0 <= s <= 5.0 for s in sleeps)  # 순차·대기 (NaverClient)

    assert all(t.source == "NAVER" for t in trades)
    big = [t for t in trades if t.area_key == 84.97]
    assert len(big) == 5
    c = next(t for t in big if t.contract_date == date(2026, 9, 10))
    assert c.cancelled is True and c.price == 238000 and c.floor_group == "NORMAL"
    small = [t for t in trades if t.area_key == 59.99]
    # "B1" 층은 숫자가 아니라 제외+경고, tradeType B1(전세)은 조용히 제외
    assert [(t.floor, t.floor_group, t.price) for t in small] == [(2, "LOW", 170000)]
    assert any("층 값 오류 1건" in w for w in client.warnings)


def test_fetch_naver_trades_empty_area_types_no_request() -> None:
    fake = FakeNaver()
    assert fetch_naver_trades(_client(fake), CX, []) == []
    assert fake.calls == []


def test_schema_changed_raises() -> None:
    fake = FakeNaver(real={"1": _load("trades_real_schema_changed_synthetic.json"), "2": "{}"})
    with pytest.raises(CollectorError) as ei:
        fetch_naver_trades(_client(fake), CX, AREAS)
    assert ei.value.stage == "schema_changed"


def test_unknown_type_name_raises_schema_changed() -> None:
    areas = [AreaType("3009", 84.97, 84.97, 112.4, 34, "112X")]
    with pytest.raises(CollectorError) as ei:
        fetch_naver_trades(_client(FakeNaver()), CX, areas)
    assert ei.value.stage == "schema_changed"


def test_blocked_raises_and_or_warning_wrapper_keeps_going() -> None:
    fake = FakeNaver(status=403)
    client = _client(fake)
    with pytest.raises(CollectorError) as ei:
        fetch_naver_trades(client, CX, AREAS)
    assert ei.value.stage == "blocked"

    trades, warns = fetch_naver_trades_or_warning(_client(FakeNaver(status=403)), CX, AREAS)
    assert trades is None  # 실패는 빈 목록이 아니라 None
    assert len(warns) == 1 and "3009" in warns[0] and "blocked" in warns[0] and "교차검증 생략" in warns[0]


def test_or_warning_success_passthrough() -> None:
    trades, warns = fetch_naver_trades_or_warning(_client(FakeNaver()), CX, AREAS)
    assert trades and warns == []


def test_naver_trade_fixtures_marked_synthetic() -> None:
    for p in FIX.glob("trades_*.json"):
        assert json.loads(p.read_text(encoding="utf-8"))["_meta"]["synthetic"] is True, p.name

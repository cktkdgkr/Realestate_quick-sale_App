"""naver_listings.py 테스트. 합성 fixture(tests/fixtures/naver/)와 httpx.MockTransport만 쓴다."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.collectors.errors import CollectorError
from app.collectors.naver_listings import (
    BACKOFF_S,
    TIMEOUT_S,
    NaverClient,
    fetch_complex,
    fetch_listings,
)
from app.domain.dedup import dedup

NAVER = Path(__file__).resolve().parents[1] / "fixtures" / "naver"
CNO = "99901"


def load(name: str) -> dict:
    return json.loads((NAVER / name).read_text(encoding="utf-8"))


def json_resp(obj, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=obj)


class Recorder:
    """요청 기록 + 경로·페이지별 응답. 응답이 callable이면 호출한다."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path
        page = request.url.params.get("page")
        r = self.routes.get((key, page), self.routes.get(key))
        if r is None:
            return httpx.Response(404)
        if isinstance(r, list):
            r = r.pop(0)
        return r(request) if callable(r) else r


def make_client(routes: dict, rng=lambda: 0.5):
    rec = Recorder(routes)
    sleeps: list[float] = []
    client = NaverClient(http=httpx.Client(transport=httpx.MockTransport(rec)), sleep=sleeps.append, rng=rng)
    return client, rec, sleeps


COMPLEX_PATH = f"/api/complexes/{CNO}"
ART_PATH = f"/api/articles/complex/{CNO}"


def full_routes() -> dict:
    return {
        COMPLEX_PATH: json_resp(load("complex_20261008.json")),
        (ART_PATH, "1"): json_resp(load("articles_p1_20261008.json")),
        (ART_PATH, "2"): json_resp(load("articles_p2_20261008.json")),
        (ART_PATH, "3"): json_resp(load("articles_p3_20261008.json")),
    }


# ---------------------------------------------------------------- fixture 자체
def test_fixtures_marked_synthetic_and_no_personal_info():
    for p in NAVER.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["_meta"]["synthetic"] is True, p.name
        assert data["_meta"]["created"] == "2026-10-08"
        text = p.read_text(encoding="utf-8")
        for banned in ("realtorName", "representativeTelNo", "cellPhoneNo", "@"):
            assert banned not in text, (p.name, banned)
    html = (NAVER / "captcha_20261008.html").read_text(encoding="utf-8")
    assert "synthetic=true" in html


# ---------------------------------------------------------------- 단지·평형 (C3-2)
def test_fetch_complex_filters_supply_119():
    client, rec, _ = make_client(full_routes())
    cx, types = fetch_complex(client, CNO)
    assert cx.complex_no == CNO and cx.name == "합성테스트아파트"
    assert cx.lawd_cd == "11650" and cx.max_floor == 25 and cx.molit_apt_seq is None
    by_name = {t.type_name: t for t in types}
    # 이하 평형은 빠짐없이 (119.0 경계 포함), 초과(119.01, 145.2)는 제외
    assert set(by_name) == {"79", "112A", "113B", "119"}
    assert [t.area_key for t in types] == [59.99, 84.03, 84.97, 94.5]
    t = by_name["112A"]
    assert (t.exclusive_m2, t.supply_m2, t.pyeong, t.complex_no) == (84.97, 112.4, 34, CNO)
    assert by_name["119"].pyeong == 36
    req = rec.requests[0]
    assert req.url.host == "new.land.naver.com"
    assert req.url.params["sameAddressGroup"] == "false"
    assert req.headers["Referer"].startswith("https://new.land.naver.com")


def test_fetch_complex_missing_field_is_schema_changed():
    data = load("complex_20261008.json")
    del data["complexPyeongDetailList"][0]["exclusiveArea"]
    client, _, _ = make_client({COMPLEX_PATH: json_resp(data)})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "schema_changed" and ei.value.complex_no == CNO


def test_fetch_complex_missing_detail_is_schema_changed():
    client, _, _ = make_client({COMPLEX_PATH: json_resp({"something": 1})})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "schema_changed"


def test_fetch_complex_mismatched_complex_no():
    data = load("complex_20261008.json")
    data["complexDetail"]["complexNo"] = "12345"
    client, _, _ = make_client({COMPLEX_PATH: json_resp(data)})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "schema_changed"


# ---------------------------------------------------------------- 매물 (C3-3, 정규화)
def _collect():
    client, rec, sleeps = make_client(full_routes())
    cx, types = fetch_complex(client, CNO)
    return client, rec, sleeps, fetch_listings(client, cx, types)


def test_fetch_listings_paginates_to_end():
    client, rec, _, listings = _collect()
    art_reqs = [r for r in rec.requests if r.url.path == ART_PATH]
    assert [r.url.params["page"] for r in art_reqs] == ["1", "2", "3"]
    for r in art_reqs:
        assert r.url.params["tradeType"] == "A1"
        assert r.url.params["sameAddressGroup"] == "false"
    ids = [x.article_no for x in listings]
    assert "2600000007" in ids and "2600000008" in ids  # 3페이지 매물 포함
    assert len(ids) == len(set(ids)) == 7  # 페이지 경계 중복 제거, 145㎡·미매칭 제외


def test_fetch_listings_fields_normalized():
    client, _, _, listings = _collect()
    by = {x.article_no: x for x in listings}
    a = by["2600000001"]
    assert (a.area_key, a.dong, a.floor_raw, a.floor_group, a.direction, a.price) == (
        84.97, "101동", "10/25", "NORMAL", "남향", 125000)
    assert a.confirmed_at == date(2026, 10, 5)
    assert a.url == f"https://new.land.naver.com/complexes/{CNO}?articleNo=2600000001"
    assert a.realtor_count == 1 and a.alt_prices == []
    assert a.dedup_key == f"{CNO}|84.97|101동|10/25|남향"
    assert by["2600000002"].dong == "101동"  # "101 동" 공백 제거
    assert by["2600000003"].floor_group == "LOW"  # 저/25
    assert (by["2600000005"].area_key, by["2600000005"].floor_group, by["2600000005"].price) == (
        84.03, "LOW", 100500)  # B1/15
    u = by["2600000006"]
    assert (u.floor_raw, u.floor_group, u.confirmed_at, u.area_key) == ("", "UNKNOWN", None, 59.99)
    assert by["2600000007"].area_key == 94.5  # 공급 119.0 경계 포함
    assert by["2600000008"].area_key == 84.97  # areaName 없음 → 전용 84.98 ±0.5 매칭
    assert "2600000004" not in by  # 공급 145㎡ 제외
    assert all(isinstance(x.price, int) for x in listings)


def test_unmatched_listing_warned_not_silently_dropped():
    client, _, _, _ = _collect()
    assert any("평형을 특정하지 못한 매물 1건" in w for w in client.warnings)


def test_listings_then_dedup():
    _, _, _, listings = _collect()
    reps = dedup(listings)
    assert len(reps) == 6
    rep = next(x for x in reps if x.dedup_key == f"{CNO}|84.97|101동|10/25|남향")
    assert (rep.article_no, rep.price, rep.realtor_count, rep.alt_prices) == (
        "2600000002", 123000, 2, [125000])


def test_zero_listings_is_ok_not_error():
    routes = {COMPLEX_PATH: json_resp(load("complex_20261008.json")),
              ART_PATH: json_resp(load("articles_empty_20261008.json"))}
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    assert fetch_listings(client, cx, types) == []


def test_listing_schema_changed():
    routes = {COMPLEX_PATH: json_resp(load("complex_20261008.json")),
              ART_PATH: json_resp(load("articles_schema_changed_20261008.json"))}
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    with pytest.raises(CollectorError) as ei:
        fetch_listings(client, cx, types)
    assert ei.value.stage == "schema_changed" and ei.value.complex_no == CNO


@pytest.mark.parametrize("drop", ["articleNo", "dealOrWarrantPrc"])
def test_listing_missing_required_field(drop):
    page = load("articles_p3_20261008.json")
    del page["articleList"][0][drop]
    routes = {COMPLEX_PATH: json_resp(load("complex_20261008.json")), ART_PATH: json_resp(page)}
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    with pytest.raises(CollectorError) as ei:
        fetch_listings(client, cx, types)
    assert ei.value.stage == "schema_changed"


def test_unparseable_price_is_schema_changed():
    page = load("articles_p3_20261008.json")
    page["articleList"][0]["dealOrWarrantPrc"] = "가격문의"
    routes = {COMPLEX_PATH: json_resp(load("complex_20261008.json")), ART_PATH: json_resp(page)}
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    with pytest.raises(CollectorError) as ei:
        fetch_listings(client, cx, types)
    assert ei.value.stage == "schema_changed"


# ---------------------------------------------------------------- 요청 정책 (C3-4)
def test_wait_between_requests_2_to_5_seconds():
    for rng_val, expected in [(0.0, 2.0), (0.5, 3.5), (0.999999, 5.0)]:
        client, rec, sleeps = make_client(full_routes(), rng=lambda v=rng_val: v)
        cx, types = fetch_complex(client, CNO)
        fetch_listings(client, cx, types)
        assert len(rec.requests) == 4
        assert len(sleeps) == 3  # 첫 요청 전에는 대기 없음, 이후 매 요청 전 1회
        assert all(s == pytest.approx(expected) for s in sleeps)
        assert all(2.0 <= s <= 5.0 for s in sleeps)


def test_timeout_is_15s():
    seen = []

    def handler(req):
        seen.append(req.extensions.get("timeout"))
        return json_resp(load("complex_20261008.json"))

    client, _, _ = make_client({COMPLEX_PATH: handler})
    fetch_complex(client, CNO)
    assert TIMEOUT_S == 15.0
    assert seen[0]["read"] == 15.0 and seen[0]["connect"] == 15.0


@pytest.mark.parametrize("status", [401, 403, 429])
def test_blocked_status_stops_immediately(status):
    client, rec, sleeps = make_client({COMPLEX_PATH: httpx.Response(status)})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "blocked" and ei.value.complex_no == CNO
    assert len(rec.requests) == 1 and sleeps == []  # 재시도 없음
    # 이후 같은 실행의 네이버 요청은 보내지 않는다
    with pytest.raises(CollectorError) as ei2:
        fetch_complex(client, "22627")
    assert ei2.value.stage == "blocked"
    assert len(rec.requests) == 1


def test_html_response_is_blocked():
    html = (NAVER / "captcha_20261008.html").read_text(encoding="utf-8")
    client, rec, _ = make_client({COMPLEX_PATH: httpx.Response(200, html=html)})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "blocked"
    assert len(rec.requests) == 1


def test_html_body_without_content_type_is_blocked():
    client, _, _ = make_client({COMPLEX_PATH: httpx.Response(200, content=b"  <html>captcha</html>")})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "blocked"


def test_redirect_is_blocked():
    client, _, _ = make_client({COMPLEX_PATH: httpx.Response(302, headers={"Location": "/login"})})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "blocked"


def test_blocked_mid_pagination_raises_not_partial():
    routes = full_routes()
    routes[(ART_PATH, "2")] = httpx.Response(429)
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    with pytest.raises(CollectorError) as ei:
        fetch_listings(client, cx, types)
    assert ei.value.stage == "blocked"


def test_retry_with_backoff_then_success():
    def boom(req):
        raise httpx.ConnectTimeout("timeout", request=req)

    routes = {COMPLEX_PATH: [boom, httpx.Response(503), json_resp(load("complex_20261008.json"))]}
    client, rec, sleeps = make_client(routes, rng=lambda: 0.0)
    cx, _ = fetch_complex(client, CNO)
    assert cx.complex_no == CNO
    assert len(rec.requests) == 3
    # 재시도 전 백오프(5, 15) + 각 요청 사이 2~5초 대기
    assert sleeps == [BACKOFF_S[0], 2.0, BACKOFF_S[1], 2.0]


def test_retry_exhausted_is_network_error():
    client, rec, sleeps = make_client({COMPLEX_PATH: httpx.Response(500)}, rng=lambda: 0.0)
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "network"
    assert len(rec.requests) == 4  # 최초 1 + 재시도 3
    assert [s for s in sleeps if s != 2.0] == list(BACKOFF_S) == [5.0, 15.0, 45.0]


def test_404_is_network_error_without_retry():
    client, rec, _ = make_client({})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "network" and len(rec.requests) == 1


def test_non_json_body_is_schema_changed():
    client, _, _ = make_client({COMPLEX_PATH: httpx.Response(200, content=b"not json",
                                                             headers={"content-type": "application/json"})})
    with pytest.raises(CollectorError) as ei:
        fetch_complex(client, CNO)
    assert ei.value.stage == "schema_changed"


def test_requests_are_sequential_single_client():
    """요청은 하나의 동기 httpx.Client로 순서대로 나간다 (병렬 없음)."""
    _, rec, _, _ = _collect()
    paths = [(r.url.path, r.url.params.get("page")) for r in rec.requests]
    assert paths == [(COMPLEX_PATH, None), (ART_PATH, "1"), (ART_PATH, "2"), (ART_PATH, "3")]


def test_default_client_without_network_fails_loudly():
    """주입 없이 만든 클라이언트도 네트워크가 막히면 network 예외 (빈 결과 아님)."""
    client = NaverClient(sleep=lambda s: None)
    try:
        with pytest.raises(CollectorError) as ei:
            fetch_complex(client, CNO)
        assert ei.value.stage == "network"
    finally:
        client.close()


# ---------------------------------------------------------------- 미매칭 (CLAUDE.md §9 명세 확정(매물) ③)
def _art(no, area_name, a1, a2, price="10억"):
    return {"articleNo": no, "areaName": area_name, "area1": a1, "area2": a2, "floorInfo": "5/25",
            "dealOrWarrantPrc": price, "buildingName": "101동", "direction": "남향",
            "articleConfirmYmd": "20261005"}


def _run_with_articles(arts):
    page = {"_meta": {"synthetic": True}, "isMoreData": False, "articleList": arts}
    routes = {COMPLEX_PATH: json_resp(load("complex_20261008.json")), ART_PATH: json_resp(page)}
    client, _, _ = make_client(routes)
    cx, types = fetch_complex(client, CNO)
    return client, (lambda: fetch_listings(client, cx, types))


def test_all_listings_unmatched_is_schema_changed():
    client, run = _run_with_articles([_art("1", "999X", 100, 70), _art("2", None, None, 33.3)])
    with pytest.raises(CollectorError) as ei:
        run()
    assert ei.value.stage == "schema_changed" and ei.value.complex_no == CNO


def test_unmatched_half_or_more_warns():
    client, run = _run_with_articles([_art("1", "112A", 112, 84), _art("2", "999X", 100, 70)])
    assert len(run()) == 1
    assert any("50% 이상" in w and "1/2" in w for w in client.warnings)


def test_unmatched_below_half_warns_count_only():
    client, run = _run_with_articles([_art("1", "112A", 112, 84), _art("2", "113B", 113, 84),
                                      _art("3", "999X", 100, 70)])
    assert len(run()) == 2
    assert any("1건 제외" in w for w in client.warnings)
    assert not any("50% 이상" in w for w in client.warnings)


def test_only_non_target_listings_is_ok_empty():
    """공급 119㎡ 초과로 대상 외가 확정된 매물만 있으면 미매칭이 아니라 0건 (정상)."""
    client, run = _run_with_articles([_art("1", "145", 145, 114, "18억")])
    assert run() == []
    assert client.warnings == []

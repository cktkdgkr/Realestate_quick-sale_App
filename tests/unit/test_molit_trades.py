"""국토부 실거래 수집기 테스트 (C4-1~C4-4). fixture는 합성(synthetic)이다 — handoff 참고."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.collectors.errors import CollectorError
from app.collectors.molit_trades import (
    ENDPOINT,
    MolitClient,
    fetch_trades,
    find_apt_seq_candidates,
    mask_secrets,
    months_back,
)
from app.domain.models import AreaType, Complex

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "molit"
FAKE_KEY = "TEST+KEY/abc=="  # 가짜 키 (인코딩이 필요한 문자를 일부러 포함)
AS_OF = date(2026, 10, 6)


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _complex(seq: str | None = "11650-100") -> Complex:
    return Complex(complex_no="3009", name="잠원동아", lawd_cd="1165010600",
                   address="서울특별시 서초구 잠원동 60-1", max_floor=15, molit_apt_seq=seq)


AREAS = [
    AreaType("3009", 59.99, 59.99, 80.5, 24, "80"),
    AreaType("3009", 84.97, 84.97, 112.4, 34, "112"),
]


class FakeMolit:
    """(DEAL_YMD, pageNo) → 응답 본문. 지정 없는 월은 빈 응답."""

    def __init__(self, pages: dict[tuple[str, str], str] | None = None, status: int = 200,
                 body: str | None = None) -> None:
        self.pages = pages or {}
        self.status = status
        self.body = body
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.body is not None or self.status != 200:
            return httpx.Response(self.status, text=self.body or "")
        q = request.url.params
        key = (q["DEAL_YMD"], q["pageNo"])
        return httpx.Response(200, text=self.pages.get(key, _read("apt_trade_empty_synthetic.xml")))


def _client(fake, sleeps: list[float] | None = None) -> MolitClient:
    rec = sleeps if sleeps is not None else []
    return MolitClient(FAKE_KEY, http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=rec.append)


def _default_pages() -> dict[tuple[str, str], str]:
    return {
        ("202609", "1"): _read("apt_trade_11650_202609_p1_synthetic.xml"),
        ("202609", "2"): _read("apt_trade_11650_202609_p2_synthetic.xml"),
        ("202410", "1"): _read("apt_trade_11650_202410_synthetic.xml"),
    }


# ------------------------------------------------------------ C4-1 범위·페이지·캐시
def test_months_back_25_including_current_month() -> None:
    ms = months_back(AS_OF)
    assert len(ms) == 25
    assert ms[0] == "202610" and ms[-1] == "202410"
    assert months_back(date(2026, 1, 31), 2) == ["202601", "202512"]


def test_fetch_queries_25_months_with_pagination_and_params() -> None:
    fake = FakeMolit(_default_pages())
    sleeps: list[float] = []
    client = _client(fake, sleeps)
    fetch_trades(client, _complex(), AREAS, AS_OF)

    months = [(r.url.params["DEAL_YMD"], r.url.params["pageNo"]) for r in fake.calls]
    assert len({m for m, _ in months}) == 25
    assert ("202609", "2") in months  # totalCount=7, 1페이지 4건 → 2페이지 요청
    assert len(fake.calls) == 26
    r = fake.calls[0]
    assert str(r.url).startswith(ENDPOINT)
    assert r.url.params["LAWD_CD"] == "11650"
    assert r.url.params["numOfRows"] == "1000"
    assert r.url.params["serviceKey"] == FAKE_KEY  # httpx가 한 번만 인코딩 (이중 인코딩 없음)
    # 순차 요청 사이 대기 (주입된 sleep, 실제로 기다리지 않음)
    assert len(sleeps) == len(fake.calls) - 1


def test_cache_per_lawd_month_shared_across_complexes_and_candidates() -> None:
    fake = FakeMolit(_default_pages())
    client = _client(fake)
    fetch_trades(client, _complex(), AREAS, AS_OF)
    n = len(fake.calls)
    other = Complex("9999", "신반포한신", "11650", "서울 서초구 잠원동 61", None, "11650-200")
    fetch_trades(client, other, AREAS, AS_OF)
    find_apt_seq_candidates(client, _complex(None), AS_OF)
    assert len(fake.calls) == n  # 같은 (LAWD_CD, 월)은 재요청하지 않음


def test_short_page_against_total_count_is_error() -> None:
    # totalCount=7인데 2페이지가 비어 있으면 빈 결과로 숨기지 않는다
    pages = {("202609", "1"): _read("apt_trade_11650_202609_p1_synthetic.xml")}
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(FakeMolit(pages)), _complex(), AREAS, AS_OF)
    assert ei.value.stage == "molit_api"
    assert ei.value.complex_no == "3009"


# ------------------------------------------------------------ C4-2 해제 거래, 변환
def test_trades_converted_and_cancelled_kept() -> None:
    client = _client(FakeMolit(_default_pages()))
    trades = fetch_trades(client, _complex(), AREAS, AS_OF)
    by = {(t.contract_date, t.floor): t for t in trades}
    assert len(trades) == 6  # 대상 단지 7건 중 72.30(매칭 실패)·114.50(36평 초과) 제외, 2024-10 2건 포함

    t = by[(date(2026, 9, 3), 12)]
    assert (t.area_key, t.price, t.floor_group, t.cancelled, t.source) == (84.97, 235000, "NORMAL", False, "MOLIT")
    assert t.deal_type == "중개거래" and isinstance(t.price, int)

    c = by[(date(2026, 9, 10), 7)]
    assert c.cancelled is True and c.price == 238000  # 해제 거래도 남긴다

    low = by[(date(2026, 9, 14), 2)]
    assert (low.area_key, low.floor_group, low.deal_type) == (59.99, "LOW", "직거래")
    basement = by[(date(2026, 9, 20), -1)]
    assert basement.floor_group == "LOW" and basement.area_key == 59.99 and basement.exclusive_m2 == 59.96

    assert {t.contract_date for t in trades} >= {date(2024, 10, 1), date(2024, 10, 30)}
    assert all(t.complex_no == "3009" for t in trades)


# ------------------------------------------------------------ C4-3 단지 식별
def test_filters_by_apt_seq_only_not_name() -> None:
    client = _client(FakeMolit(_default_pages()))
    # 이름이 전혀 다른 Complex여도 aptSeq가 같으면 그 단지 거래를 가져온다
    renamed = Complex("3009", "완전히 다른 이름", "11650", "", None, "11650-100")
    trades = fetch_trades(client, renamed, AREAS, AS_OF)
    assert len(trades) == 6
    assert all(t.price != 300000 for t in trades)  # 다른 aptSeq(11650-200) 거래 제외
    other = fetch_trades(client, Complex("x", "잠원동아", "11650", "", None, "11650-200"), AREAS, AS_OF)
    assert [t.price for t in other] == [300000]  # 이름이 같아도 aptSeq가 다르면 제외


@pytest.mark.parametrize("seq", [None, "", "   ", "\t\n"])
def test_missing_apt_seq_raises_complex_mapping_without_request(seq: str | None) -> None:
    fake = FakeMolit(_default_pages())
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(fake), _complex(seq), AREAS, AS_OF)
    assert ei.value.stage == "complex_mapping" and ei.value.complex_no == "3009"
    assert fake.calls == []


def test_candidates_listed_not_auto_confirmed() -> None:
    client = _client(FakeMolit(_default_pages()))
    cands = find_apt_seq_candidates(client, _complex(None), AS_OF)
    assert [c["apt_seq"] for c in cands] == ["11650-100", "11650-200"]
    top = cands[0]
    assert top["apt_nm"] == "잠원동아" and top["umd_nm"] == "잠원동" and top["jibun"] == "60-1"
    assert top["trade_count"] == 8 and top["last_contract"] == "2026-09-22"
    assert "지번 일치" in top["hints"]
    # 반환만 하고 Complex를 바꾸지 않는다
    c = _complex(None)
    find_apt_seq_candidates(client, c, AS_OF)
    assert c.molit_apt_seq is None


def test_candidates_empty_when_no_trades_in_lawd() -> None:
    assert find_apt_seq_candidates(_client(FakeMolit()), _complex(None), AS_OF) == []


# ------------------------------------------------------------ C4-4 면적 매칭
def test_unmatched_area_warned_but_oversize_silent(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(FakeMolit(_default_pages()))
    trades = fetch_trades(client, _complex(), AREAS, AS_OF)
    assert all(t.exclusive_m2 not in (72.30, 114.50) for t in trades)
    warns = client.drain_warnings()
    assert len(warns) == 1
    assert "3009" in warns[0] and "1건" in warns[0] and "72.3㎡" in warns[0]
    assert "114.5" not in warns[0]
    assert client.drain_warnings() == []  # drain 후 비워짐


def test_area_boundary_half_m2() -> None:
    areas = [AreaType("3009", 72.80, 72.80, 95.0, 29, "95")] + AREAS
    trades = fetch_trades(_client(FakeMolit(_default_pages())), _complex(), areas, AS_OF)
    # 72.30은 72.80과 정확히 0.5 차이 → 매칭
    assert any(t.exclusive_m2 == 72.30 and t.area_key == 72.80 for t in trades)


def test_empty_area_types_returns_no_trades_without_request_or_warning() -> None:
    fake = FakeMolit(_default_pages())
    client = _client(fake)
    assert fetch_trades(client, _complex(), [], AS_OF) == []
    assert client.warnings == [] and fake.calls == []
    with pytest.raises(CollectorError):  # 매핑 검증은 평형 유무와 상관없이 먼저 한다
        fetch_trades(client, _complex("  "), [], AS_OF)


# ------------------------------------------------------------ 오류 처리·비밀값
@pytest.mark.parametrize(
    "fixture,stage",
    [("error_result_code_synthetic.xml", "molit_api"),
     ("error_auth_gateway_synthetic.xml", "molit_auth"),
     ("error_quota_gateway_synthetic.xml", "molit_api")],
)
def test_result_code_errors(fixture: str, stage: str) -> None:
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(FakeMolit(body=_read(fixture))), _complex(), AREAS, AS_OF)
    assert ei.value.stage == stage
    assert FAKE_KEY not in str(ei.value)


@pytest.mark.parametrize("status,stage", [(401, "molit_auth"), (403, "molit_auth"), (404, "molit_api")])
def test_http_errors(status: int, stage: str) -> None:
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(FakeMolit(status=status, body="Unauthorized")), _complex(), AREAS, AS_OF)
    assert ei.value.stage == stage


def test_plain_text_auth_error_body() -> None:
    fake = FakeMolit(body="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(fake), _complex(), AREAS, AS_OF)
    assert ei.value.stage == "molit_auth"


def test_network_error_retried_then_raises_without_key(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[httpx.Request] = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError(f"connect failed for {request.url}", request=request)

    sleeps: list[float] = []
    client = MolitClient(FAKE_KEY, http=httpx.Client(transport=httpx.MockTransport(boom)), sleep=sleeps.append)
    with caplog.at_level(logging.DEBUG), pytest.raises(CollectorError) as ei:
        fetch_trades(client, _complex(), AREAS, AS_OF)
    assert ei.value.stage == "network"
    assert len(calls) == 4 and sleeps == [5.0, 15.0, 45.0]
    for text in [str(ei.value), caplog.text]:
        assert FAKE_KEY not in text
        assert "TEST%2BKEY" not in text and "TEST+KEY" not in text


def test_server_error_then_success() -> None:
    seq = iter([httpx.Response(503, text="busy")])

    def handler(request: httpx.Request) -> httpx.Response:
        nxt = next(seq, None)
        return nxt if nxt is not None else httpx.Response(200, text=_read("apt_trade_empty_synthetic.xml"))

    client = MolitClient(FAKE_KEY, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert fetch_trades(client, _complex(), AREAS, AS_OF) == []


def test_schema_change_missing_field() -> None:
    bad = _read("apt_trade_11650_202410_synthetic.xml").replace("<dealAmount>230,000</dealAmount>", "")
    with pytest.raises(CollectorError) as ei:
        fetch_trades(_client(FakeMolit({("202410", "1"): bad})), _complex(), AREAS, AS_OF)
    assert ei.value.stage == "schema_changed"


def test_httpx_request_log_is_masked(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(FakeMolit())
    with caplog.at_level(logging.INFO, logger="httpx"):
        fetch_trades(client, _complex(), AREAS, AS_OF)
    assert "serviceKey=***" in caplog.text  # httpx INFO 요청 로그가 마스킹됨
    assert "TEST%2BKEY" not in caplog.text and FAKE_KEY not in caplog.text


def test_mask_secrets_and_repr() -> None:
    param = "service" + "Key"  # 비밀값 grep(C0-3) 오탐 방지용으로 나눠 쓴다
    url = f"https://x/y?{param}=TEST%2BKEY%2Fabc%3D%3D&LAWD_CD=11650"
    assert mask_secrets(url) == f"https://x/y?{param}=***&LAWD_CD=11650"
    assert "TEST" not in mask_secrets("key TEST+KEY/abc== leaked", FAKE_KEY)
    assert FAKE_KEY not in repr(_client(FakeMolit()))


def test_empty_key_rejected() -> None:
    with pytest.raises(CollectorError) as ei:
        MolitClient("", http=httpx.Client(transport=httpx.MockTransport(FakeMolit())))
    assert ei.value.stage == "molit_auth"


def test_fixtures_marked_synthetic_and_contain_no_key() -> None:
    for p in FIX.glob("*.xml"):
        text = p.read_text(encoding="utf-8")
        assert "_meta synthetic=true" in text, p.name
        assert "serviceKey" not in text, p.name


def test_close_does_not_close_injected_http() -> None:
    http = httpx.Client(transport=httpx.MockTransport(FakeMolit()))
    with MolitClient(FAKE_KEY, http=http, sleep=lambda s: None) as c:
        fetch_trades(c, _complex(), AREAS, AS_OF)
    assert not http.is_closed


RAW_KEY = "RAWkey+/=Zq9secretVALUE=="


@pytest.mark.parametrize(
    "body,stage",
    [
        # (a) HTTP 200, XML 아닌 본문에 키 원문이 그대로 되돌아옴 (80자 자르기 전에 가려야 함)
        ("INVALID_REQUEST_PARAMETER_ERROR key " + RAW_KEY, "molit_api"),
        # (b) 게이트웨이 XML returnAuthMsg에 키
        ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
         "<returnAuthMsg>UNREGISTERED KEY " + RAW_KEY + "</returnAuthMsg>"
         "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>", "molit_auth"),
        # (c) resultCode 30, resultMsg에 키
        ("<response><header><resultCode>30</resultCode><resultMsg>bad key " + RAW_KEY
         + "</resultMsg></header></response>", "molit_auth"),
        # (d) URL 인코딩형(소문자 %xx 포함)이 되돌아옴
        ("<response><header><resultCode>99</resultCode><resultMsg>echo "
         + "RAWkey%2b%2f%3dZq9secretVALUE%3d%3d</resultMsg></header></response>", "molit_api"),
    ],
)
def test_key_echoed_in_error_body_is_masked(body: str, stage: str, caplog: pytest.LogCaptureFixture) -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    client = MolitClient(RAW_KEY, http=http, sleep=lambda s: None)
    with caplog.at_level(logging.DEBUG), pytest.raises(CollectorError) as ei:
        fetch_trades(client, _complex(), AREAS, AS_OF)
    assert ei.value.stage == stage
    for text in (str(ei.value), ei.value.detail, repr(ei.value.args), caplog.text):
        assert "Zq9secret" not in text

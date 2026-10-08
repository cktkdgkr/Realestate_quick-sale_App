"""검증자 반례: 2단계 실거래 수집 (molit_trades, naver_trades, cross_check). 네트워크 사용 안 함.

실행: .venv/bin/python tests/verifier/verify_stage2_trades.py
각 항목 PASS/FAIL 출력. FAIL이 있으면 종료 코드 1.
"""
from __future__ import annotations

import io
import logging
import socket
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 외부 네트워크 차단 (실수로 실제 호출하면 즉시 실패)
def _no_net(*a, **k):
    raise RuntimeError("network blocked by verifier")
socket.socket.connect = _no_net  # type: ignore[assignment]

import httpx  # noqa: E402

from app.collectors.errors import CollectorError  # noqa: E402
from app.collectors import molit_trades as mt  # noqa: E402
from app.collectors.molit_trades import MolitClient, fetch_trades, find_apt_seq_candidates, months_back  # noqa: E402
from app.collectors.naver_trades import cross_check  # noqa: E402
from app.domain.models import AreaType, Complex, Trade  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
KEY = "RAWkey+/=Zq9secretVALUE=="


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def item(seq="S1", ar="84.97", amt="235,000", y=2026, m=9, d=3, floor="12", cdeal="", gbn="중개거래", nm="A"):
    return (f"<item><aptNm>{nm}</aptNm><aptSeq>{seq}</aptSeq><cdealType>{cdeal}</cdealType>"
            f"<dealAmount>{amt}</dealAmount><dealDay>{d}</dealDay><dealMonth>{m}</dealMonth>"
            f"<dealYear>{y}</dealYear><dealingGbn>{gbn}</dealingGbn><excluUseAr>{ar}</excluUseAr>"
            f"<floor>{floor}</floor><jibun>1</jibun><umdNm>X동</umdNm></item>")


def xml(items: list[str], total: int | None = None) -> str:
    t = len(items) if total is None else total
    return (f"<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>"
            f"<body><items>{''.join(items)}</items><numOfRows>1000</numOfRows><pageNo>1</pageNo>"
            f"<totalCount>{t}</totalCount></body></response>")


EMPTY = xml([])


class Fake:
    def __init__(self, pages=None, default=EMPTY, fn=None):
        self.pages = pages or {}
        self.default = default
        self.calls: list[httpx.Request] = []
        self.fn = fn

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fn:
            return self.fn(req)
        q = req.url.params
        return httpx.Response(200, text=self.pages.get((q["LAWD_CD"], q["DEAL_YMD"], q["pageNo"]), self.default))


def client(fake, sleeps=None):
    rec = sleeps if sleeps is not None else []
    return MolitClient(KEY, http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=rec.append)


def cx(no="C1", lawd="1165010600", seq="S1"):
    return Complex(no, "단지", lawd, "서울 서초구 X동 1", 20, seq)


AREAS = [AreaType("C1", 59.99, 59.99, 80.0, 24, "80"), AreaType("C1", 84.97, 84.97, 112.0, 34, "112")]

# ---------------------------------------------------------------- 1. 24/25개월 경계
as_of = date(2026, 10, 6)
ms = months_back(as_of)
check("months: 25개, 202610..202410", len(ms) == 25 and ms[0] == "202610" and ms[-1] == "202410", str(ms[:2] + ms[-2:]))
ms2 = months_back(date(2026, 1, 1))
check("months: 1월 실행 → 202601..202401", ms2[0] == "202601" and ms2[-1] == "202401" and len(set(ms2)) == 25, str(ms2[-3:]))
ms3 = months_back(date(2024, 2, 29))
check("months: 윤년 2/29 → 202202 포함", ms3[-1] == "202202", ms3[-1])
# 24개월 경계일(2024-10-06) 앞뒤 거래가 모두 반환되는가 (기간 필터는 판정 단계)
pages = {("11650", "202410", "1"): xml([item(y=2024, m=10, d=5), item(y=2024, m=10, d=6, floor="5"),
                                        item(y=2024, m=10, d=1, floor="7")]),
         ("11650", "202409", "1"): xml([item(y=2024, m=9, d=30)])}
f = Fake(pages)
tr = fetch_trades(client(f), cx(), AREAS, as_of)
asked = {r.url.params["DEAL_YMD"] for r in f.calls}
check("25개월: 202409(25개월 밖) 요청 안 함", "202409" not in asked and len(asked) == 25, f"asked={len(asked)}")
check("25개월: 202410의 1·5·6일 거래 모두 반환", sorted(t.contract_date.day for t in tr) == [1, 5, 6], str([t.contract_date for t in tr]))

# ---------------------------------------------------------------- 2. totalCount 페이지네이션
p1 = xml([item(d=1)] * 1000, total=2001)
p2 = xml([item(d=2)] * 1000, total=2001)
p3 = xml([item(d=3)], total=2001)
f = Fake({("11650", "202609", "1"): p1, ("11650", "202609", "2"): p2, ("11650", "202609", "3"): p3})
tr = fetch_trades(client(f), cx(), AREAS, as_of)
pg = [r.url.params["pageNo"] for r in f.calls if r.url.params["DEAL_YMD"] == "202609"]
check("pagination: 2001건 → 3페이지", pg == ["1", "2", "3"] and len(tr) == 2001, f"pages={pg} n={len(tr)}")
# 2페이지가 비면 실패 (빈 결과로 숨기지 않음)
f = Fake({("11650", "202609", "1"): p1})
try:
    fetch_trades(client(f), cx(), AREAS, as_of)
    check("pagination: 중간 빈 페이지 → 오류", False, "no error")
except CollectorError as e:
    check("pagination: 중간 빈 페이지 → 오류", e.stage == "molit_api" and e.complex_no == "C1", str(e))
# 마지막 페이지가 totalCount보다 적게 끝나면 (항목 있지만 총합 부족) → 다음 페이지 요청 후 빈 → 오류
f = Fake({("11650", "202609", "1"): p1, ("11650", "202609", "2"): xml([item()] * 10, total=2001)})
try:
    fetch_trades(client(f), cx(), AREAS, as_of)
    check("pagination: 총합 부족 → 오류", False, "no error")
except CollectorError as e:
    check("pagination: 총합 부족 → 오류", e.stage == "molit_api", str(e))

# ---------------------------------------------------------------- 3. 같은 LAWD_CD 두 단지 캐시 공유
pages = {("11650", "202609", "1"): xml([item(seq="S1"), item(seq="S2", amt="300,000")])}
f = Fake(pages)
c = client(f)
a = fetch_trades(c, cx("C1", "1165010600", "S1"), AREAS, as_of)
n1 = len(f.calls)
b = fetch_trades(c, cx("C2", "1165010700", "S2"), [AreaType("C2", 84.97, 84.97, 112.0, 34, "112")], as_of)
check("cache: 다른 법정동(같은 시군구 앞5자리) 두 단지가 응답 공유", len(f.calls) == n1 == 25, f"calls {n1}->{len(f.calls)}")
check("cache: 단지별 aptSeq로 분리", [t.price for t in a] == [235000] and [t.price for t in b] == [300000]
      and b[0].complex_no == "C2", f"{[t.price for t in a]} {[t.price for t in b]}")
f2 = Fake(pages)
fetch_trades(client(f2), cx(), AREAS, as_of)
fetch_trades(client(f2), cx(), AREAS, as_of)
check("cache: 클라이언트 인스턴스 간에는 공유 안 함(실행 1회=인스턴스 1개)", len(f2.calls) == 50, str(len(f2.calls)))
# 다른 LAWD는 따로 요청
f3 = Fake()
c3 = client(f3)
fetch_trades(c3, cx("C1", "11650"), AREAS, as_of)
fetch_trades(c3, cx("C3", "11680", "S9"), AREAS, as_of)
check("cache: 다른 LAWD_CD는 별도 요청", len(f3.calls) == 50, str(len(f3.calls)))
# 실패한 월은 캐시되지 않아야 함 (다음 단지가 같은 월을 다시 시도) — 동작 기록용
state = {"n": 0}
def flaky(req):
    state["n"] += 1
    if req.url.params["DEAL_YMD"] == "202609" and state["n"] < 3:
        return httpx.Response(200, text="<response><header><resultCode>99</resultCode><resultMsg>ERR</resultMsg></header></response>")
    return httpx.Response(200, text=EMPTY)
f4 = Fake(fn=flaky)
c4 = client(f4)
try:
    fetch_trades(c4, cx(), AREAS, as_of)
    first = "no error"
except CollectorError as e:
    first = e.stage
try:
    fetch_trades(c4, cx("C2", seq="S2"), AREAS, as_of)
    second = "ok"
except CollectorError as e:
    second = e.stage
check("cache: 오류 월은 캐시 안 됨 → 다음 단지 재시도", first == "molit_api" and second in ("ok", "molit_api"), f"{first}/{second}")

# ---------------------------------------------------------------- 4. 해제 거래 보존
pages = {("11650", "202609", "1"): xml([item(cdeal="O", d=5), item(cdeal="", d=6), item(cdeal=" O ", d=7),
                                        item(cdeal="o", d=8)])}
tr = fetch_trades(client(Fake(pages)), cx(), AREAS, as_of)
flags = {t.contract_date.day: t.cancelled for t in tr}
check("cancelled: O→True, 빈칸→False, 공백·소문자도 True, 버리지 않음", flags == {5: True, 6: False, 7: True, 8: True}, str(flags))

# ---------------------------------------------------------------- 5. ±0.5㎡ 경계·동시 매칭
areas = [AreaType("C1", 59.5, 59.5, 80, 24, "a"), AreaType("C1", 60.5, 60.5, 81, 25, "b"),
         AreaType("C1", 84.97, 84.97, 112, 34, "c")]
cases = {"60.0": 59.5, "60.1": 60.5, "59.9": 59.5, "84.47": 84.97, "85.47": 84.97, "59.0": 59.5,
         "84.46": None, "58.99": None, "85.48": None, "61.0": 60.5, "61.01": None}
its = [item(ar=k, d=i + 1) for i, k in enumerate(cases)]
c = client(Fake({("11650", "202609", "1"): xml(its)}))
tr = fetch_trades(c, cx(), areas, as_of)
got = {f"{t.exclusive_m2}": t.area_key for t in tr}
bad = []
for k, v in cases.items():
    g = got.get(str(float(k)))
    if g != v:
        bad.append(f"{k}: expect {v} got {g}")
check("area: ±0.5 경계(포함)와 동시 매칭 시 가까운 쪽, 동률이면 작은 key", not bad, "; ".join(bad))
w = c.drain_warnings()
check("area: 대상 범위 안 미매칭(84.46, 58.99, 61.01)은 경고", len(w) == 1 and "84.46" in w[0] and "58.99" in w[0]
      and "61.01" in w[0] and "85.48" not in w[0], str(w))
# 36평 초과(대상 밖 큰 평형)는 경고 없이 제외
c = client(Fake({("11650", "202609", "1"): xml([item(ar="114.5"), item(ar="134.8")])}))
tr = fetch_trades(c, cx(), AREAS, as_of)
check("area: 36평 초과 거래는 경고 없이 제외", tr == [] and c.warnings == [], str(c.warnings))

# ---------------------------------------------------------------- 6. molit_apt_seq None/빈값
for seq in (None, ""):
    f = Fake()
    try:
        fetch_trades(client(f), cx(seq=seq), AREAS, as_of)
        check(f"apt_seq {seq!r} → complex_mapping", False, "no error")
    except CollectorError as e:
        check(f"apt_seq {seq!r} → complex_mapping, 요청 없음", e.stage == "complex_mapping" and not f.calls, str(e))
f = Fake({("11650", "202609", "1"): xml([item(seq="S1")])})
try:
    r = fetch_trades(client(f), cx(seq="   "), AREAS, as_of)
    check("apt_seq 공백 문자열 → complex_mapping (빈 결과로 숨기지 않음)", False,
          f"반환={r!r}, 요청 {len(f.calls)}회 — 오류 없이 빈 목록")
except CollectorError as e:
    check("apt_seq 공백 문자열 → complex_mapping (빈 결과로 숨기지 않음)", e.stage == "complex_mapping", str(e))
# 후보 0개/복수 — 자동 확정 안 함
cc = cx(seq=None)
cands = find_apt_seq_candidates(client(Fake({("11650", "202609", "1"): xml([item(seq="S1"), item(seq="S2")])})), cc, as_of)
check("candidates: 복수 후보 반환, Complex 불변", len(cands) == 2 and cc.molit_apt_seq is None, str([x["apt_seq"] for x in cands]))
cands1 = find_apt_seq_candidates(client(Fake({("11650", "202609", "1"): xml([item(seq="S1")])})), cc, as_of)
check("candidates: 1개여도 Complex 불변", len(cands1) == 1 and cc.molit_apt_seq is None, "")

# ---------------------------------------------------------------- 7. 인증 오류 시 키 노출
log_buf = io.StringIO()
h = logging.StreamHandler(log_buf)
h.setLevel(logging.DEBUG)
root = logging.getLogger()
root.addHandler(h)
root.setLevel(logging.DEBUG)
enc_variants = [KEY, "RAWkey%2B%2F%3DZq9secretVALUE%3D%3D", "RAWkey+%2F%3D", "Zq9secret"]

def leaked(text: str) -> list[str]:
    return [v for v in enc_variants if v in text]

def run_err(fn, label):
    f = Fake(fn=fn)
    c = client(f)
    try:
        fetch_trades(c, cx(), AREAS, as_of)
        check(f"key-leak {label}: 오류 발생", False, "no error")
        return
    except CollectorError as e:
        texts = [str(e), repr(e), e.detail, repr(c), log_buf.getvalue(), str(e.__cause__), str(e.__context__)]
        tb_ctx = []
        x = e
        while x is not None:
            tb_ctx.append(repr(x))
            x = x.__context__ or x.__cause__
        texts += tb_ctx
        bad = sorted({v for t in texts for v in leaked(t)})
        check(f"key-leak {label}: stage={e.stage}, 키 노출 없음", not bad, f"leak={bad} msg={e}")

run_err(lambda r: httpx.Response(401, text="Unauthorized"), "HTTP401")
run_err(lambda r: httpx.Response(200, text=(ROOT / "tests/fixtures/molit/error_auth_gateway_synthetic.xml").read_text()), "gateway30")
run_err(lambda r: httpx.Response(200, text=f"SERVICE_KEY_IS_NOT_REGISTERED_ERROR serviceKey={KEY}"), "plain-echo-param")
def raise_conn(r):
    raise httpx.ConnectError(f"fail {r.url}", request=r)
run_err(raise_conn, "ConnectError(url 포함)")
# 게이트웨이가 키 원문을 메시지에 그대로 되돌려주는 경우 (serviceKey= 접두 없이)
run_err(lambda r: httpx.Response(200, text=f"INVALID_REQUEST_PARAMETER_ERROR key {KEY}"), "plain-echo-raw")
run_err(lambda r: httpx.Response(200, text=f"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
                                       f"<returnAuthMsg>UNREGISTERED KEY {KEY}</returnAuthMsg><returnReasonCode>30</returnReasonCode>"
                                       f"</cmmMsgHeader></OpenAPI_ServiceResponse>"), "gateway-echo-raw")
run_err(lambda r: httpx.Response(200, text=f"<response><header><resultCode>30</resultCode><resultMsg>bad key {KEY}</resultMsg></header></response>"), "resultMsg-echo-raw")
# 정상 요청에서 httpx INFO 로그 마스킹
log_buf.truncate(0); log_buf.seek(0)
fetch_trades(client(Fake()), cx(), AREAS, as_of)
check("key-leak: 정상 요청 httpx 로그 마스킹", not leaked(log_buf.getvalue()) and "serviceKey=***" in log_buf.getvalue(),
      log_buf.getvalue()[:200])
root.removeHandler(h)

# ---------------------------------------------------------------- 8. 교차검증 경고 3종
def T(src, d, p, fl=10, ak=84.97, canc=False, cno="C1"):
    return Trade(cno, ak, ak, fl, "LOW" if fl <= 2 else "NORMAL", p, d, canc, "중개거래", src)

w = cross_check([T("MOLIT", date(2026, 5, 1), 230000)], [T("NAVER", date(2026, 5, 2), 231000)], as_of)
check("cross: 가격 불일치 WARN 1건", len(w) == 1 and w[0].startswith("[WARN]") and "불일치" in w[0], str(w))
w = cross_check([T("MOLIT", date(2026, 8, 6), 230000), T("MOLIT", date(2026, 8, 5), 230000, fl=11),
                 T("MOLIT", date(2024, 10, 1), 200000, fl=12)], [], as_of)
# CLAUDE.md §9 명세 확정(실거래) ①: 국토부에만 있는 거래는 기간 무관 [INFO]
check("cross: 국토부에만 — 기간 무관 INFO (§9 실거래 ①)", len(w) == 3 and all(x.startswith("[INFO]") for x in w), str(w))
check("cross: 국토부에만 — 최근 2개월(경계 포함)만 '반영 지연' 문구", sum("반영 지연" in x for x in w) == 1
      and any("2026-08-06" in x and "반영 지연" in x for x in w), str(w))
w = cross_check([], [T("NAVER", date(2025, 3, 1), 200000)], as_of)
check("cross: 네이버에만 WARN", len(w) == 1 and w[0].startswith("[WARN]") and "네이버에만" in w[0], str(w))
w = cross_check([T("MOLIT", date(2025, 3, 1), 200000)], [T("NAVER", date(2025, 3, 20), 200000)], as_of)
check("cross: 같은 월·층·가격 → 경고 없음", w == [], str(w))
w = cross_check([T("MOLIT", date(2025, 3, 1), 200000, canc=True)], [], as_of)
check("cross: 국토부 해제 거래만 → 경고 없음", w == [], str(w))
w = cross_check([T("MOLIT", date(2024, 9, 30), 1)], [T("NAVER", date(2024, 9, 30), 2)], as_of)
check("cross: 25개월 창 밖 무시", w == [], str(w))
mol = [T("MOLIT", date(2026, 3, 1), 100000 + i, fl=3 + i % 5) for i in range(20)]
nav = [T("NAVER", date(2026, 3, 1), 100000 + i + (i % 3), fl=3 + i % 5) for i in range(20)]
import random
outs = set()
for s in range(10):
    random.seed(s); a = mol[:]; b = nav[:]; random.shuffle(a); random.shuffle(b)
    outs.add(tuple(cross_check(a, b, as_of)))
check("cross: 입력 순서 섞어도 결정적", len(outs) == 1, f"{len(outs)} variants")
# 판정은 국토부 기준 — cross_check는 Trade 목록을 바꾸지 않음
m0 = [T("MOLIT", date(2026, 5, 1), 230000)]
snap = [vars(t).copy() if hasattr(t, "__dict__") else t for t in m0]
cross_check(m0, [T("NAVER", date(2026, 5, 1), 1)], as_of)
check("cross: 입력 불변", [vars(t) if hasattr(t, "__dict__") else t for t in m0] == snap, "")

# ---------------------------------------------------------------- 9. 순차·대기 (국토부)
sleeps: list[float] = []
f = Fake()
fetch_trades(client(f, sleeps), cx(), AREAS, as_of)
check("molit: 요청 간 대기, 25요청→24대기", len(sleeps) == 24 and all(s > 0 for s in sleeps), str(sleeps[:3]))

# ---------------------------------------------------------------- 10. 재검증 추가 (2차)
# 빈 area_types: 요청 없이 [] (대상 평형 0개는 정상), 단 매핑 오류는 여전히 먼저 오류
f = Fake({("11650", "202609", "1"): xml([item()])})
c = client(f)
check("empty area_types: 요청 없이 [], 경고 없음", fetch_trades(c, cx(), [], as_of) == [] and not f.calls and not c.warnings, "")
for bad_cx, label in ((cx(seq=None), "seq None"), (cx(seq="  "), "seq 공백"), (cx(lawd="11x"), "lawd 오류")):
    try:
        fetch_trades(client(Fake()), bad_cx, [], as_of)
        check(f"empty area_types + {label} → complex_mapping", False, "no error")
    except CollectorError as e:
        check(f"empty area_types + {label} → complex_mapping", e.stage == "complex_mapping", str(e))
# 소문자 인코딩형 키 반사
log_buf2 = io.StringIO(); h2 = logging.StreamHandler(log_buf2); root.addHandler(h2)
f = Fake(fn=lambda r: httpx.Response(200, text="ERR key=rawkey%2b%2f%3dzq9secretvalue%3d%3d"))
try:
    fetch_trades(client(f), cx(), AREAS, as_of)
    check("key-leak: 소문자 인코딩형 반사", False, "no error")
except CollectorError as e:
    check("key-leak: 소문자 인코딩형 반사 가림", "zq9secret" not in str(e).lower() and "zq9secret" not in repr(e.__context__).lower()
          and e.__cause__ is None and e.__suppress_context__, f"{e} / ctx={e.__context__!r}")
# 정상 응답 파싱은 마스킹 전처리 후에도 그대로
tr = fetch_trades(client(Fake({("11650", "202609", "1"): xml([item()])})), cx(), AREAS, as_of)
check("mask 전처리 후 정상 파싱 유지", [t.price for t in tr] == [235000], str(tr))
root.removeHandler(h2)

# 네이버 실거래: 지하층·필수 필드 (§9 실거래 ③④)
import json as _json
from app.collectors.naver_listings import NaverClient
from app.collectors.naver_trades import fetch_naver_trades, parse_naver_trade_floor
fl_cases = {"B1": -1, "B2": -2, "지하1": -1, "지하 1층": -1, "B1층": -1, "b1": -1, 3: 3, "12": 12, "옥탑": None, None: None}
badf = [f"{k!r}: {parse_naver_trade_floor(k)} != {v}" for k, v in fl_cases.items() if parse_naver_trade_floor(k) != v]
check("naver floor: 지하 → 음수, 파싱 불가 → None", not badf, "; ".join(badf))
CXN = Complex("3009", "합성", "11650", "", 15, "S1")
AREAN = [AreaType("3009", 84.97, 84.97, 112.4, 34, "112")]
cplx = (ROOT / "tests/fixtures/naver/trades_complex_3009_synthetic.json").read_text()
def naver_with(items):
    body = _json.dumps({"realPriceOnMonthList": [{"realPriceList": items}]})
    def h(req):
        txt = cplx if req.url.path == "/api/complexes/3009" else body
        return httpx.Response(200, text=txt, headers={"content-type": "application/json"})
    return NaverClient(http=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None, rng=lambda: 0.5)
base = {"tradeType": "A1", "tradeYear": "2026", "tradeMonth": 9, "tradeDate": "3", "dealPrice": 235000,
        "floor": "B1", "deleteYn": "N"}
nt = fetch_naver_trades(naver_with([base]), CXN, AREAN)
check("naver: B1 거래 → floor=-1, LOW", [(t.floor, t.floor_group) for t in nt] == [(-1, "LOW")], str(nt))
w = cross_check([Trade("3009", 84.97, 84.97, -1, "LOW", 235000, date(2026, 9, 3), False, "중개거래", "MOLIT")], nt, as_of)
check("cross: 국토부 -1층과 네이버 B1 짝지음 → 경고 없음", w == [], str(w))
for fld in ("tradeType", "tradeYear", "tradeMonth", "tradeDate", "floor"):
    it = dict(base); it.pop(fld)
    try:
        fetch_naver_trades(naver_with([it]), CXN, AREAN)
        check(f"naver: '{fld}' 누락 → schema_changed", False, "no error")
    except CollectorError as e:
        check(f"naver: '{fld}' 누락 → schema_changed", e.stage == "schema_changed", str(e))
it = dict(base); it.pop("dealPrice")
try:
    fetch_naver_trades(naver_with([it]), CXN, AREAN)
    check("naver: 가격 필드 모두 누락 → schema_changed", False, "no error")
except CollectorError as e:
    check("naver: 가격 필드 모두 누락 → schema_changed", e.stage == "schema_changed", str(e))
it = dict(base); it["tradeType"] = "B1"; it.pop("floor")
try:
    fetch_naver_trades(naver_with([it]), CXN, AREAN)
    check("naver: 전세 항목도 floor 누락이면 schema_changed", False, "no error")
except CollectorError as e:
    check("naver: 전세 항목도 floor 누락이면 schema_changed", e.stage == "schema_changed", str(e))
cl = naver_with([dict(base, floor="옥탑")])
nt = fetch_naver_trades(cl, CXN, AREAN)
check("naver: 파싱 불가 층은 제외 + 경고", nt == [] and any("층 값 오류 1건" in x for x in cl.warnings), str(cl.warnings))

fails = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
sys.exit(1 if fails else 0)

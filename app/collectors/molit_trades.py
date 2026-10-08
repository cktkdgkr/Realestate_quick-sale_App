"""국토교통부 아파트 매매 실거래가 API 수집기. 소유: trade-collector.

규칙 출처: CLAUDE.md §3·§7·§8·§10, .claude/skills/molit-trade-api/SKILL.md

- 조회 범위: as_of가 속한 달을 포함해 25개 월 (기간 필터는 판정 단계에서 정확히 한다).
- (LAWD_CD, DEAL_YMD) 단위로 MolitClient 인스턴스 안에 캐시한다 (실행 1회 = 클라이언트 1개).
- totalCount까지 페이지네이션한다.
- 해제 거래는 버리지 않고 Trade.cancelled=True로 남긴다.
- 단지 식별은 Complex.molit_apt_seq(=응답 aptSeq)로만 한다. 단지명 비교에 의존하지 않는다.
- 평형 매칭 실패 경고는 MolitClient.warnings에 쌓인다 (pipeline이 drain_warnings()로 가져간다).
- API 키는 로그·예외 메시지에 넣지 않는다 (mask_secrets, httpx 로거 필터).
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date
from typing import Any, Callable
from urllib.parse import quote, quote_plus

import httpx

from app.collectors.errors import CollectorError
from app.domain import normalize as nz
from app.domain.models import AreaType, Complex, Trade

log = logging.getLogger(__name__)

ENDPOINT = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
MONTHS_TO_FETCH = 25          # 실행월 포함 25개 월 (= 최근 24개월 + 월 경계 여유)
NUM_OF_ROWS = 1000
TIMEOUT_S = 15.0
REQUEST_INTERVAL_S = 1.0      # 국토부 요청 간 대기 (순차 실행)
RETRY_BACKOFF_S = (5.0, 15.0, 45.0)  # 네트워크 오류·5xx 재시도 대기 (최대 3회)
AREA_MATCH_TOLERANCE = 0.5    # CLAUDE.md §3.1 (36평 초과 추정 판정에만 사용)
MAX_PAGES = 100               # 무한 루프 방지

OK_RESULT_CODES = frozenset({"00", "000"})
# data.go.kr 공통 오류 코드 중 인증 계열: 20 접근 거부, 30 미등록 키, 31 기한 만료, 32 미등록 IP
AUTH_RESULT_CODES = frozenset({"20", "30", "31", "32"})
AUTH_MESSAGE_HINTS = ("SERVICE_KEY", "SERVICEKEY", "UNAUTHORIZED", "ACCESS_DENIED")

_SERVICE_KEY_RE = re.compile(r"(serviceKey=)[^&\s\"'<>]+", re.IGNORECASE)


def mask_secrets(text: str, api_key: str | None = None) -> str:
    """URL·메시지에서 serviceKey 값과 API 키 원문(및 URL 인코딩형)을 가린다."""
    out = _SERVICE_KEY_RE.sub(r"\1***", text)
    if api_key:
        for variant in {api_key, quote(api_key, safe=""), quote_plus(api_key)}:
            if variant:
                out = out.replace(variant, "***")
    return out


class _MaskServiceKeyFilter(logging.Filter):
    """httpx가 INFO 로그로 남기는 요청 URL에서 serviceKey를 가린다."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 포맷 실패 로그는 그대로 통과
            return True
        if "serviceKey" in msg or "servicekey" in msg.lower():
            record.msg = mask_secrets(msg)
            record.args = None
        return True


_MASK_FILTER = _MaskServiceKeyFilter()


def _install_log_mask() -> None:
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        if _MASK_FILTER not in lg.filters:
            lg.addFilter(_MASK_FILTER)


# ---------------------------------------------------------------- 날짜
def months_back(as_of: date, n: int = MONTHS_TO_FETCH) -> list[str]:
    """as_of가 속한 달부터 과거로 n개 월의 YYYYMM 목록 (최신 → 과거)."""
    y, m = as_of.year, as_of.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


# ---------------------------------------------------------------- 클라이언트
class MolitClient:
    """국토부 실거래 API 클라이언트. 요청은 순차, 응답은 (LAWD_CD, DEAL_YMD) 단위로 캐시."""

    def __init__(self, api_key: str, http: httpx.Client | None = None, sleep=time.sleep) -> None:
        if not api_key:
            raise CollectorError("molit_auth", "MOLIT_API_KEY가 비어 있다")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT_S)
        self._sleep: Callable[[float], Any] = sleep
        self._cache: dict[tuple[str, str], list[dict[str, str]]] = {}
        self._requested_once = False
        self.request_count = 0
        self.warnings: list[str] = []
        _install_log_mask()

    def __repr__(self) -> str:  # 키가 repr로 새지 않게
        return f"MolitClient(cached_months={len(self._cache)})"

    def drain_warnings(self) -> list[str]:
        """쌓인 경고를 꺼내고 비운다 (pipeline이 cross_check_warnings 등에 합친다)."""
        out, self.warnings = self.warnings, []
        return out

    # -- 공개: 월 단위 조회 (캐시) --
    def get_month(self, lawd_cd: str, deal_ymd: str) -> list[dict[str, str]]:
        key = (lawd_cd, deal_ymd)
        if key in self._cache:
            return self._cache[key]
        items: list[dict[str, str]] = []
        page = 1
        while True:
            total, page_items = self._fetch_page(lawd_cd, deal_ymd, page)
            items.extend(page_items)
            if len(items) >= total or not page_items:
                if len(items) < total:
                    raise CollectorError(
                        "molit_api",
                        f"{lawd_cd}/{deal_ymd}: totalCount={total}인데 {len(items)}건만 받음",
                    )
                break
            page += 1
            if page > MAX_PAGES:
                raise CollectorError("molit_api", f"{lawd_cd}/{deal_ymd}: 페이지 수 상한 초과")
        self._cache[key] = items
        return items

    # -- 내부 --
    def _fetch_page(self, lawd_cd: str, deal_ymd: str, page: int) -> tuple[int, list[dict[str, str]]]:
        text = self._request(
            {
                "serviceKey": self._api_key,
                "LAWD_CD": lawd_cd,
                "DEAL_YMD": deal_ymd,
                "pageNo": str(page),
                "numOfRows": str(NUM_OF_ROWS),
            },
            where=f"{lawd_cd}/{deal_ymd} p{page}",
        )
        return parse_response(text, where=f"{lawd_cd}/{deal_ymd} p{page}")

    def _request(self, params: dict[str, str], where: str) -> str:
        if self._requested_once:
            self._sleep(REQUEST_INTERVAL_S)
        self._requested_once = True

        last_problem = ""
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            if attempt:
                self._sleep(RETRY_BACKOFF_S[attempt - 1])
            self.request_count += 1
            try:
                resp = self._http.get(ENDPOINT, params=params, timeout=TIMEOUT_S)
            except httpx.HTTPError as e:  # 연결·타임아웃 등 → 재시도
                last_problem = f"{type(e).__name__}: {mask_secrets(str(e), self._api_key)}"
                log.warning("국토부 요청 실패 %s (시도 %d): %s", where, attempt + 1, last_problem)
                continue
            if resp.status_code in (401, 403):
                raise CollectorError("molit_auth", f"{where}: HTTP {resp.status_code} (인증키 확인 필요)")
            if resp.status_code >= 500 or resp.status_code == 429:
                last_problem = f"HTTP {resp.status_code}"
                log.warning("국토부 응답 오류 %s (시도 %d): %s", where, attempt + 1, last_problem)
                continue
            if resp.status_code != 200:
                raise CollectorError("molit_api", f"{where}: HTTP {resp.status_code}")
            return resp.text
        raise CollectorError("network", f"국토부 {where}: 재시도 {len(RETRY_BACKOFF_S)}회 후 실패 ({last_problem})")


# ---------------------------------------------------------------- 응답 파싱
def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_response(text: str, where: str = "") -> tuple[int, list[dict[str, str]]]:
    """XML 응답 → (totalCount, item dict 목록). 오류 응답이면 CollectorError."""
    body = text.strip()
    if not body.startswith("<"):
        upper = body.upper()
        if any(h in upper for h in AUTH_MESSAGE_HINTS):
            raise CollectorError("molit_auth", f"{where}: 인증 오류 응답 ({mask_secrets(body[:80])})")
        raise CollectorError("molit_api", f"{where}: XML이 아닌 응답 ({mask_secrets(body[:80])})")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise CollectorError("molit_api", f"{where}: XML 파싱 실패 ({e})") from None

    # data.go.kr 게이트웨이 공통 오류 형식
    if root.tag == "OpenAPI_ServiceResponse" or root.find(".//cmmMsgHeader") is not None:
        code = _text(root.find(".//returnReasonCode"))
        msg = _text(root.find(".//returnAuthMsg")) or _text(root.find(".//errMsg"))
        stage = "molit_auth" if code in AUTH_RESULT_CODES or any(
            h in msg.upper() for h in AUTH_MESSAGE_HINTS) else "molit_api"
        raise CollectorError(stage, f"{where}: {code} {mask_secrets(msg)}".strip())

    code = _text(root.find("./header/resultCode")) or _text(root.find(".//resultCode"))
    msg = _text(root.find("./header/resultMsg")) or _text(root.find(".//resultMsg"))
    if not code:
        raise CollectorError("schema_changed", f"{where}: resultCode 없음")
    if code not in OK_RESULT_CODES:
        stage = "molit_auth" if code in AUTH_RESULT_CODES or any(
            h in msg.upper() for h in AUTH_MESSAGE_HINTS) else "molit_api"
        raise CollectorError(stage, f"{where}: resultCode={code} {mask_secrets(msg)}".strip())

    total_raw = _text(root.find(".//body/totalCount"))
    try:
        total = int(total_raw) if total_raw else 0
    except ValueError:
        raise CollectorError("schema_changed", f"{where}: totalCount={total_raw!r}") from None
    items = [
        {child.tag: (child.text or "").strip() for child in item}
        for item in root.findall(".//body/items/item")
    ]
    return total, items


# ---------------------------------------------------------------- 단일 거래 변환
_REQUIRED = ("aptSeq", "excluUseAr", "dealAmount", "dealYear", "dealMonth", "dealDay")


def _to_trade(item: dict[str, str], complex_no: str, at: AreaType, floor: int) -> Trade:
    try:
        price = nz.parse_price(item["dealAmount"])
        contract = date(int(item["dealYear"]), int(item["dealMonth"]), int(item["dealDay"]))
    except (ValueError, KeyError) as e:
        raise CollectorError(
            "schema_changed", f"국토부 거래 필드 변환 실패: {type(e).__name__} {e}", complex_no
        ) from None
    group = nz.classify_floor(floor)
    if group not in ("LOW", "NORMAL"):  # int 입력이면 항상 LOW/NORMAL
        raise CollectorError("schema_changed", f"국토부 층 분류 실패: {floor!r}", complex_no)
    return Trade(
        complex_no=complex_no,
        area_key=at.area_key,
        exclusive_m2=float(item["excluUseAr"]),
        floor=floor,
        floor_group=group,
        price=price,
        contract_date=contract,
        cancelled=item.get("cdealType", "").strip().upper() == "O",
        deal_type=item.get("dealingGbn", "").strip(),
        source="MOLIT",
    )


# ---------------------------------------------------------------- 공개 함수
def fetch_trades(client: MolitClient, complex: Complex, area_types: list[AreaType], as_of: date) -> list[Trade]:
    """단지의 25개월 매매 실거래 (해제 거래 포함). 평형 매칭 실패는 client.warnings에 기록."""
    if not complex.molit_apt_seq:
        raise CollectorError(
            "complex_mapping",
            "molit_apt_seq 미설정: find_apt_seq_candidates로 후보를 확인해 complexes.yaml에 지정해야 한다",
            complex.complex_no,
        )
    if not complex.lawd_cd or not re.fullmatch(r"\d{5}", complex.lawd_cd[:5]):
        raise CollectorError("complex_mapping", f"lawd_cd 형식 오류: {complex.lawd_cd!r}", complex.complex_no)
    lawd = complex.lawd_cd[:5]
    seq = complex.molit_apt_seq.strip()
    max_key = max((at.area_key for at in area_types), default=None)

    trades: list[Trade] = []
    unmatched: Counter[float] = Counter()
    oversize = 0
    bad_floor = 0
    for ymd in months_back(as_of):
        try:
            month_items = client.get_month(lawd, ymd)
        except CollectorError as e:
            if e.complex_no is None:
                e.complex_no = complex.complex_no
            raise
        for item in month_items:
            if item.get("aptSeq", "").strip() != seq:
                continue
            missing = [f for f in _REQUIRED if not item.get(f, "").strip()]
            if missing:
                raise CollectorError("schema_changed", f"국토부 응답 필드 누락: {missing}", complex.complex_no)
            try:
                excl = float(item["excluUseAr"])
            except ValueError:
                raise CollectorError(
                    "schema_changed", f"excluUseAr 숫자 아님: {item['excluUseAr']!r}", complex.complex_no
                ) from None
            at = nz.match_area(excl, area_types)
            if at is None:
                if max_key is None or round(excl - max_key, 2) > AREA_MATCH_TOLERANCE:
                    oversize += 1  # 36평 초과(조사 대상 밖) 평형 → 경고 없이 제외
                else:
                    unmatched[round(excl, 2)] += 1
                continue
            floor_raw = item.get("floor", "").strip()
            try:
                floor = int(floor_raw)
            except ValueError:
                bad_floor += 1
                continue
            trades.append(_to_trade(item, complex.complex_no, at, floor))

    if unmatched:
        detail = ", ".join(f"{a}㎡ {n}건" for a, n in sorted(unmatched.items()))
        msg = (f"[WARN] 단지 {complex.complex_no} 국토부 실거래 평형 매칭 실패 "
               f"{sum(unmatched.values())}건 제외 (전용 {detail})")
        client.warnings.append(msg)
        log.warning(msg)
    if bad_floor:
        msg = f"[WARN] 단지 {complex.complex_no} 국토부 실거래 층 값 오류 {bad_floor}건 제외"
        client.warnings.append(msg)
        log.warning(msg)
    if oversize:
        log.debug("단지 %s: 조사 대상 밖(36평 초과 추정) 실거래 %d건 제외", complex.complex_no, oversize)

    trades.sort(key=lambda t: (t.area_key, t.contract_date, t.floor, t.price))
    return trades


def _norm_name(s: str) -> str:
    return re.sub(r"[\s()\-·.,]|아파트", "", s or "")


def find_apt_seq_candidates(client: MolitClient, complex: Complex, as_of: date) -> list[dict]:
    """molit_apt_seq 후보 목록 (자동 확정하지 않는다). 사용자 확인용 참고 점수 순 정렬.

    각 dict: apt_seq, apt_nm, umd_nm, jibun, trade_count, last_contract(YYYY-MM-DD), score, hints
    score는 참고용(법정동 일치 +1, 지번 일치 +2, 단지명 포함 관계 +1)이며 판정에 쓰지 않는다.
    """
    if not complex.lawd_cd or not re.fullmatch(r"\d{5}", complex.lawd_cd[:5]):
        raise CollectorError("complex_mapping", f"lawd_cd 형식 오류: {complex.lawd_cd!r}", complex.complex_no)
    lawd = complex.lawd_cd[:5]
    groups: dict[str, dict] = {}
    for ymd in months_back(as_of):
        for item in client.get_month(lawd, ymd):
            seq = item.get("aptSeq", "").strip()
            if not seq:
                continue
            g = groups.setdefault(seq, {
                "apt_seq": seq, "apt_nm": item.get("aptNm", "").strip(),
                "umd_nm": item.get("umdNm", "").strip(), "jibun": item.get("jibun", "").strip(),
                "trade_count": 0, "last_contract": "",
            })
            g["trade_count"] += 1
            try:
                d = date(int(item["dealYear"]), int(item["dealMonth"]), int(item["dealDay"])).isoformat()
            except (KeyError, ValueError):
                d = ""
            g["last_contract"] = max(g["last_contract"], d)

    addr = complex.address or ""
    addr_tokens = set(re.split(r"\s+", addr.strip()))
    cname = _norm_name(complex.name)
    out = []
    for g in groups.values():
        hints = []
        score = 0
        if g["umd_nm"] and g["umd_nm"] in addr:
            score += 1
            hints.append("법정동 일치")
        if g["jibun"] and g["jibun"] in addr_tokens:
            score += 2
            hints.append("지번 일치")
        gname = _norm_name(g["apt_nm"])
        if cname and gname and (cname in gname or gname in cname):
            score += 1
            hints.append("단지명 유사")
        out.append({**g, "score": score, "hints": hints})
    out.sort(key=lambda g: (-g["score"], -g["trade_count"], g["apt_seq"]))
    return out

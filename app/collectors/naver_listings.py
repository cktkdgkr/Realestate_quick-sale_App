"""네이버 부동산 매매 매물 수집기. 소유: listing-collector.

엔드포인트·필드 이름은 .claude/skills/naver-land-collector §2·§3의 **후보**다.
2026-10-08 현재 이 개발 환경에서 네이버 접속이 막혀 있어 실제 응답으로 확인하지 못했다.
4단계 전에 실제 응답으로 확인·교체해야 한다 (docs/handoff/listing-collector.md).

요청 정책 (CLAUDE.md §8, 스킬 §4):
- 모든 요청은 한 NaverClient를 통해 순차 실행. 요청 사이 2~5초 랜덤 대기.
- 타임아웃 15초, 네트워크 오류·5xx는 5s/15s/45s 백오프로 최대 3회 재시도.
- 401/403/429, 3xx, HTML 응답 → 즉시 CollectorError("blocked"), 이후 같은 클라이언트의 모든 요청 중단.
- 차단 회피 기법(프록시, 지문 조작, 캡차 우회, 토큰 위조)은 쓰지 않는다.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx

from app.collectors.errors import CollectorError
from app.domain.dedup import make_dedup_key
from app.domain.models import AreaType, Complex, Listing
from app.domain.normalize import (
    area_key as to_area_key,
    classify_floor,
    is_target_area,
    match_area,
    parse_price,
    to_pyeong,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://new.land.naver.com"
TIMEOUT_S = 15.0
MIN_WAIT_S = 2.0
MAX_WAIT_S = 5.0
BACKOFF_S = (5.0, 15.0, 45.0)  # 재시도 3회
MAX_PAGES = 200  # 페이지네이션 무한 루프 방지 (넘으면 schema_changed)

DEFAULT_HEADERS = {
    # 스킬 §4: 일반 브라우저와 같은 User-Agent와 Referer만 쓴다. 그 외 위장 금지.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

BLOCKED_STATUS = {401, 403, 429}


class NaverClient:
    """네이버 부동산 JSON 요청 클라이언트. 매물·실거래 수집이 같은 인스턴스를 공유한다.

    sleep, rng를 주입하면 테스트에서 실제로 기다리지 않는다.
    """

    def __init__(self, http: httpx.Client | None = None, sleep: Callable[[float], Any] = time.sleep,
                 rng: Callable[[], float] = random.random) -> None:
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._sleep = sleep
        self._rng = rng
        self._requested_once = False
        self.blocked: CollectorError | None = None
        self.request_count = 0
        self.warnings: list[str] = []  # 실패는 아니지만 리포트에 남길 만한 이상 징후

    # ------------------------------------------------------------ 수명
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> NaverClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 요청
    def _pace(self) -> None:
        """두 번째 요청부터 2~5초 랜덤 대기."""
        if self._requested_once:
            self._sleep(MIN_WAIT_S + (MAX_WAIT_S - MIN_WAIT_S) * self._rng())
        self._requested_once = True

    def _block(self, detail: str, complex_no: str | None) -> CollectorError:
        err = CollectorError("blocked", detail, complex_no)
        self.blocked = err
        logger.error("네이버 차단 감지, 남은 네이버 요청 중단: %s", err)
        return err

    def get_json(self, path: str, params: dict[str, Any] | None = None, *,
                 complex_no: str | None = None) -> Any:
        """GET {BASE_URL}{path} → JSON. 실패는 CollectorError로 올린다 (빈 값 반환 없음)."""
        if self.blocked is not None:
            raise CollectorError("blocked", f"이전 요청에서 차단 감지되어 중단 ({self.blocked.detail})",
                                 complex_no)

        url = f"{BASE_URL}{path}"
        last_problem = ""
        for attempt in range(len(BACKOFF_S) + 1):
            if attempt > 0:
                wait = BACKOFF_S[attempt - 1]
                logger.warning("네이버 요청 재시도 %d/%d (%.0fs 후): %s %s",
                               attempt, len(BACKOFF_S), wait, path, last_problem)
                self._sleep(wait)
            self._pace()
            self.request_count += 1
            try:
                resp = self._http.get(url, params=params, headers=DEFAULT_HEADERS, timeout=TIMEOUT_S)
            except httpx.TransportError as e:
                last_problem = f"{type(e).__name__}: {e}"
                continue

            status = resp.status_code
            if status in BLOCKED_STATUS:
                raise self._block(f"HTTP {status} ({path})", complex_no)
            if 300 <= status < 400:
                raise self._block(f"HTTP {status} 리다이렉트 ({path})", complex_no)
            if status >= 500:
                last_problem = f"HTTP {status}"
                continue
            if status != 200:
                raise CollectorError("network", f"HTTP {status} ({path})", complex_no)

            ctype = resp.headers.get("content-type", "").lower()
            body = resp.text
            if "html" in ctype or body.lstrip().startswith("<"):
                raise self._block(f"HTML 응답(캡차·차단 페이지 의심) ({path})", complex_no)
            try:
                return resp.json()
            except ValueError as e:
                raise CollectorError("schema_changed", f"JSON 파싱 실패 ({path}): {e}", complex_no) from e

        raise CollectorError("network", f"재시도 {len(BACKOFF_S)}회 후 실패 ({path}): {last_problem}",
                             complex_no)


# ---------------------------------------------------------------- 응답 파싱 보조
def _require(obj: Any, key: str, where: str, complex_no: str) -> Any:
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise CollectorError("schema_changed", f"{where}에 필수 필드 '{key}' 없음", complex_no)
    return obj[key]


def _to_float(v: Any, field: str, complex_no: str) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError) as e:
        raise CollectorError("schema_changed", f"{field} 숫자 변환 실패: {v!r}", complex_no) from e


def _parse_ymd(v: Any) -> date | None:
    if not v:
        return None
    try:
        return datetime.strptime(str(v).strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _to_int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def article_url(complex_no: str, article_no: str) -> str:
    return f"{BASE_URL}/complexes/{complex_no}?articleNo={article_no}"


# ---------------------------------------------------------------- 단지·평형
def fetch_complex(client: NaverClient, complex_no: str) -> tuple[Complex, list[AreaType]]:
    """단지 정보와 공급 ≤119.0㎡ 평형 목록. 대상 평형이 0개면 빈 목록 (정상)."""
    data = client.get_json(f"/api/complexes/{complex_no}", {"sameAddressGroup": "false"},
                           complex_no=complex_no)
    detail = _require(data, "complexDetail", "단지 응답", complex_no)
    resp_no = str(_require(detail, "complexNo", "complexDetail", complex_no))
    if resp_no != str(complex_no):
        raise CollectorError("schema_changed", f"요청 단지 {complex_no}와 응답 단지 {resp_no}가 다름",
                             complex_no)
    name = str(_require(detail, "complexName", "complexDetail", complex_no))
    cortar = str(_require(detail, "cortarNo", "complexDetail", complex_no))
    if len(cortar) < 5 or not cortar[:5].isdigit():
        raise CollectorError("schema_changed", f"cortarNo 형식 이상: {cortar!r}", complex_no)

    complex_ = Complex(
        complex_no=str(complex_no),
        name=name,
        lawd_cd=cortar[:5],
        address=str(detail.get("address") or detail.get("roadAddress") or ""),
        max_floor=_to_int_or_none(detail.get("highFloor")),
        molit_apt_seq=None,  # 네이버 응답에는 없다. config/complexes.yaml에서 채운다
    )

    pyeongs = _require(data, "complexPyeongDetailList", "단지 응답", complex_no)
    if not isinstance(pyeongs, list):
        raise CollectorError("schema_changed", "complexPyeongDetailList가 목록이 아님", complex_no)

    area_types: list[AreaType] = []
    seen: dict[float, AreaType] = {}
    for p in pyeongs:
        supply = _to_float(_require(p, "supplyArea", "평형", complex_no), "supplyArea", complex_no)
        exclusive = _to_float(_require(p, "exclusiveArea", "평형", complex_no), "exclusiveArea", complex_no)
        type_name = str(_require(p, "pyeongName", "평형", complex_no))
        if not is_target_area(supply):
            continue
        key = to_area_key(exclusive)
        if key in seen:
            msg = (f"[{complex_no}] 전용 {key}㎡ 평형이 여러 개 ({seen[key].type_name}, {type_name}). "
                   f"첫 번째만 사용")
            logger.warning(msg)
            client.warnings.append(msg)
            continue
        at = AreaType(complex_no=str(complex_no), area_key=key, exclusive_m2=exclusive,
                      supply_m2=supply, pyeong=to_pyeong(supply), type_name=type_name)
        seen[key] = at
        area_types.append(at)

    area_types.sort(key=lambda a: a.area_key)
    return complex_, area_types


# ---------------------------------------------------------------- 매물
def _match_article_area(art: dict, area_types: list[AreaType], complex_no: str) -> AreaType | None:
    """매물의 평형을 찾는다. None이면 대상 외 평형.

    1) areaName == AreaType.type_name (평형명 일치)
    2) area1(공급)이 있고 119.0㎡ 초과면 대상 외
    3) area2(전용)를 ±0.5㎡로 match_area
    """
    area_name = art.get("areaName")
    if area_name:
        for at in area_types:
            if at.type_name == str(area_name):
                return at
    area1, area2 = art.get("area1"), art.get("area2")
    if area1 is None and area2 is None and not area_name:
        raise CollectorError("schema_changed", f"매물 {art.get('articleNo')}에 면적 필드 없음", complex_no)
    if area1 is not None and not is_target_area(_to_float(area1, "area1", complex_no)):
        return None
    if area2 is not None:
        return match_area(_to_float(area2, "area2", complex_no), area_types)
    return None


def fetch_listings(client: NaverClient, complex: Complex, area_types: list[AreaType]) -> list[Listing]:
    """매매 매물 전체 (페이지 끝까지). dedup 전 원본. 대상 평형 매물만 반환.

    0건이면 [] (정상). 실패는 CollectorError.
    """
    complex_no = complex.complex_no
    if not area_types:
        return []

    listings: list[Listing] = []
    seen_articles: set[str] = set()
    unmatched = 0
    page = 1
    while True:
        if page > MAX_PAGES:
            raise CollectorError("schema_changed", f"페이지가 {MAX_PAGES}을 넘음 (isMoreData 종료 안 됨)",
                                 complex_no)
        data = client.get_json(
            f"/api/articles/complex/{complex_no}",
            {"realEstateType": "APT", "tradeType": "A1", "page": page,
             "sameAddressGroup": "false", "order": "prc"},
            complex_no=complex_no,
        )
        articles = _require(data, "articleList", f"매물 응답 page={page}", complex_no)
        more = _require(data, "isMoreData", f"매물 응답 page={page}", complex_no)
        if not isinstance(articles, list) or not isinstance(more, bool):
            raise CollectorError("schema_changed", f"매물 응답 page={page} 형식 이상", complex_no)

        for art in articles:
            article_no = str(_require(art, "articleNo", "매물", complex_no))
            if article_no in seen_articles:
                continue  # 페이지 경계에서 같은 매물이 다시 오는 경우
            seen_articles.add(article_no)
            price_raw = _require(art, "dealOrWarrantPrc", f"매물 {article_no}", complex_no)
            at = _match_article_area(art, area_types, complex_no)
            if at is None:
                a1 = art.get("area1")
                if a1 is None or is_target_area(_to_float(a1, "area1", complex_no)):
                    unmatched += 1
                continue
            try:
                price = parse_price(str(price_raw))
            except ValueError as e:
                raise CollectorError("schema_changed", f"매물 {article_no} 가격 파싱 실패: {price_raw!r}",
                                     complex_no) from e

            floor_raw = str(art.get("floorInfo") or "").strip()
            dong = str(art.get("buildingName") or "").replace(" ", "")
            direction = str(art.get("direction") or "").strip()
            listings.append(Listing(
                article_no=article_no,
                complex_no=complex_no,
                area_key=at.area_key,
                dong=dong,
                floor_raw=floor_raw,
                floor_group=classify_floor(floor_raw),
                direction=direction,
                price=price,
                confirmed_at=_parse_ymd(art.get("articleConfirmYmd")),
                realtor_count=1,
                alt_prices=[],
                dedup_key=make_dedup_key(complex_no, at.area_key, dong, floor_raw, direction),
                url=article_url(complex_no, article_no),
            ))

        if not more:
            break
        page += 1

    if unmatched:
        msg = f"[{complex_no}] 평형을 특정하지 못한 매물 {unmatched}건 제외 (공급 119㎡ 이하 추정)"
        logger.warning(msg)
        client.warnings.append(msg)
    logger.info("[%s] 매매 매물 %d건 수집 (%d페이지)", complex_no, len(listings), page)
    return listings

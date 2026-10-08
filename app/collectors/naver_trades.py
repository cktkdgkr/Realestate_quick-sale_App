"""네이버 실거래 탭 수집기와 국토부 교차검증. 소유: trade-collector.

규칙 출처: CLAUDE.md §7·§10, .claude/skills/molit-trade-api §6, naver-land-collector §2·§4

- 네이버 실거래는 **교차검증 전용**이다. 판정(rules.judge)에는 국토부(MOLIT) Trade만 넘긴다.
- 엔드포인트·필드는 스킬 문서의 후보로 만든 가정이다 (4단계에서 실제 응답으로 확인, handoff 참고).
- 요청은 모두 NaverClient.get_json(순차, 2~5초 대기, 재시도, 차단 감지)을 거친다.
- 실패는 CollectorError로 올린다. pipeline은 fetch_naver_trades_or_warning으로 경고만 남기고 계속 진행할 수 있다.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any

from app.collectors.errors import CollectorError
from app.domain import normalize as nz
from app.domain.models import AreaType, Complex, Trade

if TYPE_CHECKING:
    from app.collectors.naver_listings import NaverClient

log = logging.getLogger(__name__)

TRADE_TYPE_SALE = "A1"
REAL_PRICE_YEARS = 5
RECENT_MONTHS_INFO = 2      # 국토부에만 있는 거래가 이 기간 안이면 네이버 반영 지연 가능 → INFO
WINDOW_MONTHS = 25          # 국토부 조회 범위와 같게 (실행월 포함 25개 월)


# ---------------------------------------------------------------- 수집
def _require(obj: Any, key: str, where: str, complex_no: str) -> Any:
    if not isinstance(obj, dict) or obj.get(key) is None:
        raise CollectorError("schema_changed", f"네이버 실거래 {where}에 필수 필드 '{key}' 없음", complex_no)
    return obj[key]


def _area_numbers(client: NaverClient, complex: Complex, area_types: list[AreaType]) -> dict[float, str]:
    """AreaType(type_name=pyeongName) → 네이버 평형번호(pyeongNo, 실거래 요청의 areaNo)."""
    cno = complex.complex_no
    data = client.get_json(f"/api/complexes/{cno}", {"sameAddressGroup": "false"}, complex_no=cno)
    pyeongs = _require(data, "complexPyeongDetailList", "단지 응답", cno)
    if not isinstance(pyeongs, list):
        raise CollectorError("schema_changed", "complexPyeongDetailList가 목록이 아님", cno)
    by_name = {str(_require(p, "pyeongName", "평형", cno)): str(_require(p, "pyeongNo", "평형", cno))
               for p in pyeongs}
    out: dict[float, str] = {}
    for at in area_types:
        if at.type_name not in by_name:
            raise CollectorError("schema_changed", f"평형 '{at.type_name}'의 pyeongNo를 찾지 못함", cno)
        out[at.area_key] = by_name[at.type_name]
    return out


def _parse_item(it: dict, at: AreaType, cno: str) -> Trade | None:
    """realPriceList 항목 → Trade. 매매가 아니면 None. 층이 숫자가 아니면 None(호출부에서 경고)."""
    if str(it.get("tradeType", TRADE_TYPE_SALE)) != TRADE_TYPE_SALE:
        return None
    try:
        y = int(_require(it, "tradeYear", "거래", cno))
        m = int(_require(it, "tradeMonth", "거래", cno))
        d = int(_require(it, "tradeDate", "거래", cno))
        contract = date(y, m, d)
    except ValueError as e:
        raise CollectorError("schema_changed", f"네이버 실거래 계약일 변환 실패: {e}", cno) from None
    raw_price = it.get("dealPrice")
    if raw_price is None:
        raw_price = _require(it, "formattedPrice", "거래", cno)
    try:
        price = raw_price if isinstance(raw_price, int) and not isinstance(raw_price, bool) \
            else nz.parse_price(str(raw_price))
    except ValueError:
        raise CollectorError("schema_changed", f"네이버 실거래 가격 변환 실패: {raw_price!r}", cno) from None
    try:
        floor = int(str(it.get("floor", "")).strip())
    except ValueError:
        return None
    group = nz.classify_floor(floor)
    excl_raw = it.get("exclusiveArea")
    try:
        excl = float(excl_raw) if excl_raw not in (None, "") else at.exclusive_m2
    except (TypeError, ValueError):
        excl = at.exclusive_m2
    return Trade(
        complex_no=cno, area_key=at.area_key, exclusive_m2=excl, floor=floor,
        floor_group=group,  # int 입력이므로 LOW/NORMAL
        price=price, contract_date=contract,
        cancelled=str(it.get("deleteYn", "N")).upper() == "Y",
        deal_type=str(it.get("dealingGbn") or ""), source="NAVER",
    )


def fetch_naver_trades(client: NaverClient, complex: Complex, area_types: list[AreaType]) -> list[Trade]:
    """네이버 실거래 탭의 매매 실거래 (교차검증용). 0건이면 [] (정상), 실패는 CollectorError."""
    cno = complex.complex_no
    if not area_types:
        return []
    area_nos = _area_numbers(client, complex, area_types)
    trades: list[Trade] = []
    bad_floor = 0
    for at in area_types:
        data = client.get_json(
            f"/api/complexes/{cno}/prices/real",
            {"complexNo": cno, "tradeType": TRADE_TYPE_SALE, "areaNo": area_nos[at.area_key],
             "year": str(REAL_PRICE_YEARS), "type": "table"},
            complex_no=cno,
        )
        months = _require(data, "realPriceOnMonthList", "응답", cno)
        if not isinstance(months, list):
            raise CollectorError("schema_changed", "realPriceOnMonthList가 목록이 아님", cno)
        for mon in months:
            items = _require(mon, "realPriceList", "월 묶음", cno)
            if not isinstance(items, list):
                raise CollectorError("schema_changed", "realPriceList가 목록이 아님", cno)
            for it in items:
                t = _parse_item(it, at, cno)
                if t is None:
                    if str(it.get("tradeType", TRADE_TYPE_SALE)) == TRADE_TYPE_SALE:
                        bad_floor += 1
                    continue
                trades.append(t)
    if bad_floor:
        msg = f"[WARN] 단지 {cno} 네이버 실거래 층 값 오류 {bad_floor}건 교차검증에서 제외"
        log.warning(msg)
        client.warnings.append(msg)
    trades.sort(key=lambda t: (t.area_key, t.contract_date, t.floor, t.price))
    return trades


def fetch_naver_trades_or_warning(client: NaverClient, complex: Complex,
                                  area_types: list[AreaType]) -> tuple[list[Trade] | None, list[str]]:
    """pipeline용: 네이버 실거래 실패는 교차검증만 생략하고 경고로 남긴다 (판정에는 영향 없음).

    반환 (trades, warnings). 실패 시 trades=None — 빈 목록과 구분해 cross_check를 호출하지 않는다.
    """
    try:
        return fetch_naver_trades(client, complex, area_types), []
    except CollectorError as e:
        msg = f"[WARN] 단지 {complex.complex_no} 네이버 실거래 수집 실패({e.stage}) — 교차검증 생략: {e.detail}"
        log.warning(msg)
        return None, [msg]


# ---------------------------------------------------------------- 교차검증
def _add_months(d: date, delta: int) -> date:
    idx = d.year * 12 + (d.month - 1) + delta
    y, m = divmod(idx, 12)
    m += 1
    days = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days))


def _fmt(price: int) -> str:
    return nz.format_price(price)


def _where(k: tuple[str, float, str, int]) -> str:
    cno, ak, ym, floor = k
    return f"단지 {cno} 전용 {ak}㎡ {ym} {floor}층"


def cross_check(molit: list[Trade], naver: list[Trade], as_of: date) -> list[str]:
    """국토부·네이버 실거래 비교 경고 (cross_check_warnings). 판정에는 영향 없음.

    비교 범위: 국토부 조회 범위(as_of 달 포함 25개 월의 첫날 ~ as_of), 해제 거래 제외.
    짝짓기 키: (단지, area_key, 계약년월, 층). 같은 키·같은 가격이면 같은 거래로 본다.
    경고 3종:
      MOLIT_ONLY  국토부에만 있음. 계약일이 최근 2개월 이내면 [INFO](네이버 반영 지연 가능), 그 외 [WARN]
      NAVER_ONLY  네이버에만 있음 [WARN] (국토부 매칭 실패·단지 식별 오류 의심)
      PRICE_DIFF  같은 키인데 가격이 다름 [WARN]
    """
    start = _add_months(as_of.replace(day=1), -(WINDOW_MONTHS - 1))
    recent_from = _add_months(as_of, -RECENT_MONTHS_INFO)

    def in_window(t: Trade) -> bool:
        return start <= t.contract_date <= as_of

    def key(t: Trade) -> tuple[str, float, str, int]:
        return (t.complex_no, t.area_key, f"{t.contract_date:%Y-%m}", t.floor)

    m_groups: dict[tuple, list[Trade]] = defaultdict(list)
    n_groups: dict[tuple, list[Trade]] = defaultdict(list)
    m_cancelled: dict[tuple, list[int]] = defaultdict(list)
    for t in molit:
        if t.source != "MOLIT":
            raise ValueError("cross_check: molit 목록에 MOLIT 이외 거래가 섞임")
        if not in_window(t):
            continue
        if t.cancelled:
            m_cancelled[key(t)].append(t.price)
        else:
            m_groups[key(t)].append(t)
    for t in naver:
        if t.source != "NAVER":
            raise ValueError("cross_check: naver 목록에 NAVER 이외 거래가 섞임")
        if in_window(t) and not t.cancelled:
            n_groups[key(t)].append(t)

    rows: list[tuple[tuple, int, str]] = []  # (정렬키, 순번, 메시지)
    for k in sorted(set(m_groups) | set(n_groups)):
        ms = sorted(m_groups.get(k, []), key=lambda t: (t.price, t.contract_date))
        ns = sorted(n_groups.get(k, []), key=lambda t: (t.price, t.contract_date))
        # 1) 같은 가격끼리 짝짓기
        rest_m: list[Trade] = []
        pool = list(ns)
        for t in ms:
            hit = next((i for i, n in enumerate(pool) if n.price == t.price), None)
            if hit is None:
                rest_m.append(t)
            else:
                pool.pop(hit)
        rest_n = pool
        # 2) 남은 것끼리 가격 불일치로 짝짓기
        for a, b in zip(rest_m, rest_n):
            rows.append((k, 0, f"[WARN] 교차검증 가격 불일치: {_where(k)} 국토부 {_fmt(a.price)} / "
                               f"네이버 {_fmt(b.price)} (판정은 국토부 기준)"))
        for a in rest_m[len(rest_n):]:
            if a.contract_date >= recent_from:
                rows.append((k, 1, f"[INFO] 교차검증 국토부에만 있는 최근 거래: {_where(k)} {_fmt(a.price)} "
                                   f"(계약 {a.contract_date.isoformat()}, 네이버 반영 지연 가능)"))
            else:
                rows.append((k, 1, f"[WARN] 교차검증 국토부에만 있는 거래: {_where(k)} {_fmt(a.price)} "
                                   f"(계약 {a.contract_date.isoformat()})"))
        for b in rest_n[len(rest_m):]:
            note = " — 국토부에서는 해제 거래" if b.price in m_cancelled.get(k, []) else \
                " (국토부 평형 매칭 실패 또는 단지 식별 오류 의심)"
            rows.append((k, 2, f"[WARN] 교차검증 네이버에만 있는 거래: {_where(k)} {_fmt(b.price)} "
                               f"(계약 {b.contract_date.isoformat()}){note}"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[2] for r in rows]

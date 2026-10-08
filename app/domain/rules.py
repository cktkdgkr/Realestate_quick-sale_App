"""급매 판정 규칙 (순수 함수, I/O·현재 시각 호출 금지). 담당: 급매 판정 agent.

근거: CLAUDE.md §4, §10 / .claude/skills/bargain-rules/SKILL.md
- 입력은 한 단지 × 한 area_key 분량이고, 매물은 dedup이 끝난 대표 매물이다.
- 가격 비교는 정수 곱셈만 쓴다 (§4.3). float는 표시용 discount_pct 계산에만 쓴다.
- 기준일 as_of는 인자로만 받는다.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Literal

from app.domain.models import Listing, Trade, Verdict
from app.domain.normalize import area_key as _norm_area_key

# §4.3 비율(%) — price * 100 <= base * RATIO
NORMAL_RATIO = 95
LOW_RATIO = 90
TRADE_WINDOW_MONTHS = 24
TRADE_SAMPLE_SIZE = 3

Reason = Literal["TRADE", "LISTING"]


# ---------------------------------------------------------------------------
# 내부 보조 함수
# ---------------------------------------------------------------------------
def _minus_months(d: date, months: int) -> date:
    """dateutil.relativedelta(months=n)와 같은 결과 (말일 초과 시 그 달 말일로 맞춤)."""
    total = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _window_start(as_of: date) -> date:
    return _minus_months(as_of, TRADE_WINDOW_MONTHS)


def _validate_scope(listings: list[Listing], trades: list[Trade]) -> None:
    """한 단지 × 한 area_key 범위인지, 대표 매물 dedup_key가 겹치지 않는지 검사."""
    complex_nos = {x.complex_no for x in listings} | {t.complex_no for t in trades}
    if len(complex_nos) > 1:
        raise ValueError(f"judge 입력에 여러 단지가 섞여 있음: {sorted(complex_nos)}")
    # CLAUDE.md §9: area_key는 normalize.area_key로 반올림한 값끼리 비교한다
    area_keys = {_norm_area_key(x.area_key) for x in listings} | {
        _norm_area_key(t.area_key) for t in trades
    }
    if len(area_keys) > 1:
        raise ValueError(f"judge 입력에 여러 area_key가 섞여 있음: {sorted(area_keys)}")
    seen: set[str] = set()
    for x in listings:
        if x.dedup_key in seen:
            raise ValueError(f"대표 매물이 아닌 입력 (dedup_key 중복): {x.dedup_key!r}")
        seen.add(x.dedup_key)


def _trade_sample(trades: list[Trade], as_of: date) -> list[int]:
    """bargain-rules §2: 기준가 표본 가격 (최신순 최대 3건)."""
    start = _window_start(as_of)
    eligible = [
        t
        for t in trades
        if t.floor_group == "NORMAL" and not t.cancelled and start <= t.contract_date <= as_of
    ]
    # 계약일 내림차순, 같은 날이면 가격 내림차순 → 입력 순서와 무관하게 결정적
    eligible.sort(key=lambda t: (t.contract_date, t.price), reverse=True)
    return [t.price for t in eligible[:TRADE_SAMPLE_SIZE]]


def _median_int(prices: list[int]) -> int | None:
    if not prices:
        return None
    s = sorted(prices)
    if len(s) == 1:
        return s[0]
    if len(s) == 2:
        return (s[0] + s[1]) // 2
    return s[1]  # 3건: 가운데 값


def _is_below(price: int, base: int | None, ratio: int) -> bool:
    """§4.3 정수 비교 (경계 포함)."""
    return base is not None and price * 100 <= base * ratio


def _discount_pct(price: int, bases: list[int]) -> float | None:
    """CLAUDE.md §9 rules ④ / bargain-rules §5: 표시 전용 할인율.

    근거 기준가 중 할인율이 더 큰 쪽 = 기준가가 더 큰 쪽 (정수 max로 고른다).
    값 = (base - price) / base * 100 을 정수 연산으로 소수 첫째 자리 ROUND_HALF_UP.
    """
    if not bases:
        return None
    base = max(bases)
    num = (base - price) * 1000  # 0.1% 단위 분자
    if num >= 0:
        tenths = (2 * num + base) // (2 * base)
    else:  # 판정상 급매면 price <= base라 오지 않지만, 대칭 반올림(0에서 멀어지는 쪽)으로 둔다
        tenths = -((-2 * num + base) // (2 * base))
    return tenths / 10


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------
def trade_base(trades: list[Trade], as_of: date) -> tuple[int | None, bool]:
    """(T_normal, trade_sample_short). 표본 0건이면 (None, True)."""
    sample = _trade_sample(trades, as_of)
    return _median_int(sample), len(sample) < TRADE_SAMPLE_SIZE


def judge(listings: list[Listing], trades: list[Trade], as_of: date) -> list[Verdict]:
    """한 단지 × 한 area_key의 대표 매물을 판정한다.

    반환 순서는 입력 순서와 무관하게 (price, dedup_key, article_no) 오름차순이다.
    alert_kind는 항상 None (notifier가 채운다).
    """
    _validate_scope(listings, trades)
    t_normal, short = trade_base(trades, as_of)

    normal_prices = [x.price for x in listings if x.floor_group == "NORMAL"]
    l_normal_all = min(normal_prices) if normal_prices else None

    verdicts: list[Verdict] = []
    for x in sorted(listings, key=lambda v: (v.price, v.dedup_key, v.article_no)):
        reasons: list[Reason] = []
        listing_base: int | None = None
        used_bases: list[int] = []

        if x.floor_group == "NORMAL":
            # L_normal(x): 자기 자신(dedup_key 기준, 입력에서 유일함을 검사함)을 제외한 최저가
            others = [
                o.price for o in listings if o.floor_group == "NORMAL" and o.dedup_key != x.dedup_key
            ]
            listing_base = min(others) if others else None
            ratio = NORMAL_RATIO
        elif x.floor_group == "LOW":
            listing_base = l_normal_all
            ratio = LOW_RATIO
        else:  # UNKNOWN: 판정하지 않음
            verdicts.append(
                Verdict(
                    listing=x,
                    is_bargain=False,
                    reasons=[],
                    trade_base=t_normal,
                    listing_base=None,
                    discount_pct=None,
                    trade_sample_short=short,
                    alert_kind=None,
                )
            )
            continue

        if _is_below(x.price, t_normal, ratio):
            reasons.append("TRADE")
            used_bases.append(t_normal)  # type: ignore[arg-type]
        if _is_below(x.price, listing_base, ratio):
            reasons.append("LISTING")
            used_bases.append(listing_base)  # type: ignore[arg-type]

        verdicts.append(
            Verdict(
                listing=x,
                is_bargain=bool(reasons),
                reasons=reasons,
                trade_base=t_normal,
                listing_base=listing_base,
                discount_pct=_discount_pct(x.price, used_bases),
                trade_sample_short=short,
                alert_kind=None,
            )
        )
    return verdicts


def area_summary(listings: list[Listing], trades: list[Trade], as_of: date) -> dict:
    """리포트 현황표용 요약 (CLAUDE.md §10 키).

    - t_normal: 일반층 실거래 기준가 (없으면 None → "실거래 부족")
    - trade_sample_count: 기준가 계산에 쓴 표본 건수 (0~3)
    - trade_sample_short: 표본 < 3건
    - l_normal_min: NORMAL 대표 매물 최저가 (없으면 None)
    - l_low_min: LOW 대표 매물 최저가 (참고용, 없으면 None)
    - listing_count: 대표 매물 전체 건수 (UNKNOWN 포함)
    - unknown_count: 층 미상(UNKNOWN) 대표 매물 건수
    """
    _validate_scope(listings, trades)
    sample = _trade_sample(trades, as_of)
    normal = [x.price for x in listings if x.floor_group == "NORMAL"]
    low = [x.price for x in listings if x.floor_group == "LOW"]
    return {
        "t_normal": _median_int(sample),
        "trade_sample_count": len(sample),
        "trade_sample_short": len(sample) < TRADE_SAMPLE_SIZE,
        "l_normal_min": min(normal) if normal else None,
        "l_low_min": min(low) if low else None,
        "listing_count": len(listings),
        "unknown_count": sum(1 for x in listings if x.floor_group == "UNKNOWN"),
    }

"""매물 중복 묶기 (CLAUDE.md §3.4, .claude/skills/listing-dedup). 소유: listing-collector.

dedup_key 형식은 재알림 이력(alert_history)의 키이므로 바꾸지 않는다.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date

from app.domain.models import Listing

logger = logging.getLogger(__name__)


def _norm(s: str | None) -> str:
    return (s or "").replace(" ", "").upper()


def make_dedup_key(complex_no: str, area_key: float, dong: str | None, floor_raw: str | None,
                   direction: str | None) -> str:
    """dedup_key = complex_no|area_key(.2f)|norm(dong)|norm(floor_raw)|norm(direction)"""
    return f"{complex_no}|{area_key:.2f}|{_norm(dong)}|{_norm(floor_raw)}|{_norm(direction)}"


def listing_key(x: Listing) -> str:
    return make_dedup_key(x.complex_no, x.area_key, x.dong, x.floor_raw, x.direction)


def _rep_sort_key(x: Listing) -> tuple:
    # 가격 오름차순 → confirmed_at 최신 우선(None은 가장 오래된 것으로) → article_no 사전순
    confirmed = (x.confirmed_at or date.min).toordinal()
    return (x.price, -confirmed, x.article_no)


def dedup(listings: list[Listing]) -> list[Listing]:
    """대표 매물만 반환한다. 입력은 바꾸지 않는다.

    - 대표: 그룹 내 최저가 (동률이면 confirmed_at 최신, 그다음 article_no 사전순)
    - realtor_count: 그룹 크기
    - alt_prices: 그룹 내 대표 가격과 다른 호가 (중복 제거, 오름차순)
    - dedup_key: 필드에서 다시 계산한 값
    - 결과 순서: 각 그룹이 입력에 처음 나타난 순서
    """
    groups: dict[str, list[Listing]] = {}
    for x in listings:
        groups.setdefault(listing_key(x), []).append(x)

    result: list[Listing] = []
    for key, members in groups.items():
        rep = min(members, key=_rep_sort_key)
        prices = [m.price for m in members]
        alt = sorted({p for p in prices if p != rep.price})
        lo, hi = min(prices), max(prices)
        if len(members) >= 3 and (hi - lo) * 100 >= lo * 5:
            logger.warning(
                "dedup: 서로 다른 집이 묶였을 수 있음 key=%s 건수=%d 가격범위=%d~%d",
                key, len(members), lo, hi,
            )
        result.append(dataclasses.replace(
            rep, realtor_count=len(members), alt_prices=alt, dedup_key=key,
        ))
    return result

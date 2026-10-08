"""층·면적·가격 정규화 (공용, 순수 함수). 소유: listing-collector.

규칙 출처: CLAUDE.md §3, .claude/skills/floor-area-normalizer/SKILL.md
시그니처: CLAUDE.md §10
"""

from __future__ import annotations

import re
from typing import Literal

from app.domain.models import AreaType

FloorGroup = Literal["LOW", "NORMAL", "UNKNOWN"]

PYEONG_M2 = 3.3058
MAX_SUPPLY_M2 = 119.0
AREA_MATCH_TOLERANCE = 0.5

_INT_RE = re.compile(r"^[+-]?\d+$")
_PRICE_EOK_RE = re.compile(r"^(\d+)억(\d*)$")
_PRICE_MANWON_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------- 층
def _group_from_int(n: int) -> FloorGroup:
    return "LOW" if n <= 2 else "NORMAL"


def classify_floor(raw: str | int | None) -> FloorGroup:
    """층 표기를 LOW / NORMAL / UNKNOWN 으로 분류한다 (CLAUDE.md §3.2)."""
    if raw is None or isinstance(raw, bool):
        return "UNKNOWN"
    if isinstance(raw, int):
        return _group_from_int(raw)
    if not isinstance(raw, str):
        return "UNKNOWN"

    s = re.sub(r"\s+", "", raw)
    if not s:
        return "UNKNOWN"
    head = s.split("/", 1)[0]
    if head.endswith("층"):  # CLAUDE.md §9 (2026-10-08): 끝의 "층"은 떼고 해석 ("3층", "저층", "고층")
        head = head[:-1]
    if not head:
        return "UNKNOWN"

    if head == "저":
        return "LOW"
    if head in ("중", "고"):
        return "NORMAL"
    if head[0] in ("B", "b") or head.startswith("지하") or head.startswith("반지하"):
        return "LOW"
    if _INT_RE.match(head):
        return _group_from_int(int(head))
    return "UNKNOWN"


# ---------------------------------------------------------------- 면적
def area_key(exclusive_m2: float) -> float:
    """평형 식별자: 전용면적 소수 둘째 자리 반올림 (CLAUDE.md §3.1)."""
    return round(float(exclusive_m2), 2)


def to_pyeong(supply_m2: float) -> int:
    """공급면적 → 평 (반올림 정수)."""
    return round(float(supply_m2) / PYEONG_M2)


def is_target_area(supply_m2: float) -> bool:
    """조사 대상 평형 여부: 공급면적 ≤ 119.0㎡."""
    return float(supply_m2) <= MAX_SUPPLY_M2


def match_area(exclusive_m2: float, area_types: list[AreaType]) -> AreaType | None:
    """전용면적과 ±0.5㎡ 이내인 평형 중 차이가 가장 작은 것. 동률이면 area_key가 작은 쪽."""
    best: tuple[float, float, AreaType] | None = None
    for at in area_types:
        diff = round(abs(float(exclusive_m2) - at.area_key), 2)
        if diff > AREA_MATCH_TOLERANCE:
            continue
        cand = (diff, at.area_key, at)
        if best is None or cand[:2] < best[:2]:
            best = cand
    return best[2] if best else None


# ---------------------------------------------------------------- 가격
def parse_price(s: str) -> int:
    """한국식 가격 문자열 → 만원 정수. 실패 시 ValueError (0으로 대체하지 않는다).

    "12억 5,000" → 125000, "9억" → 90000, "8,500" → 8500, "125,000" → 125000
    """
    if not isinstance(s, str):
        raise ValueError(f"가격은 문자열이어야 한다: {s!r}")
    t = re.sub(r"[\s,]", "", s)
    m = _PRICE_EOK_RE.match(t)
    if m:
        value = int(m.group(1)) * 10000 + (int(m.group(2)) if m.group(2) else 0)
    elif _PRICE_MANWON_RE.match(t):
        value = int(t)
    else:
        raise ValueError(f"가격 파싱 실패: {s!r}")
    if value <= 0:
        raise ValueError(f"가격이 0 이하: {s!r}")
    return value


def format_price(manwon: int) -> str:
    """만원 정수 → 표시 문자열. 125000 → "12억 5,000", 90000 → "9억", 8500 → "8,500"."""
    if isinstance(manwon, bool) or not isinstance(manwon, int):
        raise ValueError(f"가격은 만원 단위 정수여야 한다: {manwon!r}")
    if manwon < 0:
        raise ValueError(f"음수 가격: {manwon!r}")
    eok, rest = divmod(manwon, 10000)
    if eok == 0:
        return f"{rest:,}"
    if rest == 0:
        return f"{eok:,}억"
    return f"{eok:,}억 {rest:,}"

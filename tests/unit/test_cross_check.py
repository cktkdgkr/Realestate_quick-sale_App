"""국토부·네이버 실거래 교차검증 테스트 (C4-5: 경고 3종)."""

from __future__ import annotations

from datetime import date

import pytest

from app.collectors.naver_trades import cross_check
from app.domain.models import Trade

AS_OF = date(2026, 10, 6)


def T(src: str, d: date, price: int, floor: int = 10, ak: float = 84.97, cancelled: bool = False,
      cno: str = "3009") -> Trade:
    return Trade(cno, ak, ak, floor, "LOW" if floor <= 2 else "NORMAL", price, d, cancelled, "중개거래", src)  # type: ignore[arg-type]


def M(*a, **k) -> Trade:
    return T("MOLIT", *a, **k)


def N(*a, **k) -> Trade:
    return T("NAVER", *a, **k)


def test_identical_no_warnings() -> None:
    m = [M(date(2026, 5, 1), 230000), M(date(2025, 1, 3), 220000, floor=2)]
    n = [N(date(2026, 5, 20), 230000), N(date(2025, 1, 3), 220000, floor=2)]  # 같은 계약월이면 일자 달라도 짝
    assert cross_check(m, n, AS_OF) == []


def test_price_mismatch_warning() -> None:
    w = cross_check([M(date(2026, 5, 1), 230000)], [N(date(2026, 5, 1), 232000)], AS_OF)
    assert len(w) == 1
    assert w[0].startswith("[WARN] 교차검증 가격 불일치")
    assert "2026-05" in w[0] and "10층" in w[0] and "84.97" in w[0]
    assert "23억" in w[0] and "23억 2,000" in w[0]


def test_molit_only_recent_is_info_older_is_warn() -> None:
    m = [M(date(2026, 8, 6), 230000), M(date(2026, 8, 5), 231000, floor=11), M(date(2026, 3, 1), 225000)]
    w = cross_check(m, [], AS_OF)
    assert len(w) == 3
    info = [x for x in w if x.startswith("[INFO]")]
    warn = [x for x in w if x.startswith("[WARN]")]
    assert len(info) == 1 and "2026-08-06" in info[0] and "반영 지연" in info[0]  # 경계: as_of-2개월 당일 포함
    assert len(warn) == 2 and all("국토부에만 있는" in x for x in warn)


def test_naver_only_warning() -> None:
    w = cross_check([], [N(date(2026, 1, 9), 210000, floor=5)], AS_OF)
    assert len(w) == 1 and w[0].startswith("[WARN] 교차검증 네이버에만 있는 거래") and "단지 식별" in w[0]


def test_naver_only_matching_cancelled_molit_is_flagged() -> None:
    w = cross_check([M(date(2026, 1, 9), 210000, cancelled=True)], [N(date(2026, 1, 9), 210000)], AS_OF)
    assert len(w) == 1 and "해제 거래" in w[0]


def test_cancelled_on_both_sides_ignored() -> None:
    assert cross_check([M(date(2026, 1, 9), 210000, cancelled=True)],
                       [N(date(2026, 1, 9), 210000, cancelled=True)], AS_OF) == []


def test_outside_window_ignored() -> None:
    # 국토부 조회 범위(2024-10-01 ~ as_of) 밖의 네이버 거래는 비교하지 않는다
    assert cross_check([], [N(date(2024, 9, 30), 200000), N(date(2026, 10, 7), 200000)], AS_OF) == []
    w = cross_check([], [N(date(2024, 10, 1), 200000)], AS_OF)
    assert len(w) == 1


def test_multiset_pairing_same_key() -> None:
    # 같은 키에 거래 2건: 하나는 일치, 하나는 가격 불일치
    m = [M(date(2026, 4, 1), 230000), M(date(2026, 4, 20), 240000)]
    n = [N(date(2026, 4, 1), 230000), N(date(2026, 4, 20), 241000)]
    w = cross_check(m, n, AS_OF)
    assert len(w) == 1 and "가격 불일치" in w[0] and "24억 1,000" in w[0]


def test_different_area_or_floor_or_complex_not_paired() -> None:
    m = [M(date(2026, 4, 1), 230000, floor=10)]
    n = [N(date(2026, 4, 1), 230000, floor=11)]
    w = cross_check(m, n, AS_OF)
    assert len(w) == 2  # 국토부에만 1, 네이버에만 1
    w2 = cross_check([M(date(2026, 4, 1), 230000, cno="A")], [N(date(2026, 4, 1), 230000, cno="B")], AS_OF)
    assert len(w2) == 2


def test_deterministic_order() -> None:
    m = [M(date(2026, 4, 1), 230000, floor=f) for f in (3, 9, 5)]
    n = [N(date(2026, 4, 1), 1000 + f, floor=f) for f in (12, 4)]
    a = cross_check(m, n, AS_OF)
    b = cross_check(list(reversed(m)), list(reversed(n)), AS_OF)
    assert a == b


def test_source_mixup_rejected() -> None:
    with pytest.raises(ValueError):
        cross_check([N(date(2026, 4, 1), 1)], [], AS_OF)

"""급매 판정 규칙 테스트.

- bargain-rules SKILL §6 R01~R21: 기대값을 그대로 옮긴다 (수정 금지, C5-1).
- verification-checklist C5-3 반례, C5-4 (reasons 순서·discount_pct).
- 그 밖에 판정 agent가 추가한 경계 케이스 (E 접두어).
"""

from __future__ import annotations

import ast
import random
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from app.domain import rules
from app.domain.models import Listing, Trade
from app.domain.rules import area_summary, judge, trade_base

AS_OF = date(2026, 10, 13)
CX = "C1"
AK = 84.97


# ---------------------------------------------------------------------------
# 테스트 데이터 생성 도우미
# ---------------------------------------------------------------------------
_FLOOR_RAW = {"NORMAL": "10/25", "LOW": "1/25", "UNKNOWN": "-/25"}


def L(group: str, price: int, key: str, floor_raw: str | None = None, area_key: float = AK,
      complex_no: str = CX) -> Listing:
    return Listing(
        article_no=f"A-{key}",
        complex_no=complex_no,
        area_key=area_key,
        dong="101",
        floor_raw=floor_raw if floor_raw is not None else _FLOOR_RAW[group],
        floor_group=group,  # type: ignore[arg-type]
        direction="남향",
        price=price,
        confirmed_at=None,
        realtor_count=1,
        alt_prices=[],
        dedup_key=key,
        url=f"https://example.invalid/{key}",
    )


def T(d: date, price: int, group: str = "NORMAL", cancelled: bool = False, floor: int | None = None,
      area_key: float = AK, complex_no: str = CX) -> Trade:
    if floor is None:
        floor = 10 if group == "NORMAL" else 1
    return Trade(
        complex_no=complex_no,
        area_key=area_key,
        exclusive_m2=area_key,
        floor=floor,
        floor_group=group,  # type: ignore[arg-type]
        price=price,
        contract_date=d,
        cancelled=cancelled,
        deal_type="중개거래",
        source="MOLIT",
    )


BASIC = [
    T(date(2026, 9, 20), 100000),
    T(date(2026, 8, 11), 98000),
    T(date(2026, 7, 5), 102000),
    T(date(2025, 12, 1), 90000),
]

def others(*specs: tuple[str, int]) -> list[Listing]:
    return [L(g, p, f"O{i}") for i, (g, p) in enumerate(specs)]


# ---------------------------------------------------------------------------
# bargain-rules §6 기준 테스트 케이스 (R01~R21)
#   (id, trades, x, other listings, expected)
#   expected 키: is_bargain, reasons, + 선택: listing_base, trade_base, discount_pct, short
# ---------------------------------------------------------------------------
R_CASES = [
    pytest.param(BASIC, L("NORMAL", 95000, "X"), others(("NORMAL", 100000), ("NORMAL", 99000)),
                 dict(is_bargain=True, reasons=["TRADE"]), id="R01"),
    pytest.param(BASIC, L("NORMAL", 95001, "X"), others(("NORMAL", 100000), ("NORMAL", 99000)),
                 dict(is_bargain=False, reasons=[]), id="R02"),
    pytest.param([], L("NORMAL", 94000, "X"), others(("NORMAL", 99000), ("NORMAL", 100000)),
                 dict(is_bargain=True, reasons=["LISTING"], listing_base=99000), id="R03"),
    pytest.param([], L("NORMAL", 90000, "X"), [],
                 dict(is_bargain=False, reasons=[], listing_base=None), id="R04"),
    pytest.param([], L("NORMAL", 90000, "X"), others(("NORMAL", 90000)),
                 dict(is_bargain=False, reasons=[]), id="R05"),
    pytest.param(BASIC, L("NORMAL", 90000, "X"), others(("NORMAL", 100000)),
                 dict(is_bargain=True, reasons=["TRADE", "LISTING"], discount_pct=10.0), id="R06"),
    pytest.param(BASIC, L("LOW", 90000, "X", floor_raw="1/25"), others(("NORMAL", 99000)),
                 dict(is_bargain=True, reasons=["TRADE"]), id="R07"),
    pytest.param(BASIC, L("LOW", 90001, "X", floor_raw="2/15"), others(("NORMAL", 100000)),
                 dict(is_bargain=False, reasons=[]), id="R08"),
    pytest.param([], L("LOW", 91000, "X", floor_raw="1/25"), others(("LOW", 110000), ("NORMAL", 100000)),
                 dict(is_bargain=False, reasons=[]), id="R09"),
    pytest.param([], L("LOW", 89000, "X", floor_raw="저/15"), others(("NORMAL", 100000)),
                 dict(is_bargain=True, reasons=["LISTING"]), id="R10"),
    pytest.param(BASIC, L("NORMAL", 95000, "X", floor_raw="3/25"), [],
                 dict(is_bargain=True, reasons=["TRADE"]), id="R11"),
    pytest.param([], L("NORMAL", 95000, "X"), others(("UNKNOWN", 80000)),
                 dict(is_bargain=False, reasons=[]), id="R12"),
    pytest.param(BASIC, L("UNKNOWN", 50000, "X"), others(("NORMAL", 100000)),
                 dict(is_bargain=False, reasons=[]), id="R13"),
    pytest.param([T(date(2026, 10, 1), 70000, group="LOW")] + BASIC, L("NORMAL", 95000, "X"), [],
                 dict(is_bargain=True, reasons=["TRADE"], trade_base=100000), id="R14"),
    pytest.param([T(date(2026, 10, 2), 80000, cancelled=True)] + BASIC, L("NORMAL", 95000, "X"), [],
                 dict(is_bargain=True, reasons=["TRADE"], trade_base=100000), id="R15"),
    pytest.param([T(date(2024, 10, 12), 100000)], L("NORMAL", 90000, "X"), [],
                 dict(is_bargain=False, reasons=[], trade_base=None), id="R16"),
    pytest.param([T(date(2024, 10, 13), 100000)], L("NORMAL", 95000, "X"), [],
                 dict(is_bargain=True, reasons=["TRADE"], trade_base=100000, short=True), id="R17"),
    pytest.param([T(date(2026, 9, 1), 100000), T(date(2026, 8, 1), 97001)], L("NORMAL", 93575, "X"), [],
                 dict(is_bargain=True, reasons=["TRADE"], trade_base=98500, short=True), id="R18"),
    pytest.param(
        [T(date(2026, 9, 1), 120000), T(date(2026, 8, 1), 100000),
         T(date(2026, 7, 1), 101000), T(date(2026, 6, 1), 80000)],
        L("NORMAL", 95950, "X"), [],
        dict(is_bargain=True, reasons=["TRADE"], trade_base=101000), id="R19"),
    pytest.param(BASIC, L("LOW", 90000, "X", floor_raw="B1/15"), others(("NORMAL", 100000)),
                 dict(is_bargain=True, reasons=["TRADE", "LISTING"]), id="R20"),
]


def _verdict_of(verdicts, key="X"):
    found = [v for v in verdicts if v.listing.dedup_key == key]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("trades, x, other, expected", R_CASES)
def test_reference_cases(trades, x, other, expected):
    verdicts = judge([x] + other, trades, AS_OF)
    assert len(verdicts) == 1 + len(other)
    v = _verdict_of(verdicts)
    assert v.is_bargain is expected["is_bargain"]
    assert v.reasons == expected["reasons"]
    if "listing_base" in expected:
        assert v.listing_base == expected["listing_base"]
    if "trade_base" in expected:
        assert v.trade_base == expected["trade_base"]
    if "discount_pct" in expected:
        assert v.discount_pct == expected["discount_pct"]
    if "short" in expected:
        assert v.trade_sample_short is expected["short"]
    assert v.alert_kind is None


def test_R21_mixed_area_key_raises():
    mixed = [L("NORMAL", 95000, "X"), L("NORMAL", 100000, "Y", area_key=59.99)]
    with pytest.raises(ValueError):
        judge(mixed, BASIC, AS_OF)


def test_basic_trades_give_t_normal_100000_not_short():
    assert trade_base(BASIC, AS_OF) == (100000, False)


# ---------------------------------------------------------------------------
# C5-3 검증자 반례
# ---------------------------------------------------------------------------
def _snapshot(verdicts):
    return [asdict(v) for v in verdicts]


def test_C5_3_shuffled_input_is_deterministic_10_runs():
    same_day = date(2026, 9, 1)
    trades = [
        T(same_day, 100000), T(same_day, 90000), T(same_day, 110000),
        T(date(2026, 8, 1), 50000),  # 4번째: 표본에서 빠져야 함
        T(date(2026, 10, 1), 60000, group="LOW"),
        T(date(2026, 9, 5), 40000, cancelled=True),
    ]
    listings = [
        L("NORMAL", 95000, "X"), L("NORMAL", 99000, "N2"), L("NORMAL", 99000, "N3"),
        L("LOW", 85000, "LW"), L("UNKNOWN", 70000, "U"),
    ]
    expected = _snapshot(judge(listings, trades, AS_OF))
    expected_summary = area_summary(listings, trades, AS_OF)
    # 같은 계약일 3건 → 중앙값 100000 (50000은 4번째라 제외)
    assert expected_summary["t_normal"] == 100000
    rng = random.Random(20261013)
    for _ in range(10):
        t2, l2 = trades[:], listings[:]
        rng.shuffle(t2)
        rng.shuffle(l2)
        assert _snapshot(judge(l2, t2, AS_OF)) == expected
        assert area_summary(l2, t2, AS_OF) == expected_summary


def test_C5_3_same_day_tie_uses_price_desc_for_sample_selection():
    # 같은 날 4건 중 가격 내림차순 상위 3건(130000, 120000, 110000) → 중앙값 120000
    same_day = date(2026, 9, 1)
    prices = [100000, 110000, 120000, 130000]
    rng = random.Random(1)
    for _ in range(10):
        p = prices[:]
        rng.shuffle(p)
        assert trade_base([T(same_day, x) for x in p], AS_OF) == (120000, False)


def test_C5_3_single_normal_listing_no_trades_is_not_bargain():
    (v,) = judge([L("NORMAL", 10000, "X")], [], AS_OF)
    assert v.is_bargain is False
    assert v.reasons == []
    assert v.trade_base is None and v.listing_base is None and v.discount_pct is None


def test_C5_3_only_low_listings_no_normal_no_trades_is_not_bargain():
    listings = [L("LOW", 50000, "A"), L("LOW", 100000, "B", floor_raw="저/15"),
                L("LOW", 10000, "C", floor_raw="B1/15")]
    verdicts = judge(listings, [], AS_OF)
    assert len(verdicts) == 3
    assert all(not v.is_bargain and v.reasons == [] for v in verdicts)


# ---------------------------------------------------------------------------
# C5-4 reasons 순서, discount_pct
# ---------------------------------------------------------------------------
def test_C5_4_discount_pct_uses_larger_discount_of_reason_bases():
    # T_normal=100000, L_normal(x)=110000 → 둘 다 근거. 할인율 큰 쪽 = 110000 기준
    (v, _) = judge([L("NORMAL", 90000, "X"), L("NORMAL", 110000, "Y")], BASIC, AS_OF)
    assert v.listing.dedup_key == "X"
    assert v.reasons == ["TRADE", "LISTING"]
    assert v.discount_pct == round((1 - 90000 / 110000) * 100, 1) == 18.2


def test_C5_4_discount_pct_only_from_reason_base():
    # TRADE만 근거 (매물 기준 99000*95=9,405,000 < 9,500,000). 할인율은 T_normal 기준 5.0
    (v,) = [x for x in judge([L("NORMAL", 95000, "X"), L("NORMAL", 99000, "Y")], BASIC, AS_OF)
            if x.listing.dedup_key == "X"]
    assert v.reasons == ["TRADE"]
    assert v.discount_pct == 5.0


def test_C5_4_not_bargain_has_no_discount_pct():
    v = _verdict_of(judge([L("NORMAL", 99000, "X")], BASIC, AS_OF))
    assert v.discount_pct is None
    assert v.trade_base == 100000


# ---------------------------------------------------------------------------
# 추가 경계 케이스 (판정 agent)
# ---------------------------------------------------------------------------
def test_E01_future_trade_after_as_of_excluded():
    trades = [T(date(2026, 10, 14), 50000)] + BASIC
    assert trade_base(trades, AS_OF) == (100000, False)


def test_E02_trade_on_as_of_included():
    assert trade_base([T(AS_OF, 100000)], AS_OF) == (100000, True)


def test_E03_window_leap_day_clamps_like_relativedelta():
    # 2028-02-29 - 24개월 = 2026-02-28 (relativedelta와 동일)
    as_of = date(2028, 2, 29)
    assert trade_base([T(date(2026, 2, 28), 100000)], as_of)[0] == 100000
    assert trade_base([T(date(2026, 2, 27), 100000)], as_of)[0] is None


def test_E04_zero_trades_short_true():
    assert trade_base([], AS_OF) == (None, True)


@pytest.mark.parametrize("price, reasons", [(81000, ["LISTING"]), (81001, [])],
                         ids=["E05-boundary", "E05-above"])
def test_E05_low_listing_base_is_min_of_all_normals(price, reasons):
    # L_normal_all = 90000 → 기준 90000*90 = 8,100,000
    v = _verdict_of(judge([L("LOW", price, "X"), L("NORMAL", 100000, "A"), L("NORMAL", 90000, "B")],
                          [], AS_OF))
    assert v.listing_base == 90000
    assert v.reasons == reasons


def test_E06_normal_listing_base_excludes_only_self():
    verdicts = {v.listing.dedup_key: v for v in judge(
        [L("NORMAL", 80000, "X"), L("NORMAL", 100000, "A"), L("NORMAL", 90000, "B")], [], AS_OF)}
    assert verdicts["X"].listing_base == 90000
    assert verdicts["B"].listing_base == 80000
    assert verdicts["A"].listing_base == 80000


def test_E07_mixed_complex_raises():
    with pytest.raises(ValueError):
        judge([L("NORMAL", 1, "X"), L("NORMAL", 2, "Y", complex_no="C2")], [], AS_OF)


def test_E08_trade_area_key_mismatch_raises():
    with pytest.raises(ValueError):
        judge([L("NORMAL", 95000, "X")], [T(date(2026, 9, 1), 100000, area_key=59.99)], AS_OF)


def test_E09_duplicate_dedup_key_raises():
    with pytest.raises(ValueError):
        judge([L("NORMAL", 90000, "X"), L("NORMAL", 100000, "X")], [], AS_OF)


def test_E10_empty_input():
    assert judge([], [], AS_OF) == []
    assert area_summary([], [], AS_OF) == {
        "t_normal": None, "trade_sample_count": 0, "trade_sample_short": True,
        "l_normal_min": None, "l_low_min": None, "listing_count": 0, "unknown_count": 0,
    }


def test_E11_unknown_verdict_shape():
    v = _verdict_of(judge([L("UNKNOWN", 1, "X")], BASIC, AS_OF))
    assert (v.is_bargain, v.reasons, v.listing_base, v.discount_pct, v.alert_kind) == (
        False, [], None, None, None)


def test_E12_all_verdicts_alert_kind_none():
    listings = [L("NORMAL", 90000, "X"), L("NORMAL", 100000, "Y"), L("LOW", 50000, "Z"),
                L("UNKNOWN", 1, "U")]
    assert all(v.alert_kind is None for v in judge(listings, BASIC, AS_OF))


def test_area_summary_keys_and_values():
    listings = [L("NORMAL", 99000, "A"), L("NORMAL", 97000, "B"), L("LOW", 88000, "C"),
                L("LOW", 87000, "D"), L("UNKNOWN", 50000, "U")]
    trades = [T(date(2026, 9, 1), 100000), T(date(2026, 8, 1), 97001),
              T(date(2026, 9, 2), 60000, group="LOW")]
    assert area_summary(listings, trades, AS_OF) == {
        "t_normal": 98500,
        "trade_sample_count": 2,
        "trade_sample_short": True,
        "l_normal_min": 97000,
        "l_low_min": 87000,
        "listing_count": 5,
        "unknown_count": 1,
    }


def test_area_summary_mixed_area_key_raises():
    with pytest.raises(ValueError):
        area_summary([L("NORMAL", 1, "X")], [T(date(2026, 9, 1), 1, area_key=59.99)], AS_OF)


# ---------------------------------------------------------------------------
# C5-2 순수성 (정적 검사)
# ---------------------------------------------------------------------------
def test_C5_2_rules_module_has_no_io_or_clock():
    src = Path(rules.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "calendar", "datetime", "typing", "app"}
    for banned in ("open(", ".now(", ".today(", "time.time", "httpx", "sqlalchemy", "os.environ",
                   "print("):
        assert banned not in src, banned


# ---------------------------------------------------------------------------
# CLAUDE.md §9 확정 사항 (verifier 반려 1회차 대응)
# ---------------------------------------------------------------------------
def test_S9_area_key_equal_after_normalize_round_is_same_area():
    # 84.97 / 84.9700001 → normalize.area_key로 둘 다 84.97
    (v,) = judge([L("NORMAL", 95000, "X", area_key=84.97)],
                 [T(date(2026, 9, 1), 100000, area_key=84.9700001)], AS_OF)
    assert v.is_bargain is True
    assert v.reasons == ["TRADE"]


def test_S9_area_summary_area_key_rounded_compare():
    s = area_summary([L("NORMAL", 99000, "X", area_key=59.994)],
                     [T(date(2026, 9, 1), 100000, area_key=59.99)], AS_OF)
    assert s["t_normal"] == 100000


@pytest.mark.parametrize("fn", [judge, area_summary], ids=["judge", "area_summary"])
def test_S9_area_key_different_after_round_raises(fn):
    with pytest.raises(ValueError):
        fn([L("NORMAL", 95000, "X", area_key=84.97)],
           [T(date(2026, 9, 1), 100000, area_key=84.98)], AS_OF)


def test_S9_unknown_verdict_fields():
    v = _verdict_of(judge([L("UNKNOWN", 50000, "X"), L("NORMAL", 100000, "A")], BASIC, AS_OF))
    assert v.is_bargain is False
    assert v.reasons == []
    assert v.listing_base is None
    assert v.discount_pct is None


def test_S9_judge_output_order_price_dedup_article_asc():
    listings = [
        L("NORMAL", 100000, "B"), L("UNKNOWN", 90000, "Z"), L("NORMAL", 90000, "C"),
        L("LOW", 90000, "A"), L("NORMAL", 80000, "Y"),
    ]
    expected = ["Y", "A", "C", "Z", "B"]  # 가격 → dedup_key 오름차순
    rng = random.Random(7)
    for _ in range(10):
        l2 = listings[:]
        rng.shuffle(l2)
        assert [v.listing.dedup_key for v in judge(l2, BASIC, AS_OF)] == expected


def test_S9_judge_output_order_article_no_tiebreak():
    # dedup_key가 같으면 ValueError이므로 article_no 단계는 정렬 키 자체로 확인한다
    a = L("NORMAL", 90000, "K1")
    b = L("NORMAL", 90000, "K2")
    a.article_no, b.article_no = "Z9", "A1"
    assert [v.listing.dedup_key for v in judge([b, a], [], AS_OF)] == ["K1", "K2"]


@pytest.mark.parametrize(
    "price, expected_pct",
    [
        (94950, 5.1),   # 정확히 5.05 → HALF_UP 5.1 (float round는 5.0이 됨)
        (94951, 5.0),   # 5.049
        (94949, 5.1),   # 5.051
        (95000, 5.0),   # 5.00
    ],
    ids=["HALF_UP-5.05", "below-5.049", "above-5.051", "exact-5.0"],
)
def test_S9_discount_pct_round_half_up_integer(price, expected_pct):
    v = _verdict_of(judge([L("NORMAL", price, "X")], BASIC, AS_OF))  # T_normal = 100000
    assert v.reasons == ["TRADE"]
    assert v.discount_pct == expected_pct


def test_S9_discount_pct_half_up_with_odd_base():
    from app.domain.rules import _discount_pct
    # (200000-189900)*1000/200000 = 50.5 tenths → 5.05% → 5.1
    assert _discount_pct(189900, [200000]) == 5.1
    # (98500-93575)*1000/98500 = 50.0 tenths → 5.0
    assert _discount_pct(93575, [98500]) == 5.0
    # 큰 기준가(할인율 큰 쪽) 선택
    assert _discount_pct(90000, [100000, 110000]) == 18.2

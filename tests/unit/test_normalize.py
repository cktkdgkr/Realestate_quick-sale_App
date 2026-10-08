"""normalize.py 테스트. floor-area-normalizer 스킬의 모든 표 + 검증 C2-2 반례."""

import pickle

import pytest

from app.collectors.errors import CollectorError
from app.domain.models import AreaType
from app.domain.normalize import (
    area_key,
    classify_floor,
    format_price,
    is_target_area,
    match_area,
    parse_price,
    to_pyeong,
)


def _at(key: float) -> AreaType:
    return AreaType(complex_no="1", area_key=key, exclusive_m2=key, supply_m2=110.0, pyeong=33,
                    type_name=str(key))


# ---------------------------------------------------------------- §1 층 (스킬 표 그대로)
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1/25", "LOW"),
        ("2/15", "LOW"),
        ("3/25", "NORMAL"),
        ("저/15", "LOW"),
        ("중/20", "NORMAL"),
        ("고/20", "NORMAL"),
        ("B1/15", "LOW"),
        ("지하1/10", "LOW"),
        (" 12 / 25 ", "NORMAL"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        ("-/25", "UNKNOWN"),
        (1, "LOW"),
        (2, "LOW"),
        (0, "LOW"),
        (-1, "LOW"),
        (3, "NORMAL"),
    ],
)
def test_classify_floor_skill_table(raw, expected):
    assert classify_floor(raw) == expected


# 검증 C2-2 반례 + 처리 순서상의 추가 경계
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("저 / 15", "LOW"),
        ("고", "NORMAL"),
        ("15", "NORMAL"),
        ("B2", "LOW"),
        ("옥탑", "UNKNOWN"),
        ("b1/15", "LOW"),
        ("반지하/5", "LOW"),
        ("지하", "LOW"),
        ("중", "NORMAL"),
        ("저", "LOW"),
        ("2", "LOW"),
        ("3", "NORMAL"),
        ("   ", "UNKNOWN"),
        ("/25", "UNKNOWN"),
        ("3층", "UNKNOWN"),
        ("저층/15", "UNKNOWN"),
    ],
)
def test_classify_floor_counterexamples(raw, expected):
    assert classify_floor(raw) == expected


# ---------------------------------------------------------------- §2 면적
@pytest.mark.parametrize("supply, expected", [(112.4, 34), (119.0, 36)])
def test_to_pyeong(supply, expected):
    assert to_pyeong(supply) == expected


@pytest.mark.parametrize("supply, expected", [(119.0, True), (119.01, False)])
def test_is_target_area(supply, expected):
    assert is_target_area(supply) is expected


@pytest.mark.parametrize("raw, expected", [(84.97, 84.97), (84.971, 84.97), (59.996, 60.0), (114.9, 114.9)])
def test_area_key(raw, expected):
    assert area_key(raw) == expected


@pytest.mark.parametrize(
    "exclusive, keys, expected",
    [
        (84.98, [84.97, 59.99], 84.97),
        (85.6, [84.97], None),
        (84.5, [84.97, 84.03], 84.03),
        (84.47, [84.97], 84.97),  # 차이 정확히 0.5 → 포함 (부동소수점 오차 방지)
        (84.0, [], None),
    ],
)
def test_match_area(exclusive, keys, expected):
    got = match_area(exclusive, [_at(k) for k in keys])
    assert (got.area_key if got else None) == expected


# ---------------------------------------------------------------- §3 가격
@pytest.mark.parametrize(
    "s, expected",
    [
        ("12억 5,000", 125000),
        ("9억", 90000),
        ("12억5000", 125000),
        ("8,500", 8500),
        (" 125,000 ", 125000),
        ("10억 500", 100500),
    ],
)
def test_parse_price(s, expected):
    got = parse_price(s)
    assert got == expected and isinstance(got, int)


@pytest.mark.parametrize("s", ["가격문의", "", "억", "12.5억", "0", "12억 5천", None])
def test_parse_price_invalid(s):
    with pytest.raises(ValueError):
        parse_price(s)


@pytest.mark.parametrize(
    "manwon, expected",
    [(125000, "12억 5,000"), (90000, "9억"), (8500, "8,500"), (100500, "10억 500"), (1250000, "125억")],
)
def test_format_price(manwon, expected):
    assert format_price(manwon) == expected


@pytest.mark.parametrize("manwon", [125000, 90000, 8500, 100500, 1])
def test_format_parse_roundtrip(manwon):
    assert parse_price(format_price(manwon)) == manwon


# ---------------------------------------------------------------- CollectorError
def test_collector_error_fields():
    e = CollectorError("blocked", "HTTP 429", complex_no="3009")
    assert (e.stage, e.detail, e.complex_no) == ("blocked", "HTTP 429", "3009")
    assert "3009" in str(e) and "blocked" in str(e)
    e2 = pickle.loads(pickle.dumps(e))
    assert (e2.stage, e2.detail, e2.complex_no) == ("blocked", "HTTP 429", "3009")


def test_collector_error_rejects_unknown_stage():
    with pytest.raises(ValueError):
        CollectorError("oops")

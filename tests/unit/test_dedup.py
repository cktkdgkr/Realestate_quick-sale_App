"""dedup.py 테스트. listing-dedup 스킬 §4 D01~D06 + 키 형식·경고 조건."""

import logging
import random
from datetime import date

import pytest

from app.domain.dedup import dedup, make_dedup_key
from app.domain.models import Listing
from app.domain.normalize import classify_floor


def L(article_no: str, price: int, *, dong: str = "101동", floor_raw: str = "10/25",
      direction: str = "남향", area_key: float = 84.97, confirmed_at: date | None = date(2026, 10, 1),
      complex_no: str = "3009") -> Listing:
    return Listing(
        article_no=article_no, complex_no=complex_no, area_key=area_key, dong=dong,
        floor_raw=floor_raw, floor_group=classify_floor(floor_raw), direction=direction,
        price=price, confirmed_at=confirmed_at, realtor_count=1, alt_prices=[],
        dedup_key="", url=f"https://new.land.naver.com/complexes/{complex_no}?articleNo={article_no}",
    )


def test_key_format():
    assert make_dedup_key("3009", 84.9, "101 동", "저 / 15", "남향") == "3009|84.90|101동|저/15|남향"
    assert make_dedup_key("3009", 84.97, "", "b1/15", None) == "3009|84.97||B1/15|"


def test_D01_same_key_three_listings():
    out = dedup([L("a", 100000), L("b", 98000), L("c", 98000)])
    assert len(out) == 1
    rep = out[0]
    assert rep.price == 98000
    assert rep.realtor_count == 3
    assert rep.article_no == "b"  # 가격·confirmed_at 동률 → article_no 사전순
    assert rep.alt_prices == [100000]
    assert rep.dedup_key == "3009|84.97|101동|10/25|남향"


def test_D02_direction_differs():
    out = dedup([L("a", 100000, direction="남향"), L("b", 100000, direction="동향")])
    assert len(out) == 2
    assert all(x.realtor_count == 1 for x in out)


def test_D03_dong_whitespace_normalized():
    out = dedup([L("a", 100000, dong="101동"), L("b", 99000, dong="101 동")])
    assert len(out) == 1
    assert out[0].article_no == "b" and out[0].realtor_count == 2


def test_D04_same_price_latest_confirmed_wins():
    out = dedup([
        L("a", 100000, confirmed_at=date(2026, 9, 1)),
        L("b", 100000, confirmed_at=date(2026, 10, 5)),
        L("c", 100000, confirmed_at=None),
    ])
    assert len(out) == 1
    assert out[0].article_no == "b"
    assert out[0].alt_prices == []


def test_D05_empty_dong_same_floor_text(caplog):
    with caplog.at_level(logging.WARNING, logger="app.domain.dedup"):
        out = dedup([L("a", 100000, dong="", floor_raw="저/15"),
                     L("b", 101000, dong="", floor_raw="저/15")])
    assert len(out) == 1
    assert out[0].realtor_count == 2 and out[0].floor_group == "LOW"
    assert caplog.records == []


def test_D06_area_key_differs():
    out = dedup([L("a", 100000, area_key=84.97), L("b", 100000, area_key=84.96)])
    assert len(out) == 2


def test_warning_when_three_or_more_with_5pct_spread(caplog):
    with caplog.at_level(logging.WARNING, logger="app.domain.dedup"):
        dedup([L("a", 100000, dong="", direction="", floor_raw="중/20"),
               L("b", 105000, dong="", direction="", floor_raw="중/20"),
               L("c", 101000, dong="", direction="", floor_raw="중/20")])
    assert len(caplog.records) == 1


def test_unknown_floor_also_grouped():
    out = dedup([L("a", 90000, floor_raw=""), L("b", 91000, floor_raw="")])
    assert len(out) == 1 and out[0].floor_group == "UNKNOWN"


def test_input_not_mutated_and_deterministic():
    items = [L("a", 100000), L("b", 98000), L("c", 98000), L("d", 70000, dong="102동")]
    before = [(x.realtor_count, x.alt_prices, x.dedup_key) for x in items]
    expected = sorted((x.article_no, x.realtor_count) for x in dedup(items))
    for seed in range(10):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert sorted((x.article_no, x.realtor_count) for x in dedup(shuffled)) == expected
    assert [(x.realtor_count, x.alt_prices, x.dedup_key) for x in items] == before


@pytest.mark.parametrize("n", [0, 1])
def test_small_inputs(n):
    items = [L("a", 100000)][:n]
    out = dedup(items)
    assert len(out) == n
    if n:
        assert out[0].realtor_count == 1

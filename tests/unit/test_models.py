"""app.domain.models가 CLAUDE.md §5와 필드명·타입·순서까지 같은지 확인."""

from __future__ import annotations

import dataclasses
import typing
from datetime import date, datetime
from typing import Literal

import pytest

from app.domain import models as m

EXPECTED: dict[str, list[tuple[str, object]]] = {
    "Complex": [
        ("complex_no", str),
        ("name", str),
        ("lawd_cd", str),
        ("address", str),
        ("max_floor", int | None),
        ("molit_apt_seq", str | None),
    ],
    "AreaType": [
        ("complex_no", str),
        ("area_key", float),
        ("exclusive_m2", float),
        ("supply_m2", float),
        ("pyeong", int),
        ("type_name", str),
    ],
    "Listing": [
        ("article_no", str),
        ("complex_no", str),
        ("area_key", float),
        ("dong", str),
        ("floor_raw", str),
        ("floor_group", Literal["LOW", "NORMAL", "UNKNOWN"]),
        ("direction", str),
        ("price", int),
        ("confirmed_at", date | None),
        ("realtor_count", int),
        ("alt_prices", list[int]),
        ("dedup_key", str),
        ("url", str),
    ],
    "Trade": [
        ("complex_no", str),
        ("area_key", float),
        ("exclusive_m2", float),
        ("floor", int),
        ("floor_group", Literal["LOW", "NORMAL"]),
        ("price", int),
        ("contract_date", date),
        ("cancelled", bool),
        ("deal_type", str),
        ("source", Literal["MOLIT", "NAVER"]),
    ],
    "Verdict": [
        ("listing", m.Listing),
        ("is_bargain", bool),
        ("reasons", list[Literal["TRADE", "LISTING"]]),
        ("trade_base", int | None),
        ("listing_base", int | None),
        ("discount_pct", float | None),
        ("trade_sample_short", bool),
        ("alert_kind", Literal["NEW", "PRICE_DROP", "ONGOING", None]),
    ],
    "RunResult": [
        ("run_id", str),
        ("started_at", datetime),
        ("status", Literal["OK", "PARTIAL", "FAILED"]),
        ("verdicts", list[m.Verdict]),
        ("errors", list[str]),
        ("cross_check_warnings", list[str]),
    ],
}


@pytest.mark.parametrize("cls_name", sorted(EXPECTED))
def test_schema_matches_claude_md_section5(cls_name: str) -> None:
    cls = getattr(m, cls_name)
    assert dataclasses.is_dataclass(cls)
    fields = dataclasses.fields(cls)
    hints = typing.get_type_hints(cls)
    actual = [(f.name, hints[f.name]) for f in fields]
    assert actual == EXPECTED[cls_name]
    # 기본값이 없어야 한다 (누락된 필드가 조용히 채워지지 않도록)
    for f in fields:
        assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING


def test_no_extra_public_dataclasses() -> None:
    public = {
        name
        for name, obj in vars(m).items()
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == m.__name__
    }
    assert public == set(EXPECTED)


def test_construct_sample_objects() -> None:
    listing = m.Listing(
        article_no="A1", complex_no="3009", area_key=84.97, dong="101동", floor_raw="3/25",
        floor_group="NORMAL", direction="남향", price=125000, confirmed_at=date(2026, 10, 1),
        realtor_count=2, alt_prices=[126000], dedup_key="k", url="https://example.invalid/a1",
    )
    verdict = m.Verdict(
        listing=listing, is_bargain=True, reasons=["TRADE"], trade_base=135000,
        listing_base=None, discount_pct=7.4, trade_sample_short=False, alert_kind="NEW",
    )
    run = m.RunResult(
        run_id="r1", started_at=datetime(2026, 10, 13, 10, 0), status="OK",
        verdicts=[verdict], errors=[], cross_check_warnings=[],
    )
    assert run.verdicts[0].listing.price == 125000
    assert isinstance(listing.price, int)

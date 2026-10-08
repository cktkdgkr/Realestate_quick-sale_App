"""모듈 간 공통 데이터 스키마.

CLAUDE.md §5를 필드명·타입·순서까지 그대로 옮긴 것이다.
필드를 추가·변경하려면 Orchestrator 승인이 필요하다 (CLAUDE.md §5).
가격 필드(price, alt_prices, trade_base, listing_base)는 모두 만원 단위 정수다 (CLAUDE.md §3.3).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


@dataclass
class Complex:
    complex_no: str
    name: str
    lawd_cd: str
    address: str
    max_floor: int | None
    molit_apt_seq: str | None  # 국토부 단지 식별자. None이면 "실거래 매칭 확인 필요"


@dataclass
class AreaType:
    complex_no: str
    area_key: float
    exclusive_m2: float
    supply_m2: float
    pyeong: int
    type_name: str


@dataclass
class Listing:
    article_no: str
    complex_no: str
    area_key: float
    dong: str
    floor_raw: str
    floor_group: Literal["LOW", "NORMAL", "UNKNOWN"]
    direction: str
    price: int
    confirmed_at: date | None
    realtor_count: int
    alt_prices: list[int]
    dedup_key: str
    url: str


@dataclass
class Trade:
    complex_no: str
    area_key: float
    exclusive_m2: float
    floor: int
    floor_group: Literal["LOW", "NORMAL"]
    price: int
    contract_date: date
    cancelled: bool
    deal_type: str
    source: Literal["MOLIT", "NAVER"]


@dataclass
class Verdict:
    listing: Listing
    is_bargain: bool
    reasons: list[Literal["TRADE", "LISTING"]]
    trade_base: int | None
    listing_base: int | None
    discount_pct: float | None
    trade_sample_short: bool
    alert_kind: Literal["NEW", "PRICE_DROP", "ONGOING", None]


@dataclass
class RunResult:
    run_id: str
    started_at: datetime
    status: Literal["OK", "PARTIAL", "FAILED"]
    verdicts: list[Verdict]
    errors: list[str]
    cross_check_warnings: list[str]

"""SQLAlchemy 2.x ORM 모델 (SQLite).

규칙
- 가격(만원)은 모두 Integer. float 컬럼은 면적(㎡)과 표시용 discount_pct뿐이다.
- 시각은 UTCDateTime(시간대 있는 datetime만 받음, UTC로 저장, UTC aware로 반환).
- 스키마 변경은 반드시 Alembic 마이그레이션을 추가해서 한다 (state/history.sqlite3가 매주 이어서 쓰이므로).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ComplexRow(Base):
    """단지 메타데이터 캐시. 조사 대상의 원본은 config/complexes.yaml이다."""

    __tablename__ = "complexes"

    complex_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    lawd_cd: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(300))
    max_floor: Mapped[int | None] = mapped_column(Integer)
    molit_apt_seq: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AreaTypeRow(Base):
    __tablename__ = "area_types"
    __table_args__ = (UniqueConstraint("complex_no", "area_key", name="uq_area_types_complex_area"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    complex_no: Mapped[str] = mapped_column(
        String(32), ForeignKey("complexes.complex_no", ondelete="CASCADE")
    )
    area_key: Mapped[float] = mapped_column(Float)
    exclusive_m2: Mapped[float] = mapped_column(Float)
    supply_m2: Mapped[float] = mapped_column(Float)
    pyeong: Mapped[int] = mapped_column(Integer)
    type_name: Mapped[str] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class RunRow(Base):
    """실행 1회 기록. status: RUNNING(진행 중/비정상 종료) | OK | PARTIAL | FAILED."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    # 실행일(Asia/Seoul 기준 날짜). 24개월 계산 기준일로 쓰인다.
    as_of: Mapped[date] = mapped_column(Date)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    cross_check_warnings: Mapped[list] = mapped_column(JSON, default=list)
    # 단지별 결과 요약: {complex_no: {"status": "OK"|"FAILED", "error": str|None, ...}}
    complex_results: Mapped[dict] = mapped_column(JSON, default=dict)
    report_path: Mapped[str | None] = mapped_column(Text)


class ListingSnapshotRow(Base):
    """실행 시점의 대표 매물(dedup 후). Listing 필드를 그대로 저장."""

    __tablename__ = "listings_snapshot"
    __table_args__ = (
        UniqueConstraint("run_id", "dedup_key", name="uq_listings_snapshot_run_dedup"),
        Index("ix_listings_snapshot_complex_area", "complex_no", "area_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    article_no: Mapped[str] = mapped_column(String(32))
    complex_no: Mapped[str] = mapped_column(String(32))
    area_key: Mapped[float] = mapped_column(Float)
    dong: Mapped[str] = mapped_column(String(50))
    floor_raw: Mapped[str] = mapped_column(String(50))
    floor_group: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(20))
    price: Mapped[int] = mapped_column(Integer)
    confirmed_at: Mapped[date | None] = mapped_column(Date)
    realtor_count: Mapped[int] = mapped_column(Integer)
    alt_prices: Mapped[list] = mapped_column(JSON, default=list)
    dedup_key: Mapped[str] = mapped_column(String(300), index=True)
    url: Mapped[str] = mapped_column(Text)


class TradeSnapshotRow(Base):
    """실행 시점에 수집한 실거래. 해제 거래(cancelled=True)도 버리지 않고 저장."""

    __tablename__ = "trades_snapshot"
    __table_args__ = (Index("ix_trades_snapshot_complex_area", "complex_no", "area_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    complex_no: Mapped[str] = mapped_column(String(32))
    area_key: Mapped[float] = mapped_column(Float)
    exclusive_m2: Mapped[float] = mapped_column(Float)
    floor: Mapped[int] = mapped_column(Integer)
    floor_group: Mapped[str] = mapped_column(String(8))
    price: Mapped[int] = mapped_column(Integer)
    contract_date: Mapped[date] = mapped_column(Date)
    cancelled: Mapped[bool] = mapped_column(Boolean)
    deal_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(8))


class VerdictRow(Base):
    """판정 결과. listing은 listings_snapshot 행을 가리킨다."""

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    listing_snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("listings_snapshot.id", ondelete="CASCADE"), unique=True
    )
    dedup_key: Mapped[str] = mapped_column(String(300), index=True)
    is_bargain: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    trade_base: Mapped[int | None] = mapped_column(Integer)
    listing_base: Mapped[int | None] = mapped_column(Integer)
    discount_pct: Mapped[float | None] = mapped_column(Float)  # 표시용. 판정 비교에는 쓰지 않는다.
    trade_sample_short: Mapped[bool] = mapped_column(Boolean)
    alert_kind: Mapped[str | None] = mapped_column(String(16))


class AlertHistoryRow(Base):
    """재알림 이력 (dedup_key 단위, CLAUDE.md §4.4).

    active=True  : 현재 급매로 알려진 상태
    active=False : 급매가 아니게 되었거나 매물이 사라짐. deactivated_run_id가 이번 실행이면
                   리포트 "지난주 급매 중 내려간 매물"에 1회 표시.
    """

    __tablename__ = "alert_history"

    dedup_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    complex_no: Mapped[str] = mapped_column(String(32), index=True)
    area_key: Mapped[float] = mapped_column(Float)
    first_alerted_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_alerted_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_alerted_price: Mapped[int] = mapped_column(Integer)
    last_seen_run_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deactivated_run_id: Mapped[str | None] = mapped_column(String(64))

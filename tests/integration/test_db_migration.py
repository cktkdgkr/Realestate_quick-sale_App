"""Alembic 마이그레이션으로 DB를 만들고, ORM 모델과 어긋남이 없는지 확인."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.db import (
    AlertHistoryRow,
    Base,
    ListingSnapshotRow,
    RunRow,
    VerdictRow,
    make_engine,
    make_session_factory,
    upgrade_db,
)

TABLES = {
    "complexes",
    "area_types",
    "runs",
    "listings_snapshot",
    "trades_snapshot",
    "verdicts",
    "alert_history",
}
KST = timezone(timedelta(hours=9))


@pytest.fixture
def engine(tmp_db_path: Path):
    upgrade_db(tmp_db_path)
    eng = make_engine(tmp_db_path)
    yield eng
    eng.dispose()


def test_upgrade_creates_file_and_tables(tmp_db_path: Path, engine) -> None:
    assert tmp_db_path.is_file()
    assert TABLES <= set(inspect(engine).get_table_names())


def test_migration_matches_orm_models(engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent(tmp_db_path: Path, engine) -> None:
    upgrade_db(tmp_db_path)  # 두 번째 적용도 오류 없음


def test_alert_history_columns(engine) -> None:
    cols = {c["name"] for c in inspect(engine).get_columns("alert_history")}
    assert {"dedup_key", "first_alerted_at", "last_alerted_price", "active"} <= cols
    pk = inspect(engine).get_pk_constraint("alert_history")["constrained_columns"]
    assert pk == ["dedup_key"]


def test_roundtrip_and_utc_storage(engine) -> None:
    Session = make_session_factory(engine)
    started = datetime(2026, 10, 13, 10, 0, tzinfo=KST)
    with Session.begin() as s:
        s.add(RunRow(run_id="r1", started_at=started, status="RUNNING", dry_run=True,
                     as_of=date(2026, 10, 13), errors=[], cross_check_warnings=[], complex_results={}))
        s.flush()
        ls = ListingSnapshotRow(run_id="r1", article_no="A1", complex_no="3009", area_key=84.97,
                                dong="101동", floor_raw="3/25", floor_group="NORMAL", direction="남향",
                                price=125000, confirmed_at=None, realtor_count=2, alt_prices=[126000],
                                dedup_key="3009|84.97|101동|3/25|남향", url="https://example.invalid/a1")
        s.add(ls)
        s.flush()
        s.add(VerdictRow(run_id="r1", listing_snapshot_id=ls.id, dedup_key=ls.dedup_key, is_bargain=True,
                         reasons=["TRADE", "LISTING"], trade_base=135000, listing_base=None,
                         discount_pct=7.4, trade_sample_short=False, alert_kind="NEW"))
        s.add(AlertHistoryRow(dedup_key=ls.dedup_key, complex_no="3009", area_key=84.97,
                              first_alerted_at=started, last_alerted_at=started,
                              last_alerted_price=125000, last_seen_run_id="r1", active=True))
    with Session() as s:
        run = s.get(RunRow, "r1")
        assert run.started_at == started  # 같은 순간
        assert run.started_at.tzinfo is UTC
        assert run.started_at.hour == 1  # 10:00 KST == 01:00 UTC
        v = s.scalars(select(VerdictRow)).one()
        assert v.reasons == ["TRADE", "LISTING"]
        assert isinstance(v.trade_base, int)
        assert s.scalars(select(ListingSnapshotRow)).one().alt_prices == [126000]


def test_naive_datetime_rejected(engine) -> None:
    Session = make_session_factory(engine)
    with pytest.raises(StatementError):
        with Session.begin() as s:
            s.add(RunRow(run_id="r2", started_at=datetime(2026, 10, 13, 10, 0), status="OK",
                         dry_run=False, as_of=date(2026, 10, 13), errors=[],
                         cross_check_warnings=[], complex_results={}))


def test_foreign_keys_enforced(engine) -> None:
    Session = make_session_factory(engine)
    with pytest.raises(IntegrityError):
        with Session.begin() as s:
            s.add(ListingSnapshotRow(run_id="missing-run", article_no="A", complex_no="1", area_key=59.0,
                                     dong="", floor_raw="", floor_group="UNKNOWN", direction="", price=1,
                                     confirmed_at=None, realtor_count=1, alt_prices=[], dedup_key="k",
                                     url="u"))

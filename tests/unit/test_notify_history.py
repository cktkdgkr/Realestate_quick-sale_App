"""재알림 분류·이력 갱신 (CLAUDE.md §4.4, notify 스킬 §2, 체크리스트 C6-1·C6-2·C6-3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.migrate import upgrade_db
from app.db.models import AlertHistoryRow
from app.db.session import make_engine, make_session_factory
from app.notify.history import classify_alerts, commit_history, prior_alerts
from tests.unit.notify_sample import PREV_RUN_AT, hist, listing, new_session, verdict

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
C1, C2 = "100", "200"


def bargain(price: int, cno: str = C1, dong: str = "101동", art: str = "a1"):
    return verdict(listing(cno, 84.97, dong, "10/20", "NORMAL", price, art), ["TRADE"], 100000, None, 5.0)


def not_bargain(price: int, cno: str = C1, dong: str = "101동", art: str = "a1"):
    return verdict(listing(cno, 84.97, dong, "10/20", "NORMAL", price, art), [], 100000, None, None)


def key(cno: str = C1, dong: str = "101동") -> str:
    return f"{cno}|84.97|{dong}|10/20|남향"


def snapshot(session) -> list[tuple]:
    session.expire_all()
    rows = session.scalars(select(AlertHistoryRow).order_by(AlertHistoryRow.dedup_key)).all()
    return [
        (r.dedup_key, r.complex_no, r.area_key, r.first_alerted_at, r.last_alerted_at,
         r.last_alerted_price, r.last_seen_run_id, r.active, r.deactivated_run_id)
        for r in rows
    ]


def row(session, k: str) -> AlertHistoryRow:
    session.expire_all()
    return session.get(AlertHistoryRow, k)


@pytest.fixture
def session():
    s = new_session()
    yield s
    s.close()


# ---------------------------------------------------------------- 분류 표 각 행 (C6-1)

def test_row1_new_when_no_history(session):
    vs, gone = classify_alerts(session, [bargain(95000)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind == "NEW" and gone == []
    stats = commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    assert stats["NEW"] == 1
    r = row(session, key())
    assert (r.active, r.last_alerted_price, r.first_alerted_at, r.last_alerted_at, r.last_seen_run_id) == \
        (True, 95000, NOW, NOW, "r2")
    assert r.complex_no == C1 and r.area_key == 84.97 and r.deactivated_run_id is None


def test_row1_new_when_history_inactive_reactivates(session):
    h = hist(key(), C1, 84.97, 90000, active=False)
    h.deactivated_run_id = "r0"
    session.add(h)
    session.commit()
    vs, gone = classify_alerts(session, [bargain(95000)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind == "NEW" and gone == []
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    r = row(session, key())
    assert (r.active, r.last_alerted_price, r.deactivated_run_id, r.first_alerted_at) == (True, 95000, None, NOW)


def test_row2_price_drop(session):
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    vs, _ = classify_alerts(session, [bargain(94999)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind == "PRICE_DROP"
    assert prior_alerts(session, [key()])[key()]["last_alerted_price"] == 95000
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    r = row(session, key())
    assert (r.last_alerted_price, r.last_alerted_at, r.first_alerted_at, r.active, r.last_seen_run_id) == \
        (94999, NOW, PREV_RUN_AT, True, "r2")


@pytest.mark.parametrize("price", [95000, 95001])
def test_row3_ongoing_same_or_higher(session, price):
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    vs, gone = classify_alerts(session, [bargain(price)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind == "ONGOING" and gone == []
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    r = row(session, key())
    # last_seen만 갱신: 알림가·알림 시각 그대로
    assert (r.last_alerted_price, r.last_alerted_at, r.active, r.last_seen_run_id) == \
        (95000, PREV_RUN_AT, True, "r2")


def test_row4a_listing_disappeared(session):
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    vs, gone = classify_alerts(session, [], set(), "r2", dry_run=False)
    assert vs == []
    assert [(g["dedup_key"], g["reason"], g["listing"], g["last_alerted_price"]) for g in gone] == \
        [(key(), "GONE", None, 95000)]
    stats = commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    assert stats["DEACTIVATED"] == 1
    r = row(session, key())
    assert (r.active, r.deactivated_run_id, r.last_alerted_price) == (False, "r2", 95000)


def test_row4b_no_longer_bargain(session):
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    vs, gone = classify_alerts(session, [not_bargain(99000)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind is None
    assert gone[0]["reason"] == "NOT_BARGAIN" and gone[0]["listing"].price == 99000
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    assert row(session, key()).active is False


def test_gone_shown_only_once(session):
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    vs, gone = classify_alerts(session, [], set(), "r2", dry_run=False)
    assert len(gone) == 1
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    _, gone_next = classify_alerts(session, [], set(), "r3", dry_run=False)
    assert gone_next == []


def test_non_bargain_without_history_has_no_kind_and_no_row(session):
    vs, gone = classify_alerts(session, [not_bargain(100000)], set(), "r2", dry_run=False)
    assert vs[0].alert_kind is None and gone == []
    commit_history(session, vs, set(), "r2", dry_run=False, now=NOW)
    assert snapshot(session) == []


# ---------------------------------------------------------------- 수집 실패 단지 (C6-2)

def test_failed_complex_history_untouched(session):
    session.add_all([
        hist(key(C2, "1동"), C2, 84.97, 95000),           # 실패 단지, 이번에 매물 없음
        hist(key(C2, "2동"), C2, 84.97, 95000),           # 실패 단지, 일부 결과가 들어와도
        hist(key(C1, "1동"), C1, 84.97, 95000),           # 정상 단지, 매물 없음 → 비활성화
    ])
    session.commit()
    before = {r[0]: r for r in snapshot(session)}
    verdicts = [bargain(90000, cno=C2, dong="2동", art="b2"), bargain(80000, cno=C2, dong="3동", art="b3")]
    vs, gone = classify_alerts(session, verdicts, {C2}, "r2", dry_run=False)
    assert [g["dedup_key"] for g in gone] == [key(C1, "1동")]
    stats = commit_history(session, vs, {C2}, "r2", dry_run=False, now=NOW)
    assert stats["SKIPPED_FAILED"] == 2
    after = {r[0]: r for r in snapshot(session)}
    assert after[key(C2, "1동")] == before[key(C2, "1동")]
    assert after[key(C2, "2동")] == before[key(C2, "2동")]
    assert key(C2, "3동") not in after          # 실패 단지에는 새 이력도 만들지 않는다
    assert after[key(C1, "1동")][7] is False


# ---------------------------------------------------------------- dry_run (CLAUDE.md §9)

def test_dry_run_never_changes_db(session):
    session.add_all([
        hist(key(C1, "1동"), C1, 84.97, 95000),   # 가격 인하 예정
        hist(key(C1, "2동"), C1, 84.97, 95000),   # 사라짐 예정
    ])
    session.commit()
    before = snapshot(session)
    verdicts = [bargain(90000, dong="1동", art="x1"), bargain(90000, dong="9동", art="x9")]
    vs, gone = classify_alerts(session, verdicts, set(), "r2", dry_run=True)
    assert [v.alert_kind for v in vs] == ["PRICE_DROP", "NEW"]      # 분류는 기존 이력으로 계산
    assert [g["dedup_key"] for g in gone] == [key(C1, "2동")]
    stats = commit_history(session, vs, set(), "r2", dry_run=True, now=NOW)
    assert stats == {"NEW": 1, "PRICE_DROP": 1, "ONGOING": 0, "DEACTIVATED": 1,
                     "SKIPPED_FAILED": 0, "dry_run": 1}
    session.rollback()
    assert snapshot(session) == before


def test_classify_is_read_only_even_without_dry_run(session):
    """C6-3 조정판: 파일 쓰기 전에는 DB가 바뀌면 안 된다. 분류만 하고 commit을 안 부르면 그대로다."""
    session.add(hist(key(), C1, 84.97, 95000))
    session.commit()
    before = snapshot(session)
    classify_alerts(session, [bargain(90000, dong="9동", art="z")], set(), "r2", dry_run=False)
    assert not session.new and not session.dirty
    session.rollback()
    assert snapshot(session) == before


# ---------------------------------------------------------------- 입력 검사·기타

def test_duplicate_dedup_key_rejected(session):
    with pytest.raises(ValueError, match="dedup_key"):
        classify_alerts(session, [bargain(90000, art="a"), bargain(91000, art="b")], set(), "r", dry_run=True)


def test_classify_does_not_mutate_input(session):
    original = [bargain(90000)]
    vs, _ = classify_alerts(session, original, set(), "r", dry_run=True)
    assert original[0].alert_kind is None and vs[0].alert_kind == "NEW"


def test_commit_requires_aware_now(session):
    with pytest.raises(ValueError):
        commit_history(session, [bargain(90000)], set(), "r", dry_run=False, now=datetime(2026, 10, 13))


def test_works_on_migrated_db(tmp_db_path):
    """Alembic 스키마(실제 state/history.sqlite3와 같은 것)에서도 동작한다."""
    upgrade_db(tmp_db_path)
    s = make_session_factory(make_engine(tmp_db_path))()
    vs, _ = classify_alerts(s, [bargain(90000)], set(), "r1", dry_run=False)
    commit_history(s, vs, set(), "r1", dry_run=False, now=NOW)
    vs2, _ = classify_alerts(s, [bargain(89000)], set(), "r2", dry_run=False)
    assert vs2[0].alert_kind == "PRICE_DROP"
    s.close()

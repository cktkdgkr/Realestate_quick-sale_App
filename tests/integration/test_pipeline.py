"""파이프라인 통합 테스트 (C7-3 실패 격리, C7-4 TZ=UTC의 as_of, C7-5 락, 드라이런).

다른 모듈(수집·판정·알림)은 전부 fake로 바꿔 끼운다. 실제 외부 호출 없음.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import pipeline
from app.collectors import molit_trades, naver_listings, naver_trades
from app.db import (
    ListingSnapshotRow,
    RunRow,
    VerdictRow,
    make_engine,
    make_session_factory,
    upgrade_db,
)
from app.domain import dedup, rules
from app.domain.models import AreaType, Complex, Listing, Trade, Verdict
from app.notify import history, report, summary

try:  # 실제 CollectorError가 생겼으면 그것을 쓴다
    from app.collectors.errors import CollectorError  # type: ignore
except ImportError:  # pragma: no cover - 수집 agent 작업 전

    class CollectorError(Exception):  # type: ignore[no-redef]
        def __init__(self, stage: str, detail: str = "", complex_no: str | None = None):
            super().__init__(f"{stage}: {detail}")
            self.stage, self.detail, self.complex_no = stage, detail, complex_no


FIXED_NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)  # 화 10:00 KST
SECRET = "SECRET-MOLIT-KEY-9f8e7d"
AREA = 84.97


def _listing(cno: str, n: int, price: int, floor_group: str = "NORMAL") -> Listing:
    key = f"{cno}|{AREA}|10{n}동|{n + 3}/20|남향"
    return Listing(article_no=f"{cno}-{n}", complex_no=cno, area_key=AREA, dong=f"10{n}동",
                   floor_raw=f"{n + 3}/20", floor_group=floor_group, direction="남향", price=price,
                   confirmed_at=None, realtor_count=1, alt_prices=[], dedup_key=key,
                   url=f"https://example.invalid/{cno}/{n}")


def _trade(cno: str, price: int, source: str = "MOLIT") -> Trade:
    return Trade(complex_no=cno, area_key=AREA, exclusive_m2=AREA, floor=10, floor_group="NORMAL",
                 price=price, contract_date=date(2026, 9, 1), cancelled=False, deal_type="중개거래",
                 source=source)


@dataclass
class Fakes:
    """fake 모듈 동작과 호출 기록."""

    fail: dict[str, tuple[str, Exception]] = field(default_factory=dict)  # complex_no -> (단계, 예외)
    calls: list[tuple[str, str]] = field(default_factory=list)
    trades_as_of: list[date] = field(default_factory=list)
    judge_inputs: list[tuple[str, int, int]] = field(default_factory=list)
    classify_args: list[dict] = field(default_factory=list)
    commit_calls: list[dict] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)
    runs: list = field(default_factory=list)
    summary_raises: Exception | None = None

    def _maybe_fail(self, stage: str, cno: str) -> None:
        self.calls.append((stage, cno))
        if cno in self.fail and self.fail[cno][0] == stage:
            raise self.fail[cno][1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfile = tmp_path / "complexes.yaml"
    cfile.write_text(
        "complexes:\n"
        "  - complex_no: '111'\n    name: 가단지\n    molit_apt_seq: 'SEQ-111'\n"
        "  - complex_no: '222'\n    name: 나단지\n    molit_apt_seq: 'SEQ-222'\n"
        "  - complex_no: '333'\n    name: 다단지\n",
        encoding="utf-8",
    )
    for k, v in {
        "MOLIT_API_KEY": SECRET,
        "DRY_RUN": "false",
        "DB_PATH": str(tmp_path / "wt" / "state" / "history.sqlite3"),
        "REPORT_DIR": str(tmp_path / "wt" / "reports"),
        "OUT_DIR": str(tmp_path / "out"),
        "COMPLEXES_FILE": str(cfile),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(pipeline, "_utcnow", lambda: FIXED_NOW)
    return tmp_path


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()

    class FakeClient:
        closed = False

        def __init__(self, *a, **kw):
            pass

        def close(self):
            FakeClient.closed = True

    def fetch_complex(client, cno):
        f._maybe_fail("fetch_complex", cno)
        return (Complex(cno, f"단지{cno}", "11650", "서울 서초구", 20, None),
                [AreaType(cno, AREA, AREA, 112.4, 34, "112A")])

    def fetch_listings(client, cx, area_types):
        f._maybe_fail("fetch_listings", cx.complex_no)
        return [_listing(cx.complex_no, 1, 100000), _listing(cx.complex_no, 2, 85000)]

    def fetch_trades(client, cx, area_types, as_of):
        f._maybe_fail("fetch_trades", cx.complex_no)
        f.trades_as_of.append(as_of)
        assert cx.molit_apt_seq, "매핑 없는 단지에서 fetch_trades를 부르면 안 됨"
        return [_trade(cx.complex_no, 100000)]

    def find_apt_seq_candidates(client, cx, as_of):
        f._maybe_fail("find_apt_seq_candidates", cx.complex_no)
        return [{"apt_seq": f"CAND-{cx.complex_no}", "name": cx.name}]

    def fetch_naver_trades(client, cx, area_types):
        f._maybe_fail("fetch_naver_trades", cx.complex_no)
        return [_trade(cx.complex_no, 100000, "NAVER")]

    def cross_check(molit, naver, as_of):
        return []

    def judge(listings, trades, as_of):
        cno = listings[0].complex_no if listings else "?"
        f._maybe_fail("judge", cno)
        f.judge_inputs.append((cno, len(listings), len(trades)))
        return [Verdict(l, l.price * 100 <= 95000 * 95, ["LISTING"] if l.price < 90000 else [], None,
                        None, None, False, None) for l in listings]

    def area_summary(listings, trades, as_of):
        return {"listing_count": len(listings), "trade_sample_count": len(trades)}

    def classify_alerts(session, verdicts, failed_complex_nos, run_id, dry_run):
        f.classify_args.append(dict(verdicts=list(verdicts), failed=set(failed_complex_nos),
                                    run_id=run_id, dry_run=dry_run))
        for v in verdicts:
            v.alert_kind = "NEW" if v.is_bargain else None
        return verdicts, []

    def commit_history(session, **kw):
        f.commit_calls.append(kw)

    def render_report(run, context):
        f.runs.append(run)
        f.contexts.append(context)
        return f"<html>{run.status} {len(run.verdicts)} {' | '.join(run.errors)}</html>"

    def render_summary(run, context):
        if f.summary_raises:
            raise f.summary_raises
        return f"[{run.status}] 실패 {len(context['failed_complexes'])}\n" + "\n".join(run.errors) + "\n" + \
            "\n".join(run.cross_check_warnings) + "\n"

    for mod, name, fn in [
        (naver_listings, "NaverClient", FakeClient),
        (naver_listings, "fetch_complex", fetch_complex),
        (naver_listings, "fetch_listings", fetch_listings),
        (molit_trades, "MolitClient", FakeClient),
        (molit_trades, "fetch_trades", fetch_trades),
        (molit_trades, "find_apt_seq_candidates", find_apt_seq_candidates),
        (naver_trades, "fetch_naver_trades", fetch_naver_trades),
        (naver_trades, "cross_check", cross_check),
        (dedup, "dedup", lambda ls: list(ls)),
        (rules, "judge", judge),
        (rules, "area_summary", area_summary),
        (history, "classify_alerts", classify_alerts),
        (history, "commit_history", commit_history),
        (report, "render_report", render_report),
        (summary, "render_summary", render_summary),
    ]:
        monkeypatch.setattr(mod, name, fn, raising=False)
    return f


def _runs(env: Path) -> list[RunRow]:
    eng = make_engine(env / "wt" / "state" / "history.sqlite3")
    try:
        with make_session_factory(eng)() as s:
            return list(s.scalars(select(RunRow).order_by(RunRow.started_at)))
    finally:
        eng.dispose()


def _count(env: Path, model) -> int:
    eng = make_engine(env / "wt" / "state" / "history.sqlite3")
    try:
        with make_session_factory(eng)() as s:
            return len(s.scalars(select(model)).all())
    finally:
        eng.dispose()


# --------------------------------------------------------------------- 정상
def test_all_ok(env: Path, fakes: Fakes) -> None:
    assert pipeline.main(["--once"]) == 0
    out = (env / "out" / "summary.md").read_text(encoding="utf-8")
    assert out.startswith("[OK]")
    assert (env / "out" / "report.html").is_file()
    assert (env / "wt" / "reports" / "2026-10-13.html").is_file()
    # 처리 순서: 단지마다 fetch_complex -> fetch_listings -> fetch_trades -> fetch_naver_trades
    assert [c for c in fakes.calls if c[1] == "111"][:4] == [
        ("fetch_complex", "111"), ("fetch_listings", "111"), ("fetch_trades", "111"),
        ("fetch_naver_trades", "111")]
    assert len(fakes.commit_calls) == 1
    assert fakes.classify_args[0]["dry_run"] is False
    assert fakes.classify_args[0]["failed"] == set()
    (run,) = _runs(env)
    assert run.status == "OK" and run.finished_at is not None and run.dry_run is False
    assert run.report_path.endswith("2026-10-13.html")
    assert _count(env, ListingSnapshotRow) == 6 and _count(env, VerdictRow) == 6


def test_unmapped_complex_judged_on_listings_only(env: Path, fakes: Fakes) -> None:
    assert pipeline.main(["--once"]) == 0
    # 333은 molit_apt_seq 없음 -> fetch_trades·네이버 실거래 호출 없이 trades=[]로 판정
    assert ("fetch_trades", "333") not in fakes.calls
    assert ("fetch_naver_trades", "333") not in fakes.calls
    assert ("find_apt_seq_candidates", "333") in fakes.calls
    assert ("333", 2, 0) in fakes.judge_inputs
    ctx = fakes.contexts[0]
    (m,) = ctx["mapping_needed"]
    assert m["complex_no"] == "333" and m["apt_seq_candidates"] == [{"apt_seq": "CAND-333", "name": "단지333"}]
    assert any("실거래 매칭 확인 필요" in w for w in fakes.runs[0].cross_check_warnings)
    # complexes.yaml의 molit_apt_seq가 Complex에 반영됨
    c111 = next(c for c in ctx["complexes"] if c["complex_no"] == "111")
    assert c111["complex"].molit_apt_seq == "SEQ-111"


def test_candidate_lookup_failure_is_warning_not_hidden(env: Path, fakes: Fakes) -> None:
    fakes.fail["333"] = ("find_apt_seq_candidates", CollectorError("molit_api", "timeout"))
    assert pipeline.main(["--once"]) == 0
    w = [w for w in fakes.runs[0].cross_check_warnings if "다단지" in w or "단지333" in w]
    assert w and "후보 조회 실패" in w[0] and "molit_api" in w[0]


# --------------------------------------------------------------------- 실패 격리 (C7-3)
@pytest.mark.parametrize("stage", ["fetch_complex", "fetch_listings", "fetch_trades", "judge"])
def test_one_complex_failure_is_isolated(env: Path, fakes: Fakes, stage: str) -> None:
    fakes.fail["222"] = (stage, RuntimeError("boom"))
    assert pipeline.main(["--once"]) == 1
    c = fakes.classify_args[0]
    assert c["failed"] == {"222"}
    assert {v.listing.complex_no for v in c["verdicts"]} == {"111", "333"}
    run = fakes.runs[0]
    assert run.status == "PARTIAL"
    assert any("222" in e and "수집 실패" in e for e in run.errors)
    assert fakes.contexts[0]["failed_complexes"][0]["complex_no"] == "222"
    assert (env / "out" / "summary.md").read_text(encoding="utf-8").startswith("[PARTIAL] 실패 1")
    (row,) = _runs(env)
    assert row.status == "PARTIAL"
    assert row.complex_results["222"]["status"] == "FAILED"
    assert row.complex_results["111"]["status"] == "OK"
    assert len(fakes.commit_calls) == 1 and fakes.commit_calls[0]["failed_complex_nos"] == {"222"}


def test_all_complexes_fail(env: Path, fakes: Fakes) -> None:
    for cno in ("111", "222", "333"):
        fakes.fail[cno] = ("fetch_complex", CollectorError("network", "down"))
    assert pipeline.main(["--once"]) == 2
    assert fakes.classify_args[0]["failed"] == {"111", "222", "333"}
    assert fakes.classify_args[0]["verdicts"] == []
    assert fakes.runs[0].status == "FAILED"
    assert _runs(env)[0].status == "FAILED"
    assert (env / "out" / "summary.md").read_text(encoding="utf-8").startswith("[FAILED]")


def test_molit_failure_fails_that_complex(env: Path, fakes: Fakes) -> None:
    fakes.fail["111"] = ("fetch_trades", CollectorError("molit_auth", "key expired"))
    assert pipeline.main(["--once"]) == 1
    assert fakes.classify_args[0]["failed"] == {"111"}
    assert any("molit_auth" in e for e in fakes.runs[0].errors)


def test_cross_check_failure_only_warns(env: Path, fakes: Fakes) -> None:
    fakes.fail["111"] = ("fetch_naver_trades", CollectorError("schema_changed", "no field"))
    assert pipeline.main(["--once"]) == 0
    assert fakes.classify_args[0]["failed"] == set()
    assert ("111", 2, 1) in fakes.judge_inputs  # 국토부 실거래로 판정은 그대로
    assert any("교차검증 실패" in w and "schema_changed" in w for w in fakes.runs[0].cross_check_warnings)


def test_blocked_stops_remaining_naver_requests(env: Path, fakes: Fakes) -> None:
    fakes.fail["222"] = ("fetch_listings", CollectorError("blocked", "403"))
    assert pipeline.main(["--once"]) == 1
    assert ("fetch_complex", "333") not in fakes.calls  # 남은 네이버 요청 중단
    assert fakes.classify_args[0]["failed"] == {"222", "333"}
    assert fakes.contexts[0]["naver_blocked"] is True
    assert any("333" in e and "blocked" in e for e in fakes.runs[0].errors)


def test_blocked_in_cross_check_keeps_complex_but_stops_rest(env: Path, fakes: Fakes) -> None:
    fakes.fail["111"] = ("fetch_naver_trades", CollectorError("blocked", "429"))
    assert pipeline.main(["--once"]) == 1
    assert ("fetch_complex", "222") not in fakes.calls
    assert fakes.classify_args[0]["failed"] == {"222", "333"}


def test_secrets_are_masked(env: Path, fakes: Fakes) -> None:
    fakes.fail["111"] = ("fetch_trades", RuntimeError(
        f"GET https://apis.data.go.kr/x?serviceKey={SECRET}&LAWD_CD=11650 failed; key={SECRET}; "
        "Authorization: Bearer abcdefghijklmnop.qrstu"))
    assert pipeline.main(["--once"]) == 1
    blobs = [(env / "out" / "summary.md").read_text(encoding="utf-8"),
             (env / "out" / "report.html").read_text(encoding="utf-8"),
             repr(_runs(env)[0].errors)]
    for b in blobs:
        assert SECRET not in b
        assert "abcdefghijklmnop" not in b
    assert "serviceKey=***" in blobs[0]


# --------------------------------------------------------------------- 드라이런
@pytest.mark.parametrize("how", ["flag", "env"])
def test_dry_run_writes_out_only(env: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    if how == "env":
        monkeypatch.setenv("DRY_RUN", "true")
        rc = pipeline.main(["--once"])
    else:
        rc = pipeline.main(["--once", "--dry-run"])
    assert rc == 0
    assert (env / "out" / "summary.md").is_file() and (env / "out" / "report.html").is_file()
    assert not (env / "wt" / "reports").exists()
    assert fakes.commit_calls == []
    assert fakes.classify_args[0]["dry_run"] is True
    assert _count(env, ListingSnapshotRow) == 0
    (row,) = _runs(env)
    assert row.dry_run is True and row.status == "OK" and row.report_path is None


# --------------------------------------------------------------------- 렌더링·쓰기 실패
def test_render_failure_does_not_commit_history(env: Path, fakes: Fakes) -> None:
    fakes.summary_raises = RuntimeError("template broken")
    assert pipeline.main(["--once"]) == 2
    assert fakes.commit_calls == []
    s = (env / "out" / "summary.md").read_text(encoding="utf-8")
    assert s.startswith("[FAILED]") and "template broken" in s
    assert _runs(env)[0].status == "FAILED"
    assert _count(env, ListingSnapshotRow) == 0


def test_missing_api_key_fails_loudly(env: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOLIT_API_KEY", "")
    monkeypatch.setattr(pipeline.config, "PROJECT_ROOT", env)  # 저장소 .env를 읽지 않게
    assert pipeline.main(["--once"]) == 2
    assert fakes.calls == []
    assert "MOLIT_API_KEY" in (env / "out" / "summary.md").read_text(encoding="utf-8")


def test_bad_complexes_yaml_fails_loudly(env: Path, fakes: Fakes) -> None:
    (env / "complexes.yaml").write_text("complexes: []\n", encoding="utf-8")
    assert pipeline.main(["--once"]) == 2
    assert (env / "out" / "summary.md").read_text(encoding="utf-8").startswith("[FAILED]")


@pytest.mark.parametrize("argv", [[], ["--bogus"], ["--dry-run"]])
def test_requires_once(argv: list[str], env: Path, fakes: Fakes) -> None:
    assert pipeline.main(argv) == 2
    assert fakes.calls == []


# --------------------------------------------------------------------- 락 (C7-5)
def _insert_running(env: Path, run_id: str, started: datetime) -> None:
    db = env / "wt" / "state" / "history.sqlite3"
    upgrade_db(db)
    eng = make_engine(db)
    with make_session_factory(eng).begin() as s:
        s.add(RunRow(run_id=run_id, started_at=started, status="RUNNING", dry_run=False,
                     as_of=started.date(), errors=[], cross_check_warnings=[], complex_results={}))
    eng.dispose()


def test_lock_blocks_concurrent_run(env: Path, fakes: Fakes) -> None:
    _insert_running(env, "other", FIXED_NOW - timedelta(minutes=30))
    assert pipeline.main(["--once"]) == 2
    assert fakes.calls == []  # 수집하지 않음
    assert not (env / "out" / "summary.md").exists()  # 진행 중인 실행의 출력을 덮어쓰지 않음
    rows = {r.run_id: r for r in _runs(env)}
    assert rows["other"].status == "RUNNING"  # 남의 락은 그대로
    mine = [r for r in rows.values() if r.run_id != "other"]
    assert mine[0].status == "FAILED" and "중복 실행" in mine[0].errors[0]


def test_stale_lock_is_released(env: Path, fakes: Fakes) -> None:
    _insert_running(env, "stale", FIXED_NOW - timedelta(hours=2, minutes=1))
    assert pipeline.main(["--once"]) == 0
    rows = {r.run_id: r for r in _runs(env)}
    assert rows["stale"].status == "FAILED"
    assert "비정상 종료" in rows["stale"].errors[0]


def test_lock_is_released_after_run(env: Path, fakes: Fakes) -> None:
    assert pipeline.main(["--once"]) == 0
    assert pipeline.main(["--once"]) == 0  # 두 번째 실행도 락에 걸리지 않음
    assert [r.status for r in _runs(env)] == ["OK", "OK"]


def test_acquire_run_lock_first_inserter_wins(tmp_db_path: Path) -> None:
    upgrade_db(tmp_db_path)
    eng = make_engine(tmp_db_path)
    S = make_session_factory(eng)
    t = FIXED_NOW
    pipeline.acquire_run_lock(S, "a", t, t.date(), False)
    # 시작 시각이 더 이르더라도 나중에 들어온 실행은 진다 (삽입 순서 기준)
    with pytest.raises(pipeline.LockBusy):
        pipeline.acquire_run_lock(S, "b", t - timedelta(minutes=1), t.date(), False)
    with S() as s:
        assert s.get(RunRow, "a").status == "RUNNING"
        assert s.get(RunRow, "b").status == "FAILED"
    eng.dispose()


# --------------------------------------------------------------------- 시간대 (C7-4)
@pytest.fixture
def os_tz_utc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    "utc_now,expected",
    [
        (datetime(2026, 10, 12, 23, 30, tzinfo=UTC), date(2026, 10, 13)),  # 화 08:30 KST, UTC로는 월요일
        (datetime(2026, 10, 13, 1, 0, tzinfo=UTC), date(2026, 10, 13)),    # 화 10:00 KST
        (datetime(2026, 10, 13, 14, 59, tzinfo=UTC), date(2026, 10, 13)),  # 화 23:59 KST
        (datetime(2026, 10, 13, 15, 0, tzinfo=UTC), date(2026, 10, 14)),   # 수 00:00 KST
    ],
)
def test_as_of_is_kst_date_under_utc_os(env: Path, fakes: Fakes, os_tz_utc, monkeypatch: pytest.MonkeyPatch,
                                        utc_now: datetime, expected: date) -> None:
    assert time.strftime("%Z") == "UTC"
    monkeypatch.setattr(pipeline, "_utcnow", lambda: utc_now)
    assert pipeline.main(["--once"]) == 0
    assert set(fakes.trades_as_of) == {expected}
    assert fakes.contexts[0]["as_of"] == expected
    assert (env / "wt" / "reports" / f"{expected.isoformat()}.html").is_file()
    run = fakes.runs[0]
    assert run.started_at.utcoffset() == timedelta(hours=9)
    assert _runs(env)[0].as_of == expected


def test_now_kst_ignores_os_tz(os_tz_utc) -> None:
    assert os.environ["TZ"] == "UTC"
    assert pipeline.now_kst().utcoffset() == timedelta(hours=9)


def test_sanitize_patterns() -> None:
    s = pipeline.sanitize("https://u:p4ss@host/x?serviceKey=abc%2B&x=1 Bearer abcdefghij123 KEYVALUE",
                          ["KEYVALUE"])
    assert "p4ss" not in s and "abc%2B" not in s and "abcdefghij123" not in s and "KEYVALUE" not in s

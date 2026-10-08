"""리포트·요약 렌더링 (체크리스트 C6-3~C6-6 조정판). 스냅샷 = 고정 입력 → 고정 출력 파일.

스냅샷 갱신: UPDATE_NOTIFY_SNAPSHOTS=1 .venv/bin/pytest tests/unit/test_notify_render.py
(tests/fixtures/notify/ 스냅샷과 docs/samples/ 샘플을 함께 다시 쓴다. 바뀐 내용을 눈으로 확인할 것)
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.models import RunResult
from app.notify import report as report_mod
from app.notify.history import commit_history
from app.notify.report import reason_lines, redact, render_report, write_outputs
from app.notify.summary import render_summary
from tests.unit import notify_sample as S

ROOT = Path(__file__).resolve().parents[2]
SNAP_DIR = ROOT / "tests" / "fixtures" / "notify"
SAMPLES = {
    "report": (SNAP_DIR / "report_snapshot.html", ROOT / "docs" / "samples" / "report_sample.html"),
    "summary": (SNAP_DIR / "summary_snapshot.md", ROOT / "docs" / "samples" / "summary_sample.md"),
}
UPDATE = os.environ.get("UPDATE_NOTIFY_SNAPSHOTS") == "1"


@pytest.fixture(autouse=True)
def _format_price(monkeypatch):
    S.ensure_format_price(monkeypatch)


@pytest.fixture
def sample():
    run, ctx, session = S.build_sample()
    yield run, ctx, session
    session.close()


def _check_snapshot(name: str, text: str) -> None:
    for path in SAMPLES[name]:
        if UPDATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert path.read_text(encoding="utf-8") == text, f"스냅샷 불일치: {path}"


# ---------------------------------------------------------------- 스냅샷

def test_report_snapshot(sample):
    run, ctx, _ = sample
    _check_snapshot("report", render_report(run, ctx))


def test_summary_snapshot(sample):
    run, ctx, _ = sample
    _check_snapshot("summary", render_summary(run, ctx))


# ---------------------------------------------------------------- HTML 형식 (C6-5 조정판)

def test_report_inline_only_and_600px(sample):
    run, ctx, _ = sample
    html = render_report(run, ctx)
    low = html.lower()
    assert "<script" not in low and "<link" not in low and "<style" not in low
    assert "@import" not in low and "url(" not in low
    assert 'width="600"' in html and "max-width:600px" in html
    assert '<meta name="viewport"' in html


def test_report_sections_in_order(sample):
    run, ctx, _ = sample
    html = render_report(run, ctx)
    marks = ["<!-- 1. 요약 박스 -->", "<!-- 2. 이번 주 알림 -->", "<!-- 3. 지속 중 -->",
             "<!-- 4. 지난주 급매 중 내려간 매물 -->", "<!-- 5. 단지·평형별 현황 -->",
             "<!-- 6. 교차검증 경고·수집 오류 -->", "<!-- 7. 바닥글 -->"]
    pos = [html.index(m) for m in marks]
    assert pos == sorted(pos)
    # 각 섹션 실제 내용도 순서대로
    body = ["실행 상태", "이번 주 알림 (신규 · 가격 인하)", "지속 중인 급매", "지난주 급매 중 내려간 매물",
            "단지·평형별 현황", "교차검증 경고 · 수집 오류", "데이터 출처"]
    pos = [html.index(b) for b in body]
    assert pos == sorted(pos)


def test_report_shows_flags(sample):
    run, ctx, _ = sample
    html = render_report(run, ctx)
    for word in ["실거래 부족", "층 미상", "수집 실패", "실거래 매칭 확인 필요", "가격 인하 (이전가 → 현재가)",
                 "신규", "지속 중", "11650-0101", "molit_apt_seq", "config/complexes.yaml",
                 "저층 실거래(참고)", "표본 부족", "헬리오시티"]:
        assert word in html, word
    # 수집 실패 배너가 요약 박스보다 먼저 (맨 위)
    assert html.index("수집 실패") < html.index("실행 상태")
    # 가격 인하: 이전가 → 현재가
    assert "24억 3,000</s> → <strong>23억 8,000" in html


def test_report_escapes_html(sample):
    run, ctx, _ = sample
    run = replace(run, errors=["<script>alert(1)</script>"])
    html = render_report(run, ctx)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_secrets_redacted(sample):
    run, ctx, _ = sample
    key_name = "service" + "Key"
    run = replace(run, errors=[f"GET /x?{key_name}=abcDEF123%2B&pageNo=1"])
    out = render_report(run, ctx) + render_summary(run, ctx)
    assert "abcDEF123" not in out
    assert redact(f"a?{key_name}=zz&b=1") == f"a?{key_name}=***&b=1"


# ---------------------------------------------------------------- 요약 첫 줄 (C6-4 조정판)

def first_line(run, ctx) -> str:
    return render_summary(run, ctx).splitlines()[0]


def test_summary_first_line_partial(sample):
    run, ctx, _ = sample
    line = first_line(run, ctx)
    assert line.startswith("[수집 실패 있음]")
    assert "상태 PARTIAL" in line and "신규 2" in line and "인하 1" in line and "수집 실패 1단지" in line
    assert line == re.sub(r"<[^>]+>", "", line)  # 마크업 없음
    assert f"<title>{line}</title>" in render_report(run, ctx)


def test_summary_first_line_ok_no_bargain(sample):
    run, ctx, _ = sample
    ctx = dict(ctx, failures=[], gone=[])
    run = RunResult(run.run_id, run.started_at, "OK", [v for v in run.verdicts if not v.is_bargain], [], [])
    line = first_line(run, ctx)
    assert line == "[급매 리포트] 2026-10-13 (화) — 상태 OK · 신규 0 · 인하 0 · 지속 0 · 수집 실패 없음"
    assert "신규·가격 인하 급매 없음" in render_summary(run, ctx)


def test_summary_failed_does_not_look_like_no_bargain(sample):
    run, ctx, _ = sample
    ctx = dict(ctx, complexes=[], area_types={}, area_summaries={}, gone=[], molit_candidates={},
               failures=[{"complex_no": "3009", "name": "잠원동아", "stage": "network"},
                         {"complex_no": "22627", "name": "잠실엘스", "stage": "blocked"}])
    run = RunResult(run.run_id, run.started_at, "FAILED", [], ["network down"], [])
    text = render_summary(run, ctx)
    line = text.splitlines()[0]
    assert line.startswith("[수집 실패]") and "상태 FAILED" in line and "급매 판정 못 함" in line
    assert "수집 실패 2단지" in line
    assert "급매 없음\"이 아닙니다" in text
    assert "신규·가격 인하 급매 없음" not in text
    html = render_report(run, ctx)
    assert "급매 판정을 하지 못했습니다" in html
    assert "신규·가격 인하 급매가 없습니다." not in html


def test_summary_partial_without_failure_entries_still_flags(sample):
    run, ctx, _ = sample
    ctx = dict(ctx, failures=[])
    line = first_line(run, ctx)
    assert line.startswith("[수집 실패 있음]") and "수집 오류 있음" in line


def test_dry_run_marked(sample):
    run, ctx, _ = sample
    ctx = dict(ctx, dry_run=True, report_path="out/2026-10-13.html")
    text = render_summary(run, ctx)
    assert text.startswith("[드라이런] [수집 실패 있음]")
    assert "드라이런: 저장소에 올리지 않음" in text


# ---------------------------------------------------------------- 근거 문구 (C6-6, bargain-rules §7)

def test_reason_lines_match_bargain_rules():
    trade = S.verdict(S.listing("1", 84.97, "1동", "10/20", "NORMAL", 93575, "a"),
                      ["TRADE"], 98500, None, 5.0, short=True)
    assert reason_lines(trade, 2) == ["일반층 실거래 중앙값 98,500만원 대비 5.0% 낮음 (최근 2건, 표본 부족)"]
    both = S.verdict(S.listing("1", 84.97, "1동", "10/20", "NORMAL", 90000, "a"),
                     ["TRADE", "LISTING"], 100000, 100000, 10.0)
    assert reason_lines(both, 3) == [
        "일반층 실거래 중앙값 100,000만원 대비 10.0% 낮음 (최근 3건)",
        "일반층 다른 매물 최저가 100,000만원 대비 10.0% 낮음",
    ]
    low = S.verdict(S.listing("1", 84.97, "1동", "저/20", "LOW", 89000, "a"),
                    ["LISTING"], None, 100000, 11.0, short=True)
    assert reason_lines(low, 0) == ["일반층 매물 최저가 100,000만원 대비 11.0% 낮음 (저층 기준 90%)"]


def test_report_contains_reason_text(sample):
    run, ctx, _ = sample
    html = render_report(run, ctx)
    assert "근거: 일반층 실거래 중앙값 270,000만원 대비 7.4% 낮음 (최근 3건)" in html
    assert "근거: 일반층 다른 매물 최저가 175,000만원 대비 8.6% 낮음" in html
    assert "근거: 일반층 실거래 중앙값 190,000만원 대비 5.3% 낮음 (최근 2건, 표본 부족)" in html


# ---------------------------------------------------------------- 입력 검사

def test_missing_context_key_raises(sample):
    run, ctx, _ = sample
    ctx = dict(ctx)
    del ctx["failures"]
    with pytest.raises(KeyError, match="failures"):
        render_report(run, ctx)


def test_price_drop_without_prior_raises(sample):
    run, ctx, _ = sample
    with pytest.raises(KeyError, match="prior_alerts"):
        render_report(run, dict(ctx, prior_alerts={}))


def test_unclassified_bargain_raises(sample):
    run, ctx, _ = sample
    run = replace(run, verdicts=[replace(v, alert_kind=None) for v in run.verdicts])
    with pytest.raises(ValueError, match="classify_alerts"):
        render_report(run, ctx)


def test_naive_started_at_raises(sample):
    run, ctx, _ = sample
    with pytest.raises(ValueError):
        render_report(replace(run, started_at=run.started_at.replace(tzinfo=None)), ctx)


# ---------------------------------------------------------------- 파일 쓰기와 이력 커밋 순서 (C6-3 조정판)

def test_write_outputs_paths(sample, tmp_path):
    run, ctx, _ = sample
    paths = write_outputs(run, ctx, report_dir=tmp_path / "reports", out_dir=tmp_path / "out", dry_run=False)
    assert paths["report"] == tmp_path / "reports" / "2026-10-13.html"
    assert paths["summary"] == tmp_path / "out" / "summary.md"
    assert "reports/2026-10-13.html (reports 브랜치)" in paths["summary"].read_text(encoding="utf-8")
    assert paths["report"].read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    dry = write_outputs(run, ctx, report_dir=tmp_path / "r2", out_dir=tmp_path / "o2", dry_run=True)
    assert dry["report"] == tmp_path / "o2" / "2026-10-13.html"
    assert not (tmp_path / "r2").exists()
    assert dry["summary"].read_text(encoding="utf-8").startswith("[드라이런]")


def run_like_pipeline(run, ctx, session, tmp_path):
    """pipeline이 지킬 순서: 파일 쓰기 성공 → commit_history."""
    write_outputs(run, ctx, report_dir=tmp_path / "reports", out_dir=tmp_path / "out", dry_run=False)
    return commit_history(session, run.verdicts, {f["complex_no"] for f in ctx["failures"]}, run.run_id,
                          dry_run=False, now=S.STARTED_AT)


def test_history_not_committed_when_writing_fails(sample, tmp_path, monkeypatch):
    run, ctx, session = sample
    before = _history(session)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(report_mod, "_atomic_write", boom)
    with pytest.raises(OSError):
        run_like_pipeline(run, ctx, session, tmp_path)
    assert _history(session) == before
    assert not (tmp_path / "reports" / "2026-10-13.html").exists()


def test_no_file_written_when_summary_render_fails(sample, tmp_path, monkeypatch):
    run, ctx, _ = sample
    import app.notify.summary as summary_mod

    monkeypatch.setattr(summary_mod, "render_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        write_outputs(run, ctx, report_dir=tmp_path / "reports", out_dir=tmp_path / "out", dry_run=False)
    assert not (tmp_path / "reports").exists() or not any((tmp_path / "reports").iterdir())


def test_history_committed_after_successful_write(sample, tmp_path):
    run, ctx, session = sample
    stats = run_like_pipeline(run, ctx, session, tmp_path)
    assert stats == {"NEW": 2, "PRICE_DROP": 1, "ONGOING": 1, "DEACTIVATED": 2, "SKIPPED_FAILED": 0, "dry_run": 0}
    h = _history(session)
    assert h[f"{S.HELIO}|84.95|301동|20/35|남향"][1] is True   # 실패 단지 이력 그대로


def _history(session) -> dict:
    from sqlalchemy import select

    from app.db.models import AlertHistoryRow

    session.expire_all()
    return {r.dedup_key: (r.last_alerted_price, r.active, r.deactivated_run_id, r.last_seen_run_id)
            for r in session.scalars(select(AlertHistoryRow))}

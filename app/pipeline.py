"""주간 파이프라인 진입점: `python -m app.pipeline --once [--dry-run]` (CLAUDE.md §1.1, §10).

처리 순서 (단지마다)
    fetch_complex -> fetch_listings -> dedup -> fetch_trades -> fetch_naver_trades·cross_check
    -> area_key별 judge·area_summary
그 다음 전체에 대해
    classify_alerts -> render_report·render_summary -> 파일 쓰기 -> (드라이런이 아니면) commit_history

실패 처리 (CLAUDE.md §7)
- 단지 하나가 실패해도 나머지 단지는 계속 처리한다. 일부 실패 = PARTIAL, 전부 실패 = FAILED.
- 실패 단지는 classify_alerts에 failed_complex_nos로 넘겨 이력을 건드리지 않게 한다.
- 국토부 실거래 수집이 실패하면 그 단지는 "수집 실패"다 (실거래 조건이 빠진 판정을 "급매 없음"처럼 보이게 하지 않기 위해).
- 네이버 실거래 교차검증 실패는 판정을 막지 않고 경고만 남긴다.
- 국토부 단지 매핑(molit_apt_seq)이 없는 단지는 실거래 없이(T_normal=None) 매물 기준으로만 판정하고,
  매핑 후보를 찾아 리포트 context에 넣는다.
- 네이버에서 CollectorError(stage="blocked")가 나면 같은 실행에서 남은 네이버 요청을 모두 중단한다.

종료 코드: OK=0, PARTIAL=1, FAILED=2. 다른 실행이 진행 중이면(락) 아무 파일도 쓰지 않고 2.

다른 모듈 함수는 호출 시점에 모듈 속성으로 찾는다 (`naver_listings.fetch_complex(...)`).
테스트는 이 속성을 monkeypatch로 바꿔 끼운다.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import literal_column, select, update
from sqlalchemy.orm import Session

from app import config
from app.collectors import molit_trades, naver_listings, naver_trades
from app.db import (
    AreaTypeRow,
    ComplexRow,
    ListingSnapshotRow,
    RunRow,
    TradeSnapshotRow,
    VerdictRow,
    make_engine,
    make_session_factory,
    upgrade_db,
)
from app.domain import dedup, rules
from app.domain.models import AreaType, Complex, Listing, RunResult, Trade, Verdict
from app.notify import history, report, summary

log = logging.getLogger("app.pipeline")

EXIT_CODES = {"OK": 0, "PARTIAL": 1, "FAILED": 2}
STALE_LOCK_AFTER = timedelta(hours=2)
SUMMARY_FILE = "summary.md"
OUT_REPORT_FILE = "report.html"


# ---------------------------------------------------------------------------
# 시각 (테스트에서 _utcnow를 바꿔 끼운다)
# ---------------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(UTC)


def now_kst() -> datetime:
    """업무 시간대(Asia/Seoul, 코드 상수) 기준 현재 시각. OS의 TZ와 무관."""
    return _utcnow().astimezone(config.BUSINESS_TZ)


# ---------------------------------------------------------------------------
# 비밀값 가리기 (CLAUDE.md §8): 오류 문자열은 DB·리포트·요약·reports 브랜치로 나간다.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(serviceKey=)[^&\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?\s*bearer\s+)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{8,}"), r"\1***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***@"),
]


def sanitize(message: str, secrets: Iterable[str | None] = ()) -> str:
    out = str(message)
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _is_blocked(exc: BaseException) -> bool:
    """CollectorError(stage="blocked") 판별. errors 모듈에 직접 의존하지 않도록 stage 속성으로 본다."""
    return getattr(exc, "stage", None) == "blocked"


def _describe(exc: BaseException) -> str:
    stage = getattr(exc, "stage", None)
    detail = getattr(exc, "detail", None)
    if stage:
        return f"[{stage}] {detail or exc}"
    return f"[{type(exc).__name__}] {exc}"


# ---------------------------------------------------------------------------
# runs 테이블 락 (CLAUDE.md C7-5)
# ---------------------------------------------------------------------------
class LockBusy(RuntimeError):
    def __init__(self, other_run_id: str, other_started_at: datetime):
        super().__init__(f"다른 실행({other_run_id}, 시작 {other_started_at.isoformat()})이 진행 중입니다.")
        self.other_run_id = other_run_id


def acquire_run_lock(
    session_factory, run_id: str, started_at: datetime, as_of: date, dry_run: bool
) -> list[str]:
    """RUNNING 행을 넣어 락을 잡는다. 성공하면 해제한 오래된 락의 run_id 목록을 돌려준다.

    1) 2시간 넘은 RUNNING은 비정상 종료로 보고 FAILED로 바꾼다.
    2) 내 RUNNING 행을 넣고 커밋한다.
    3) 살아 있는 RUNNING 중 rowid(=삽입 순서)가 가장 작은 실행만 진행한다. 나머지는 LockBusy.
       SQLite는 쓰기를 직렬화하므로 두 실행이 동시에 들어와도 둘 다 같은 승자를 본다.
    """
    cutoff = started_at - STALE_LOCK_AFTER
    released: list[str] = []
    with session_factory.begin() as s:
        stale = s.scalars(
            select(RunRow).where(RunRow.status == "RUNNING", RunRow.started_at < cutoff)
        ).all()
        for row in stale:
            row.status = "FAILED"
            row.finished_at = row.finished_at or started_at
            row.errors = list(row.errors or []) + [
                "비정상 종료: 2시간 넘게 RUNNING 상태로 남아 있어 다음 실행이 락을 해제함"
            ]
            released.append(row.run_id)
        s.add(
            RunRow(
                run_id=run_id,
                started_at=started_at,
                status="RUNNING",
                dry_run=dry_run,
                as_of=as_of,
                errors=[],
                cross_check_warnings=[],
                complex_results={},
            )
        )
    with session_factory() as s:
        winner = s.scalars(
            select(RunRow.run_id)
            .where(RunRow.status == "RUNNING", RunRow.started_at >= cutoff)
            .order_by(literal_column("rowid").asc())
            .limit(1)
        ).first()
        if winner != run_id:
            other = s.get(RunRow, winner) if winner else None
            other_started = other.started_at if other else started_at
    if winner != run_id:
        with session_factory.begin() as s:
            s.execute(
                update(RunRow)
                .where(RunRow.run_id == run_id)
                .values(
                    status="FAILED",
                    finished_at=_utcnow(),
                    errors=[f"중복 실행 차단: 실행 {winner}가 진행 중"],
                )
            )
        raise LockBusy(winner or "?", other_started)
    return released


# ---------------------------------------------------------------------------
# 단지 처리
# ---------------------------------------------------------------------------
@dataclass
class ComplexOutcome:
    complex_no: str
    name: str
    status: str = "FAILED"  # OK | FAILED
    error: str | None = None
    complex: Complex | None = None
    area_types: list[AreaType] = field(default_factory=list)
    listings: list[Listing] = field(default_factory=list)  # dedup 후 대표 매물
    raw_listing_count: int = 0
    trades: list[Trade] = field(default_factory=list)  # 국토부 (판정 기준)
    naver_trades: list[Trade] = field(default_factory=list)  # 교차검증용
    verdicts: list[Verdict] = field(default_factory=list)
    area_summaries: dict[float, dict] = field(default_factory=dict)
    mapping_needed: bool = False
    apt_seq_candidates: list[dict] = field(default_factory=list)
    mapping_error: str | None = None
    cross_check_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_context(self) -> dict[str, Any]:
        return {
            "complex_no": self.complex_no,
            "name": self.name,
            "status": self.status,
            "failed": self.status != "OK",
            "error": self.error,
            "complex": self.complex,
            "area_types": self.area_types,
            "area_summaries": self.area_summaries,
            "listings": self.listings,
            "raw_listing_count": self.raw_listing_count,
            "trades": self.trades,
            "naver_trades": self.naver_trades,
            "verdicts": self.verdicts,
            "mapping_needed": self.mapping_needed,
            "apt_seq_candidates": self.apt_seq_candidates,
            "mapping_error": self.mapping_error,
            "cross_check_error": self.cross_check_error,
        }


class _NaverBlocked(RuntimeError):
    pass


def _process_complex(
    entry: config.ComplexEntry,
    naver_client: Any,
    molit_client: Any,
    as_of: date,
    naver_state: dict,
    secrets: list[str | None],
) -> ComplexOutcome:
    """단지 하나를 처리한다. 수집·판정 실패는 예외로 올린다 (호출자가 격리)."""
    out = ComplexOutcome(complex_no=entry.complex_no, name=entry.name or entry.complex_no)

    def naver_call(fn, *args):
        if naver_state.get("blocked"):
            raise _NaverBlocked(
                "네이버 접속 차단(blocked)이 감지되어 이번 실행의 남은 네이버 요청을 중단함"
            )
        try:
            return fn(*args)
        except Exception as e:
            if _is_blocked(e):
                naver_state["blocked"] = True
            raise

    # 1) 단지·평형 (공급 119.0㎡ 이하만)
    cx, area_types = naver_call(naver_listings.fetch_complex, naver_client, entry.complex_no)
    if entry.molit_apt_seq:  # complexes.yaml 값이 우선 (사용자가 확인한 매핑)
        cx = dataclasses.replace(cx, molit_apt_seq=entry.molit_apt_seq)
    out.complex, out.area_types = cx, list(area_types)
    out.name = cx.name or out.name

    # 2) 매물 -> dedup
    raw = naver_call(naver_listings.fetch_listings, naver_client, cx, out.area_types)
    out.raw_listing_count = len(raw)
    out.listings = dedup.dedup(raw)

    # 3) 국토부 실거래 (판정 기준). 실패하면 단지 전체 실패.
    if cx.molit_apt_seq:
        out.trades = list(molit_trades.fetch_trades(molit_client, cx, out.area_types, as_of))
        # 4) 네이버 실거래 교차검증: 실패해도 판정은 진행, 경고만.
        try:
            out.naver_trades = list(
                naver_call(naver_trades.fetch_naver_trades, naver_client, cx, out.area_types)
            )
            out.warnings.extend(
                f"{out.name}: {w}" for w in naver_trades.cross_check(out.trades, out.naver_trades, as_of)
            )
        except Exception as e:  # 교차검증만 실패 — 숨기지 않고 경고로 남김
            out.cross_check_error = sanitize(_describe(e), secrets)
            out.warnings.append(f"{out.name}: 네이버 실거래 교차검증 실패 {out.cross_check_error}")
    else:
        # 국토부 단지 매핑 없음: 실거래 조건 없이 매물 기준으로만 판정 + 후보 제시
        out.mapping_needed = True
        try:
            out.apt_seq_candidates = list(molit_trades.find_apt_seq_candidates(molit_client, cx, as_of))
        except Exception as e:
            out.mapping_error = sanitize(_describe(e), secrets)
        msg = (
            f"{out.name}: 실거래 매칭 확인 필요 — 국토부 단지 식별자(molit_apt_seq)가 없어 "
            f"실거래 조건 없이 매물 기준으로만 판정함. 후보 {len(out.apt_seq_candidates)}개"
        )
        if out.mapping_error:
            msg += f" (후보 조회 실패 {out.mapping_error})"
        out.warnings.append(msg + ". config/complexes.yaml에 molit_apt_seq를 적어 주세요.")

    # 5) area_key별 판정
    keys: list[float] = [a.area_key for a in out.area_types]
    stray = sorted({l.area_key for l in out.listings} - set(keys))
    if stray:
        out.warnings.append(f"{out.name}: 평형 목록에 없는 area_key의 매물이 있음 {stray} (따로 판정함)")
        keys += stray
    verdicts: list[Verdict] = []
    for k in keys:
        ls = [l for l in out.listings if l.area_key == k]
        ts = [t for t in out.trades if t.area_key == k]
        verdicts.extend(rules.judge(ls, ts, as_of))
        out.area_summaries[k] = rules.area_summary(ls, ts, as_of)
    out.verdicts = verdicts
    out.status = "OK"
    return out


def _decide_status(outcomes: list[ComplexOutcome]) -> str:
    if not outcomes:
        return "FAILED"
    ok = sum(1 for o in outcomes if o.status == "OK")
    if ok == len(outcomes):
        return "OK"
    return "PARTIAL" if ok else "FAILED"


# ---------------------------------------------------------------------------
# 스냅샷 저장 (드라이런이 아닐 때만)
# ---------------------------------------------------------------------------
def _save_snapshots(s: Session, run_id: str, outcomes: list[ComplexOutcome], verdicts: list[Verdict]) -> None:
    now = _utcnow()
    for o in outcomes:
        if o.status != "OK" or o.complex is None:
            continue
        cx = o.complex
        row = s.get(ComplexRow, cx.complex_no)
        values = dict(
            name=cx.name, lawd_cd=cx.lawd_cd, address=cx.address, max_floor=cx.max_floor,
            molit_apt_seq=cx.molit_apt_seq, updated_at=now,
        )
        if row is None:
            s.add(ComplexRow(complex_no=cx.complex_no, **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        s.flush()
        existing = {r.area_key: r for r in s.scalars(select(AreaTypeRow).where(AreaTypeRow.complex_no == cx.complex_no))}
        for a in o.area_types:
            r = existing.get(a.area_key)
            if r is None:
                s.add(AreaTypeRow(complex_no=a.complex_no, area_key=a.area_key, exclusive_m2=a.exclusive_m2,
                                  supply_m2=a.supply_m2, pyeong=a.pyeong, type_name=a.type_name, updated_at=now))
            else:
                r.exclusive_m2, r.supply_m2, r.pyeong, r.type_name, r.updated_at = (
                    a.exclusive_m2, a.supply_m2, a.pyeong, a.type_name, now)
        for t in o.trades + o.naver_trades:
            s.add(TradeSnapshotRow(run_id=run_id, **dataclasses.asdict(t)))

    snap_ids: dict[str, int] = {}
    for o in outcomes:
        if o.status != "OK":
            continue
        for l in o.listings:
            row = ListingSnapshotRow(run_id=run_id, **dataclasses.asdict(l))
            s.add(row)
            s.flush()
            snap_ids[l.dedup_key] = row.id
    for v in verdicts:
        sid = snap_ids.get(v.listing.dedup_key)
        if sid is None:
            raise RuntimeError(f"판정 결과의 매물이 스냅샷에 없음: {v.listing.dedup_key}")
        s.add(VerdictRow(run_id=run_id, listing_snapshot_id=sid, dedup_key=v.listing.dedup_key,
                         is_bargain=v.is_bargain, reasons=list(v.reasons), trade_base=v.trade_base,
                         listing_base=v.listing_base, discount_pct=v.discount_pct,
                         trade_sample_short=v.trade_sample_short, alert_kind=v.alert_kind))
    s.flush()


# ---------------------------------------------------------------------------
# 파일 쓰기
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _fallback_summary(status: str, run_id: str, as_of: date | None, errors: list[str]) -> str:
    """요약 렌더링 전에(또는 렌더링 자체가) 실패했을 때 쓰는 최소 요약. 첫 줄에 상태를 적는다."""
    lines = [
        f"[{status}] 수집 실패 — 급매 리포트를 만들지 못했습니다 (실행 {run_id}, 기준일 {as_of or '-'})",
        "",
        "이번 실행 결과를 '급매 없음'으로 보면 안 됩니다. docs/RUNBOOK.md를 보고 조치하세요.",
        "",
        "## 오류",
    ]
    lines += [f"- {e}" for e in errors] or ["- (상세 없음)"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def run_once(settings: config.Settings, dry_run: bool) -> int:
    started_at = now_kst()
    as_of = started_at.date()  # KST 날짜 (CLAUDE.md §9)
    run_id = f"{started_at:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    secrets = [settings.molit_api_key]
    out_dir = settings.out_dir

    def fail_early(msg: str) -> int:
        msg = sanitize(msg, secrets)
        log.error(msg)
        _atomic_write(out_dir / SUMMARY_FILE, _fallback_summary("FAILED", run_id, as_of, [msg]))
        return EXIT_CODES["FAILED"]

    # --- 설정·DB 준비
    try:
        entries = config.load_complexes(settings.complexes_file)
        api_key = settings.require_molit_api_key()
    except config.ConfigError as e:
        return fail_early(f"설정 오류: {e}")
    try:
        upgrade_db(settings.db_path)
        engine = make_engine(settings.db_path)
    except Exception as e:
        return fail_early(f"DB 준비 실패 ({settings.db_path}): {_describe(e)}")
    Session_ = make_session_factory(engine)

    try:
        try:
            released = acquire_run_lock(Session_, run_id, started_at, as_of, dry_run)
        except LockBusy as e:
            log.error("%s 이번 실행은 중단합니다 (out/·DB 이력 변경 없음).", e)
            return EXIT_CODES["FAILED"]
        for rid in released:
            log.warning("오래된 RUNNING 락 해제: %s", rid)
        return _run_locked(settings, dry_run, Session_, entries, api_key, run_id, started_at, as_of, secrets)
    finally:
        engine.dispose()


def _run_locked(settings, dry_run, Session_, entries, api_key, run_id, started_at, as_of, secrets) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    outcomes: list[ComplexOutcome] = []
    report_path: Path | None = None
    status = "FAILED"
    clients: list[Any] = []

    try:
        try:
            naver_client = naver_listings.NaverClient()
            clients.append(naver_client)
            molit_client = molit_trades.MolitClient(api_key)
            clients.append(molit_client)
        except Exception as e:
            raise RuntimeError(f"수집 클라이언트 생성 실패: {_describe(e)}") from e

        naver_state: dict[str, bool] = {"blocked": False}
        for entry in entries:
            label = entry.name or entry.complex_no
            try:
                o = _process_complex(entry, naver_client, molit_client, as_of, naver_state, secrets)
            except Exception as e:  # 단지 단위 격리 (CLAUDE.md §7)
                msg = sanitize(_describe(e), secrets)
                o = ComplexOutcome(complex_no=entry.complex_no, name=label, status="FAILED", error=msg)
                errors.append(f"{label}({entry.complex_no}): 수집 실패 {msg}")
                log.error("단지 %s(%s) 수집 실패: %s", label, entry.complex_no, msg)
            outcomes.append(o)
            warnings.extend(sanitize(w, secrets) for w in o.warnings)

        failed = {o.complex_no for o in outcomes if o.status != "OK"}
        all_verdicts = [v for o in outcomes if o.status == "OK" for v in o.verdicts]
        status = _decide_status(outcomes)

        with Session_() as hs:
            classified, dropped = history.classify_alerts(hs, all_verdicts, failed, run_id, dry_run)
            run = RunResult(run_id=run_id, started_at=started_at, status=status, verdicts=list(classified),
                            errors=list(errors), cross_check_warnings=list(warnings))
            context = _build_context(settings, dry_run, as_of, outcomes, failed, dropped, naver_state)
            html = report.render_report(run, context)
            md = summary.render_summary(run, context)

            # 파일 쓰기: out/ 은 항상, reports/ 는 드라이런이 아닐 때만
            _atomic_write(settings.out_dir / OUT_REPORT_FILE, html)
            if not dry_run:
                report_path = settings.report_dir / f"{as_of.isoformat()}.html"
                _atomic_write(report_path, html)
            _atomic_write(settings.out_dir / SUMMARY_FILE, md)

            # 파일 쓰기가 성공한 뒤에만 이력 커밋 (C6-3 조정판)
            if not dry_run:
                _save_snapshots(hs, run_id, outcomes, list(classified))
                history.commit_history(hs, verdicts=list(classified), failed_complex_nos=failed,
                                       run_id=run_id, dropped=dropped)
                hs.commit()
            else:
                hs.rollback()
    except Exception as e:
        msg = sanitize(f"파이프라인 오류: {_describe(e)}", secrets)
        errors.append(msg)
        log.exception("파이프라인 오류")
        status = "FAILED"
        _atomic_write(settings.out_dir / SUMMARY_FILE, _fallback_summary("FAILED", run_id, as_of, errors))
    finally:
        for c in clients:
            close = getattr(c, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # 정리 실패는 결과에 영향 없음, 로그만
                    log.warning("클라이언트 정리 실패: %s", sanitize(_describe(e), secrets))

    _finish_run(Session_, run_id, status, errors, warnings, outcomes, report_path)
    log.info("실행 %s 종료: %s (단지 %d개, 실패 %d개, 드라이런=%s)", run_id, status, len(outcomes),
             sum(1 for o in outcomes if o.status != "OK"), dry_run)
    return EXIT_CODES[status]


def _build_context(settings, dry_run, as_of, outcomes, failed, dropped, naver_state) -> dict[str, Any]:
    failed_list = [
        {"complex_no": o.complex_no, "name": o.name, "error": o.error} for o in outcomes if o.status != "OK"
    ]
    return {
        "as_of": as_of,
        "dry_run": dry_run,
        "business_tz": config.BUSINESS_TZ_NAME,
        "complexes": [o.as_context() for o in outcomes],
        "complex_count": len(outcomes),
        "failed_complex_nos": set(failed),
        "failed_complexes": failed_list,
        "dropped": list(dropped),
        "mapping_needed": [o.as_context() for o in outcomes if o.status == "OK" and o.mapping_needed],
        "naver_blocked": bool(naver_state.get("blocked")),
        "complexes_file": "config/complexes.yaml",
    }


def _finish_run(Session_, run_id, status, errors, warnings, outcomes, report_path) -> None:
    results = {
        o.complex_no: {
            "name": o.name,
            "status": o.status,
            "error": o.error,
            "listing_count": len(o.listings),
            "raw_listing_count": o.raw_listing_count,
            "trade_count": len(o.trades),
            "mapping_needed": o.mapping_needed,
        }
        for o in outcomes
    }
    with Session_.begin() as s:
        row = s.get(RunRow, run_id)
        row.status = status
        row.finished_at = _utcnow()
        row.errors = list(errors)
        row.cross_check_warnings = list(warnings)
        row.complex_results = results
        row.report_path = str(report_path) if report_path else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.pipeline", description="네이버 부동산 급매 주간 리포트 1회 실행")
    p.add_argument("--once", action="store_true", required=True, help="1회 실행하고 종료 (필수)")
    p.add_argument("--dry-run", action="store_true",
                   help="이력 DB를 바꾸지 않고 out/ 에만 리포트·요약을 쓴다 (.env의 DRY_RUN=true와 같음)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as e:  # argparse 오류는 FAILED로
        return EXIT_CODES["FAILED"] if e.code else 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = config.load_settings()
    except config.ConfigError as e:
        log.error("설정 오류: %s", e)
        try:
            _atomic_write(config.PROJECT_ROOT / config.DEFAULT_OUT_DIR / SUMMARY_FILE,
                          _fallback_summary("FAILED", "-", None, [f"설정 오류: {e}"]))
        except OSError:
            pass  # 요약도 못 쓰면 로그와 종료 코드 2로만 알린다
        return EXIT_CODES["FAILED"]
    dry_run = bool(args.dry_run or settings.dry_run)
    try:
        return run_once(settings, dry_run)
    except Exception as e:  # 예상 못 한 오류도 종료 코드 2 (1=PARTIAL과 섞이지 않게)
        log.error("예상하지 못한 오류: %s", sanitize(_describe(e), [settings.molit_api_key]))
        return EXIT_CODES["FAILED"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

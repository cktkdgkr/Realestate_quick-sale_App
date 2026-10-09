"""HTML 주간 리포트 생성. 담당: 알림·리포트 agent.

- ``render_report(run, context) -> str``: 단일 HTML (인라인 스타일, 외부 CSS·JS 없음, 폭 600px 기준).
- ``write_outputs(run, context, ...)``: 리포트와 out/summary.md를 원자적으로 쓴다.
  이것이 예외 없이 끝난 뒤에만 ``app.notify.history.commit_history``를 부른다.

context 키는 docs/handoff/notifier.md "context 계약" 참고. 요약은 아래와 같다.

필수
- complexes: list[Complex]                       조사 대상 단지 (표시 순서). 수집 실패로 Complex가 없으면 빼도 된다.
- area_types: dict[complex_no, list[AreaType]]
- area_summaries: dict[(complex_no, area_key), dict]   rules.area_summary 결과
- gone: list[dict]                               classify_alerts의 두 번째 반환값
- failures: list[dict]                           {complex_no, name?, stage, detail?, area_key?}
선택
- prior_alerts: dict[dedup_key, dict]            history.prior_alerts 결과 (PRICE_DROP이 있으면 필수)
- molit_candidates: dict[complex_no, list[dict]] molit_apt_seq 미설정 단지의 후보
                                                 (키: apt_seq, apt_nm, umd_nm, jibun, trade_count, last_contract)
- trades: dict[(complex_no, area_key), list[Trade]]   저층 실거래 참고 표시용
- collector_warnings: list[str]                  수집기 경고 (평형 미매칭, 매물 수 급감 등. CLAUDE.md §9)
- dry_run: bool, report_path: str, complexes_file: str
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.config import BUSINESS_TZ
from app.domain import normalize
from app.domain.models import AreaType, Complex, RunResult, Trade, Verdict

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

KIND_LABEL = {"NEW": "신규", "PRICE_DROP": "가격 인하", "ONGOING": "지속 중"}
FLOOR_LABEL = {"LOW": "저층", "NORMAL": "일반층", "UNKNOWN": "층 미상"}
STATUS_LABEL = {"OK": "정상", "PARTIAL": "일부 수집 실패", "FAILED": "수집 실패"}
STAGE_LABEL = {
    "blocked": "네이버 접속 차단 의심",
    "schema_changed": "네이버 응답 형식 변경 의심",
    "network": "네트워크 오류",
    "molit_api": "국토부 실거래 API 오류",
    "molit_auth": "국토부 API 인증키 오류",
    "complex_mapping": "국토부 단지 매핑 실패",
}
WEEKDAYS = "월화수목금토일"
DEFAULT_COMPLEXES_FILE = "config/complexes.yaml"

REQUIRED_KEYS = ("complexes", "area_types", "area_summaries", "gone", "failures")

_SECRET_RE = re.compile(r"(?i)(servicekey|api[_-]?key|token|password)=([^&\s]+)")


# ---------------------------------------------------------------- 공용 표시 함수

def fmt_price(manwon: int | None) -> str:
    """가격 표시는 normalize.format_price 하나로 통일한다 (notify 스킬 §4)."""
    if manwon is None:
        return "-"
    return normalize.format_price(manwon)


def pct_below(price: int, base: int) -> float:
    """표시용 할인율 (판정에는 쓰지 않는다).

    CLAUDE.md §9 rules ④와 같은 식: (base - price) / base × 100을 정수 연산으로
    소수 첫째 자리 ROUND_HALF_UP. Verdict.discount_pct와 같은 값이 나오도록 맞춘다.
    """
    if base <= 0:
        raise ValueError(f"기준가가 0 이하입니다: {base}")
    num = (base - price) * 1000
    if num >= 0:
        tenths = (2 * num + base) // (2 * base)
    else:
        tenths = -((-2 * num + base) // (2 * base))
    return tenths / 10


def reason_lines(v: Verdict, trade_sample_count: int | None) -> list[str]:
    """판정 근거 문구 (bargain-rules §7)."""
    lines: list[str] = []
    price = v.listing.price
    for r in v.reasons:
        if r == "TRADE":
            if v.trade_base is None:
                raise ValueError(f"TRADE 근거인데 trade_base가 없습니다: {v.listing.dedup_key}")
            n = "?" if trade_sample_count is None else str(trade_sample_count)
            short = ", 표본 부족" if v.trade_sample_short else ""
            lines.append(
                f"일반층 실거래 중앙값 {v.trade_base:,}만원 대비 "
                f"{pct_below(price, v.trade_base)}% 낮음 (최근 {n}건{short})"
            )
        elif r == "LISTING":
            if v.listing_base is None:
                raise ValueError(f"LISTING 근거인데 listing_base가 없습니다: {v.listing.dedup_key}")
            p = pct_below(price, v.listing_base)
            if v.listing.floor_group == "LOW":
                lines.append(f"일반층 매물 최저가 {v.listing_base:,}만원 대비 {p}% 낮음 (저층 기준 90%)")
            else:
                lines.append(f"일반층 다른 매물 최저가 {v.listing_base:,}만원 대비 {p}% 낮음")
        else:
            raise ValueError(f"알 수 없는 근거: {r!r}")
    return lines


def redact(text: str) -> str:
    """오류 문자열에 섞인 비밀값(serviceKey, api_key, token, password 파라미터 값)을 가린다 (CLAUDE.md §8)."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", str(text))


def run_date(run: RunResult) -> date:
    """리포트 날짜 = 실행 시작 시각의 KST 날짜."""
    if run.started_at.tzinfo is None:
        raise ValueError("RunResult.started_at에는 시간대가 있어야 합니다 (Asia/Seoul).")
    return run.started_at.astimezone(BUSINESS_TZ).date()


def date_label(d: date) -> str:
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})"


def stage_label(stage: str | None) -> str:
    return STAGE_LABEL.get(stage or "", stage or "원인 미상")


# ---------------------------------------------------------------- view model

def _check_context(context: Mapping) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in context]
    if missing:
        raise KeyError(f"리포트 context에 필수 키가 없습니다: {missing}")


def failed_complex_nos(context: Mapping) -> set[str]:
    return {str(f["complex_no"]) for f in context.get("failures", [])}


def _area_label(at: AreaType | None, area_key: float) -> str:
    if at is None:
        return f"전용 {area_key:g}㎡"
    name = f" {at.type_name}" if at.type_name else ""
    return f"{at.pyeong}평{name} (전용 {at.exclusive_m2:g}㎡)"


def _counts(verdicts: list[Verdict]) -> dict[str, int]:
    c = {"NEW": 0, "PRICE_DROP": 0, "ONGOING": 0}
    for v in verdicts:
        if v.is_bargain and v.alert_kind in c:
            c[v.alert_kind] += 1
    return c


def _failure_text(f: Mapping, at_by_key: Mapping) -> str:
    """실패 항목 한 줄: [평형: ]원인[: 상세]. 평형이 AreaType과 맞지 않아도 area_key로 적는다."""
    text = stage_label(f.get("stage")) + (f": {redact(f['detail'])}" if f.get("detail") else "")
    ak = f.get("area_key")
    if ak is not None:
        text = f"{_area_label(at_by_key.get((str(f['complex_no']), ak)), ak)} — {text}"
    return text


def build_title(run: RunResult, context: Mapping) -> str:
    """리포트 <title>과 summary.md 첫 줄 (notify 스킬 §4 이메일 제목 형식을 옮김)."""
    d = date_label(run_date(run))
    c = _counts(run.verdicts)
    failed = failed_complex_nos(context)
    has_failure = bool(failed) or run.status != "OK"
    prefix = ""
    if context.get("dry_run"):
        prefix += "[드라이런] "
    if run.status == "FAILED":
        prefix += "[수집 실패] "
    elif has_failure:
        prefix += "[수집 실패 있음] "
    if failed:
        fail_text = f"수집 실패 {len(failed)}단지"
    elif has_failure:
        fail_text = "수집 오류 있음"
    else:
        fail_text = "수집 실패 없음"
    if run.status == "FAILED":
        counts = f"급매 판정 못 함 (신규 {c['NEW']} · 인하 {c['PRICE_DROP']})"
    else:
        counts = f"신규 {c['NEW']} · 인하 {c['PRICE_DROP']} · 지속 {c['ONGOING']}"
    return f"{prefix}[급매 리포트] {d} — 상태 {run.status} · {counts} · {fail_text}"


def build_view(run: RunResult, context: Mapping) -> dict:
    """템플릿과 요약이 함께 쓰는 표시용 데이터."""
    _check_context(context)
    complexes: list[Complex] = list(context["complexes"])
    area_types: Mapping[str, list[AreaType]] = context["area_types"]
    summaries: Mapping = context["area_summaries"]
    prior: Mapping[str, dict] = context.get("prior_alerts") or {}
    candidates: Mapping[str, list[dict]] = context.get("molit_candidates") or {}
    trades: Mapping = context.get("trades") or {}
    failures = [dict(f) for f in context["failures"]]
    failed = failed_complex_nos(context)
    complexes_file = context.get("complexes_file") or DEFAULT_COMPLEXES_FILE

    cx_by_no = {c.complex_no: c for c in complexes}
    at_by_key = {
        (cno, at.area_key): at for cno, ats in area_types.items() for at in ats
    }

    def cx_name(cno: str) -> str:
        if cno in cx_by_no:
            return cx_by_no[cno].name
        for f in failures:
            if str(f["complex_no"]) == cno and f.get("name"):
                return str(f["name"])
        return f"단지 {cno}"

    def sample_count(cno: str, ak: float) -> int | None:
        s = summaries.get((cno, ak))
        return None if s is None else s.get("trade_sample_count")

    def item(v: Verdict) -> dict:
        lst = v.listing
        prev = None
        if v.alert_kind == "PRICE_DROP":
            if lst.dedup_key not in prior:
                raise KeyError(f"가격 인하 매물의 이전 알림가가 context['prior_alerts']에 없습니다: {lst.dedup_key}")
            prev = prior[lst.dedup_key]["last_alerted_price"]
        return {
            "kind": v.alert_kind,
            "kind_label": KIND_LABEL.get(v.alert_kind or "", ""),
            "complex_name": cx_name(lst.complex_no),
            "complex_no": lst.complex_no,
            "area_label": _area_label(at_by_key.get((lst.complex_no, lst.area_key)), lst.area_key),
            "dong": lst.dong or "동 미상",
            "floor_raw": lst.floor_raw or "-",
            "floor_label": FLOOR_LABEL[lst.floor_group],
            "direction": lst.direction,
            "price_text": fmt_price(lst.price),
            "prev_price_text": fmt_price(prev) if prev is not None else None,
            "discount_pct": v.discount_pct,
            "reasons": reason_lines(v, sample_count(lst.complex_no, lst.area_key)),
            "trade_sample_short": v.trade_sample_short,
            "realtor_count": lst.realtor_count,
            "alt_prices_text": ", ".join(fmt_price(p) for p in sorted(lst.alt_prices)),
            "confirmed_at": lst.confirmed_at.isoformat() if lst.confirmed_at else None,
            "url": lst.url,
        }

    def sort_key(v: Verdict):
        return (-(v.discount_pct or 0.0), v.listing.complex_no, v.listing.area_key, v.listing.price)

    bargains = sorted((v for v in run.verdicts if v.is_bargain), key=sort_key)
    alerts = [item(v) for v in bargains if v.alert_kind in ("NEW", "PRICE_DROP")]
    alerts.sort(key=lambda x: 0 if x["kind"] == "NEW" else 1)
    ongoing = [item(v) for v in bargains if v.alert_kind == "ONGOING"]
    unclassified = [v for v in bargains if v.alert_kind not in KIND_LABEL]
    if unclassified:
        raise ValueError("alert_kind가 비어 있는 급매가 있습니다. classify_alerts를 먼저 호출하세요.")

    gone = []
    for g in context["gone"]:
        cno = str(g["complex_no"])
        lst = g.get("listing")
        gone.append({
            "complex_name": cx_name(cno),
            "area_label": _area_label(at_by_key.get((cno, g["area_key"])), g["area_key"]),
            "last_price_text": fmt_price(g["last_alerted_price"]),
            "first_alerted": g["first_alerted_at"].astimezone(BUSINESS_TZ).date().isoformat()
            if g.get("first_alerted_at") else None,
            "reason_text": "매물이 내려감 (이번 수집에 없음)" if g.get("reason") == "GONE"
            else "급매 조건을 벗어남",
            "current_price_text": fmt_price(lst.price) if lst is not None else None,
            "where": f"{lst.dong or '동 미상'} · {lst.floor_raw or '-'}" if lst is not None else None,
            "dedup_key": g["dedup_key"],
            "url": lst.url if lst is not None else None,
        })

    unknown_by_cx: dict[str, list[Verdict]] = {}
    for v in run.verdicts:
        if v.listing.floor_group == "UNKNOWN":
            unknown_by_cx.setdefault(v.listing.complex_no, []).append(v)

    # 단지·평형별 현황 (표시 순서: context["complexes"], 그 뒤에 Complex 없는 실패 단지)
    order = [c.complex_no for c in complexes]
    for f in failures:
        cno = str(f["complex_no"])
        if cno not in order:
            order.append(cno)

    status_rows = []
    for cno in order:
        cx = cx_by_no.get(cno)
        cx_failures = [f for f in failures if str(f["complex_no"]) == cno]
        # CLAUDE.md §9 (실패 단위): 실패 항목이 하나라도 있으면 단지 전체가 수집 실패다.
        # area_key가 AreaType과 맞지 않거나 Complex가 없어도 이름과 함께 "수집 실패"로 표시한다.
        area_fail = {f["area_key"]: f for f in cx_failures if f.get("area_key") is not None}
        areas = []
        for at in sorted(area_types.get(cno, []), key=lambda a: a.area_key):
            s = summaries.get((cno, at.area_key))
            af = area_fail.get(at.area_key)
            low_ref = None
            low_trades = sorted(
                (t for t in trades.get((cno, at.area_key), []) if t.floor_group == "LOW" and not t.cancelled),
                key=lambda t: t.contract_date, reverse=True,
            )
            if low_trades:
                t: Trade = low_trades[0]
                low_ref = f"{fmt_price(t.price)} ({t.contract_date.isoformat()}, {t.floor}층, {t.deal_type})"
            areas.append({
                "label": _area_label(at, at.area_key),
                "failed": af is not None,
                "failure_text": (stage_label(af.get("stage")) + (f": {redact(af['detail'])}" if af.get("detail") else ""))
                if af else None,
                "missing": s is None and af is None,
                "t_normal_text": fmt_price(s["t_normal"]) if s and s.get("t_normal") is not None else None,
                "trade_count": s.get("trade_sample_count", 0) if s else 0,
                "no_trade": bool(s) and s.get("t_normal") is None,
                "short": bool(s) and bool(s.get("trade_sample_short")) and s.get("t_normal") is not None,
                "l_normal_text": fmt_price(s.get("l_normal_min")) if s else "-",
                "l_low_text": fmt_price(s.get("l_low_min")) if s else "-",
                "listing_count": s.get("listing_count", 0) if s else 0,
                "unknown_count": s.get("unknown_count", 0) if s else 0,
                "low_trade_ref": low_ref,
            })
        cands = []
        for c in candidates.get(cno, []):
            # 키는 find_apt_seq_candidates 기준으로 고정 (CLAUDE.md §10). score·hints 등 나머지는 표시하지 않는다.
            place = " ".join(str(c[k]) for k in ("umd_nm", "jibun") if c.get(k))
            desc = f"{c.get('apt_nm') or '-'}" + (f" ({place})" if place else "")
            if c.get("trade_count") is not None:
                desc += f" · 거래 {c['trade_count']}건"
            if c.get("last_contract"):
                desc += f" · 최근 계약 {c['last_contract']}"
            cands.append({"seq": c["apt_seq"], "desc": desc})
        status_rows.append({
            "complex_no": cno,
            "name": cx_name(cno),
            "address": cx.address if cx else "",
            "failed": bool(cx_failures),
            "failure_text": "; ".join(_failure_text(f, at_by_key) for f in cx_failures),
            "failure_items": [
                {
                    "area_label": _area_label(at_by_key.get((cno, f["area_key"])), f["area_key"])
                    if f.get("area_key") is not None else None,
                    "text": stage_label(f.get("stage")) + (f": {redact(f['detail'])}" if f.get("detail") else ""),
                }
                for f in cx_failures
            ],
            "molit_missing": cx is not None and not cx.molit_apt_seq,
            "candidates": cands,
            "areas": areas,
            "unknown_listings": [
                {
                    "area_label": _area_label(at_by_key.get((cno, v.listing.area_key)), v.listing.area_key),
                    "where": f"{v.listing.dong or '동 미상'} · "
                    + (f"층 표기 '{v.listing.floor_raw}'" if v.listing.floor_raw else "층 표기 없음"),
                    "price_text": fmt_price(v.listing.price),
                    "url": v.listing.url,
                }
                for v in sorted(unknown_by_cx.get(cno, []), key=lambda v: (v.listing.area_key, v.listing.price))
            ],
        })

    counts = _counts(run.verdicts)
    no_trade_areas = sum(1 for r in status_rows for a in r["areas"] if a["no_trade"])
    unknown_total = sum(a["unknown_count"] for r in status_rows for a in r["areas"])
    molit_missing = [r for r in status_rows if r["molit_missing"]]
    failed_rows = [r for r in status_rows if r["failed"]]

    return {
        "title": build_title(run, context),
        "date_label": date_label(run_date(run)),
        "run_id": run.run_id,
        "status": run.status,
        "status_label": STATUS_LABEL.get(run.status, run.status),
        "dry_run": bool(context.get("dry_run")),
        "complex_count": len(order),
        "counts": counts,
        "alerts": alerts,
        "ongoing": ongoing,
        "gone": gone,
        "status_rows": status_rows,
        "failed_rows": failed_rows,
        "has_failure": bool(failed) or run.status != "OK",
        "no_trade_areas": no_trade_areas,
        "unknown_total": unknown_total,
        "molit_missing": molit_missing,
        "complexes_file": complexes_file,
        "errors": [redact(e) for e in run.errors],
        "warnings": [redact(w) for w in run.cross_check_warnings],
        "collector_warnings": [redact(w) for w in context.get("collector_warnings") or []],
        "report_path": context.get("report_path"),
    }


# ---------------------------------------------------------------- 렌더링

def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_report(run: RunResult, context: dict) -> str:
    """단일 HTML 리포트. 본문 순서는 notify 스킬 §4의 1~7 (웹 링크 제외)."""
    view = build_view(run, context)
    return _env().get_template("report.html.j2").render(v=view)


def report_filename(run: RunResult) -> str:
    return f"{run_date(run).isoformat()}.html"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_outputs(
    run: RunResult,
    context: dict,
    *,
    report_dir: Path,
    out_dir: Path,
    dry_run: bool,
) -> dict[str, Path]:
    """리포트와 summary.md를 쓴다. 반환: {"report": 경로, "summary": 경로}.

    - dry_run=False: 리포트는 report_dir/YYYY-MM-DD.html (reports 브랜치 worktree)
    - dry_run=True : 리포트는 out_dir/YYYY-MM-DD.html (저장소에 올리지 않음)
    - 요약은 항상 out_dir/summary.md
    렌더링을 모두 끝낸 뒤 쓰기 시작하고, 각 파일은 임시 파일 → rename으로 원자적으로 바꾼다.
    예외 없이 반환된 경우에만 commit_history를 호출한다.
    """
    from app.notify.summary import render_summary

    target_dir = Path(out_dir if dry_run else report_dir)
    report_path = target_dir / report_filename(run)
    ctx = dict(context)
    ctx["dry_run"] = dry_run
    ctx["report_path"] = f"{target_dir.name}/{report_path.name}"
    html = render_report(run, ctx)
    summary = render_summary(run, ctx)
    summary_path = Path(out_dir) / "summary.md"
    _atomic_write(report_path, html)
    _atomic_write(summary_path, summary)
    return {"report": report_path, "summary": summary_path}

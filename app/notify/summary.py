"""실행 요약(out/summary.md) 생성. 담당: 알림·리포트 agent.

Routine 완료 알림으로 사용자에게 그대로 전달된다 (CLAUDE.md §1.1).
첫 줄 = 리포트 <title>과 같은 문장: 상태, 신규·인하 건수, 수집 실패 여부 (C6-4 조정판).
수집 실패는 첫 줄 맨 앞과 본문 맨 위에 둔다. "급매 없음"처럼 보이지 않게 한다 (CLAUDE.md §7).
context 키는 app/notify/report.py와 같다.
"""

from __future__ import annotations

from app.domain.models import RunResult
from app.notify.report import build_view


def _item_line(it: dict) -> str:
    if it["kind"] == "PRICE_DROP":
        price = f"{it['prev_price_text']} → {it['price_text']}"
    else:
        price = it["price_text"]
    reasons = " / ".join(it["reasons"])
    return (
        f"- [{it['kind_label']}] {it['complex_name']} {it['area_label']} "
        f"{it['dong']} {it['floor_raw']}({it['floor_label']}) — {price}\n"
        f"  근거: {reasons}\n"
        f"  {it['url']}"
    )


def render_summary(run: RunResult, context: dict) -> str:
    v = build_view(run, context)
    c = v["counts"]
    out: list[str] = [v["title"], ""]

    if v["has_failure"]:
        out.append("## 수집 실패")
        if v["status"] == "FAILED":
            out.append("- 이번 실행은 수집에 실패해 급매 판정을 하지 못했습니다. \"급매 없음\"이 아닙니다.")
        # 실패 단위는 단지다 (CLAUDE.md §9). 실패 항목마다 단지 이름과(있으면) 평형을 적는다.
        for r in v["failed_rows"]:
            for f in r["failure_items"]:
                where = f"{r['name']} {f['area_label']}" if f["area_label"] else f"{r['name']}({r['complex_no']})"
                out.append(f"- {where}: 수집 실패 — {f['text']}.")
            out.append(f"  → {r['name']}은(는) 이번 주 판정에서 빠졌습니다 (\"급매 없음\" 아님).")
        if not v["failed_rows"] and v["status"] != "FAILED":
            out.append("- 일부 수집 오류가 있습니다. 아래 오류 목록과 리포트를 확인하세요.")
        out.append("")

    out.append("## 실행 결과")
    out.append(f"- 상태: {v['status']} ({v['status_label']})" + (" · 드라이런 (이력 DB 갱신 안 함)" if v["dry_run"] else ""))
    out.append(f"- 조사 단지: {v['complex_count']}곳" + (f" (수집 실패 {len(v['failed_rows'])}곳)" if v["failed_rows"] else ""))
    out.append(
        f"- 신규 {c['NEW']} · 가격 인하 {c['PRICE_DROP']} · 지속 중 {c['ONGOING']}"
        f" · 지난주 급매 중 내려간 매물 {len(v['gone'])}"
    )
    out.append("")

    out.append("## 이번 주 알림 (신규·가격 인하)")
    if v["alerts"]:
        out.extend(_item_line(it) for it in v["alerts"])
    elif v["status"] == "FAILED":
        out.append("- 수집 실패로 급매를 확인하지 못했습니다.")
    elif v["has_failure"]:
        out.append("- 수집에 성공한 단지에서는 신규·가격 인하 급매가 없습니다. 수집 실패 단지는 확인하지 못했습니다.")
    else:
        out.append("- 신규·가격 인하 급매 없음")
    out.append("")

    checks: list[str] = []
    for r in v["no_target_rows"]:
        checks.append(
            f"- 대상 평형 없음: {r['name']}({r['complex_no']}) — 공급 119.0㎡ 이하 평형이 없어 판정하지 않았습니다."
        )
    for r in v["molit_missing"]:
        checks.append(
            f"- 실거래 매칭 확인 필요: {r['name']}({r['complex_no']}) — "
            f"{v['complexes_file']}에 molit_apt_seq를 적어 주세요 (후보는 리포트 참고)."
        )
    if v["no_trade_areas"]:
        checks.append(f"- 실거래 부족: {v['no_trade_areas']}개 평형은 실거래 조건 없이 매물 비교로만 판정했습니다.")
    if v["unknown_total"]:
        checks.append(f"- 층 미상: 매물 {v['unknown_total']}건은 층을 알 수 없어 판정에서 뺐습니다.")
    if v["collector_warnings"]:
        checks.append(f"- 수집 경고 {len(v['collector_warnings'])}건 (리포트 참고)")
    if v["warnings"]:
        checks.append(f"- 교차검증 경고 {len(v['warnings'])}건 (리포트 참고)")
    if v["errors"]:
        checks.append(f"- 수집 오류 {len(v['errors'])}건 (리포트 참고)")
    if checks:
        out.append("## 확인 필요")
        out.extend(checks)
        out.append("")

    if v["report_path"]:
        where = "드라이런: 저장소에 올리지 않음" if v["dry_run"] else "reports 브랜치"
        out.append(f"리포트: {v['report_path']} ({where})")
    out.append(f"실행 ID: {v['run_id']}")
    return "\n".join(out) + "\n"

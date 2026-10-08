"""검증자 반례: 2단계 notify (재알림 분류 표·이력 보호·dry_run·파일 쓰기 실패·target_complex_nos·재등장 NEW, 리포트 표시).
실행: .venv/bin/python tests/verifier/verify_stage2_notify.py   (임시 SQLite 파일, 외부 네트워크 없음)
"""
from __future__ import annotations
import hashlib, sys, tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from sqlalchemy import select
from app.db.migrate import upgrade_db
from app.db.models import AlertHistoryRow
from app.db.session import make_engine, make_session_factory
from app.domain.models import Listing, Verdict, RunResult, Complex, AreaType
from app.notify.history import classify_alerts, commit_history, prior_alerts
from app.notify import report as R
from app.notify.summary import render_summary

fails: list[str] = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + (f"  -- {d}" if d and not c else ""))
    if not c: fails.append(n)

TMP = Path(tempfile.mkdtemp(prefix="verify_notify_"))
_n = [0]
def fresh():
    _n[0] += 1
    p = TMP / f"h{_n[0]}.sqlite3"; upgrade_db(p)
    return p, make_session_factory(make_engine(p))
def L(cno, dong, price, ak=84.97, floor="10/20", g="NORMAL"):
    k = f"{cno}|{ak}|{dong}|{floor}|남"
    return Listing(f"a-{cno}-{dong}-{price}", cno, ak, dong, floor, g, "남", price, None, 1, [], k, f"https://example.invalid/{k}")
def B(cno, dong, price, **kw):  # 급매
    return Verdict(L(cno, dong, price, **kw), True, ["TRADE"], 100000, None, 5.0, False, None)
def N(cno, dong, price, **kw):  # 급매 아님
    return Verdict(L(cno, dong, price, **kw), False, [], 100000, None, None, False, None)
def rows(path):  # 별도 엔진으로 디스크 상태를 본다 (세션 캐시와 무관)
    S = make_session_factory(make_engine(path))
    with S() as s:
        return {r.dedup_key: (r.active, r.last_alerted_price, r.first_alerted_at, r.last_alerted_at, r.last_seen_run_id, r.deactivated_run_id)
                for r in s.scalars(select(AlertHistoryRow))}
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def step(S, verdicts, run_id, now, failed=frozenset(), targets=frozenset({"C1", "C2"}), dry=False):
    with S() as s:
        vs, gone = classify_alerts(s, verdicts, set(failed), run_id, dry, set(targets))
        prior = prior_alerts(s, [v.listing.dedup_key for v in vs if v.alert_kind == "PRICE_DROP"])
        st = commit_history(s, vs, set(failed), run_id, dry_run=dry, target_complex_nos=set(targets), now=now)
    return vs, gone, prior, st
W = [datetime(2026, 10, 6, 1, tzinfo=UTC) + timedelta(weeks=i) for i in range(8)]
k = lambda cno, dong, ak=84.97: f"{cno}|{ak}|{dong}|10/20|남"

# ---- 분류 표 각 행: 다주차 흐름
p, S = fresh()
vs, gone, _, st = step(S, [B("C1", "1", 95000)], "r1", W[0])
check("행1 이력 없음 → NEW, 생성·active·last_alerted_price", vs[0].alert_kind == "NEW" and rows(p)[k("C1","1")][:2] == (True, 95000))
vs, *_ = step(S, [B("C1", "1", 97000)], "r2", W[1])
r = rows(p)[k("C1","1")]
check("행3 더 높은 가격 → ONGOING, 알림가·알림시각 유지, last_seen 갱신", vs[0].alert_kind == "ONGOING" and r[1] == 95000 and r[3] == W[0] and r[4] == "r2", r)
vs, *_ = step(S, [B("C1", "1", 96000)], "r3", W[2])
check("행3 올랐다 내렸어도 이전 알림가(95000) 이상이면 ONGOING", vs[0].alert_kind == "ONGOING" and rows(p)[k("C1","1")][1] == 95000)
vs, *_ = step(S, [B("C1", "1", 95000)], "r3b", W[2])
check("행3 같은 가격 → ONGOING", vs[0].alert_kind == "ONGOING")
vs, _, prior, _ = step(S, [B("C1", "1", 94999)], "r4", W[3])
r = rows(p)[k("C1","1")]
check("행2 1만원 인하 → PRICE_DROP, prior=95000, 알림가 갱신, first 유지", vs[0].alert_kind == "PRICE_DROP" and prior[k("C1","1")]["last_alerted_price"] == 95000 and r[1] == 94999 and r[2] == W[0] and r[3] == W[3], (prior, r))
vs, gone, _, st = step(S, [], "r5", W[4])
check("행4 매물 사라짐 → gone 1건(GONE), active=False", [(g["dedup_key"], g["reason"]) for g in gone] == [(k("C1","1"), "GONE")] and rows(p)[k("C1","1")][0] is False and rows(p)[k("C1","1")][5] == "r5")
_, gone, _, _ = step(S, [], "r6", W[5])
check("행4 '내려간 매물'은 1회만", gone == [])
vs, gone, _, _ = step(S, [B("C1", "1", 94999)], "r7", W[6])
r = rows(p)[k("C1","1")]
check("내려갔던 급매가 같은 가격으로 재등장 → NEW (ONGOING 아님), 재활성", vs[0].alert_kind == "NEW" and r[0] is True and r[2] == W[6] and r[5] is None, r)
vs, gone, _, _ = step(S, [N("C1", "1", 99000)], "r8", W[7])
check("행4 급매 조건 벗어남 → NOT_BARGAIN gone, alert_kind None", vs[0].alert_kind is None and [g["reason"] for g in gone] == ["NOT_BARGAIN"] and rows(p)[k("C1","1")][0] is False)
vs, *_ = step(S, [B("C1", "1", 99000)], "r9", W[7] + timedelta(weeks=1))
check("급매 아님→다시 급매(더 높은 가격이라도) → NEW", vs[0].alert_kind == "NEW")

# ---- 수집 실패 단지 보호
p, S = fresh()
step(S, [B("C1", "1", 95000), B("C2", "1", 95000), B("C2", "2", 95000)], "r1", W[0])
before = rows(p); h0 = digest(p)
vs, gone, _, st = step(S, [B("C2", "2", 90000), N("C2", "1", 99999), B("C2", "9", 80000), B("C1", "1", 95000)], "r2", W[1], failed={"C2"})
after = rows(p)
check("실패 단지: active 이력이 사라져도/급매 아님이어도 gone 아님", all(g["complex_no"] != "C2" for g in gone))
check("실패 단지: 기존 행 변화 없음 (가격 인하·비활성 모두 미반영)", all(after[x] == before[x] for x in before if x.startswith("C2")), {x: (before[x], after.get(x)) for x in before if x.startswith("C2")})
check("실패 단지: 새 행 안 만듦", k("C2","9") not in after)
check("실패 단지 Verdict 분류는 표시용으로 계산(PRICE_DROP/NEW)", [v.alert_kind for v in vs[:3]] == ["PRICE_DROP", None, "NEW"], [v.alert_kind for v in vs])
check("정상 단지 C1은 정상 갱신(ONGOING last_seen=r2)", after[k("C1","1")][4] == "r2")
# 실패 + 대상 제외
p, S = fresh(); step(S, [B("C2", "1", 95000)], "r1", W[0]); before = rows(p)
_, gone, _, st = step(S, [], "r2", W[1], failed={"C2"}, targets={"C1"})
check("실패 단지가 대상에서도 빠졌을 때: 보호 우선 (변화 없음, gone 없음)", rows(p) == before and gone == [] and st["RETIRED"] == 0, st)

# ---- dry_run: DB 파일 바이트 불변
p, S = fresh()
step(S, [B("C1", "1", 95000), B("C1", "2", 95000), B("C3", "1", 95000)], "r1", W[0], targets={"C1", "C3"})
h0 = digest(p); before = rows(p)
with S() as s:
    vs, gone = classify_alerts(s, [B("C1", "1", 90000), B("C1", "3", 90000)], set(), "r2", True, {"C1"})
    st = commit_history(s, vs, set(), "r2", dry_run=True, target_complex_nos={"C1"}, now=W[1])
    pending = bool(s.new or s.dirty or s.deleted)
    s.close()
check("dry_run: 분류는 기존 이력 사용 (PRICE_DROP, NEW) + gone(C1|2)", [v.alert_kind for v in vs] == ["PRICE_DROP", "NEW"] and [g["dedup_key"] for g in gone] == [k("C1","2")])
check("dry_run: 세션에 보류 변경 없음", not pending)
check("dry_run: DB 파일 바이트·행 불변 (RETIRED 포함)", digest(p) == h0 and rows(p) == before and st["RETIRED"] == 1 and st["dry_run"] == 1, st)

# ---- 파일 쓰기 실패 → 커밋 안 함 (실제 OSError: report_dir 자리에 일반 파일)
p, S = fresh(); step(S, [B("C1", "1", 95000)], "r1", W[0], targets={"C1"}); h0 = digest(p)
cx = [Complex("C1", "단지1", "11710", "주소", 20, "seq")]
ats = {"C1": [AreaType("C1", 84.97, 84.97, 112.0, 34, "112")]}
summ = {("C1", 84.97): dict(t_normal=100000, trade_sample_count=3, trade_sample_short=False, l_normal_min=94000, l_low_min=None, listing_count=2, unknown_count=0)}
blocker = TMP / "not_a_dir"; blocker.write_text("x")
committed = False
with S() as s:
    vs, gone = classify_alerts(s, [B("C1", "1", 94000), B("C1", "5", 94000)], set(), "r2", False, {"C1"})
    prior = prior_alerts(s, [v.listing.dedup_key for v in vs if v.alert_kind == "PRICE_DROP"])
    run = RunResult("r2", datetime(2026, 10, 13, 10, tzinfo=UTC), "OK", vs, [], [])
    ctx = dict(complexes=cx, area_types=ats, area_summaries=summ, gone=gone, failures=[], prior_alerts=prior)
    try:
        R.write_outputs(run, ctx, report_dir=blocker / "reports", out_dir=TMP / "out_fail", dry_run=False)
        commit_history(s, vs, set(), "r2", dry_run=False, target_complex_nos={"C1"}, now=W[1]); committed = True
    except OSError:
        pass
check("파일 쓰기 실패(OSError) → commit 안 됨, DB 바이트 불변", not committed and digest(p) == h0)
check("파일 쓰기 실패 → summary.md도 안 써짐", not (TMP / "out_fail" / "summary.md").exists())
# 다음 실행에서 다시 NEW/PRICE_DROP으로 잡힘
with S() as s:
    vs2, _ = classify_alerts(s, [B("C1", "1", 94000), B("C1", "5", 94000)], set(), "r3", False, {"C1"})
check("쓰기 실패 후 다음 실행: 같은 급매가 다시 PRICE_DROP/NEW", [v.alert_kind for v in vs2] == ["PRICE_DROP", "NEW"])

# ---- target_complex_nos
p, S = fresh(); step(S, [B("C1", "1", 95000), B("C3", "1", 95000)], "r1", W[0], targets={"C1", "C3"})
_, gone, _, st = step(S, [B("C1", "1", 95000)], "r2", W[1], targets={"C1"})
r = rows(p)
check("대상 제외 단지: gone 표시 안 함 + 조용히 비활성화", gone == [] and r[k("C3","1")][0] is False and r[k("C3","1")][5] == "r2" and st["RETIRED"] == 1, (gone, st))
vs, *_ = step(S, [B("C3", "1", 95000)], "r3", W[2], targets={"C1", "C3"})
check("대상에 다시 넣은 단지의 같은 급매 → NEW", vs[0].alert_kind == "NEW")
try:
    with S() as s: classify_alerts(s, [B("C9", "1", 1)], set(), "r", True, {"C1"}); ok = False
except ValueError: ok = True
check("대상 밖 단지 Verdict → ValueError", ok)

# ---- 리포트 표시: 평형 단위 실패, FAILED, 비밀값·이메일
run = RunResult("r", datetime(2026, 10, 13, 10, tzinfo=UTC), "PARTIAL", [N("C1", "1", 99000)], ["x"], [])
base_ctx = dict(complexes=cx, area_types=ats, area_summaries={}, gone=[], prior_alerts={})
ctx = dict(base_ctx, failures=[{"complex_no": "C1", "stage": "network", "area_key": 84.97}])
html, sm = R.render_report(run, ctx), render_summary(run, ctx)
check("평형 단위 실패: 리포트 현황표·요약에 '수집 실패' + 평형 표시", "수집 실패 — 네트워크 오류" in html and "단지1 34평 112 (전용 84.97㎡): 수집 실패" in sm, sm)
ctx2 = dict(base_ctx, failures=[{"complex_no": "C1", "stage": "network", "area_key": 84.9}])  # AreaType에 없는 area_key
sm2, html2 = render_summary(run, ctx2), R.render_report(run, ctx2)
check("AreaType에 없는 area_key 실패도 단지·평형이 '수집 실패'로 이름 표시", "단지1" in sm2.split("## 실행 결과")[0] and "84.9" in sm2.split("## 실행 결과")[0], sm2.split("## 실행 결과")[0])
runA = RunResult("r", datetime(2026, 10, 13, 10, tzinfo=UTC), "PARTIAL", [], [], [])
ctxA = dict(complexes=[], area_types={}, area_summaries={}, gone=[], failures=[{"complex_no": "C5", "name": "단지5", "stage": "network", "area_key": 59.9}])
smA, htmlA = render_summary(runA, ctxA), R.render_report(runA, ctxA)
check("Complex·AreaType 없는 단지의 평형 실패: '조사 대상 평형이 없습니다'로 보이지 않고 단지명이 수집 실패로 표시",
      "조사 대상 평형(공급 119.0㎡ 이하)이 없습니다" not in htmlA and "단지5" in smA.split("## 실행 결과")[0], smA.split("## 실행 결과")[0])
runF = RunResult("r", datetime(2026, 10, 13, 10, tzinfo=UTC), "FAILED", [], [], [])
smF = render_summary(runF, dict(complexes=[], area_types={}, area_summaries={}, gone=[], failures=[]))
check("FAILED + failures 비어 있음: 첫 줄 [수집 실패], '급매 없음' 문구 없음", smF.startswith("[수집 실패]") and "급매 없음\"이 아닙니다" in smF and "신규·가격 인하 급매 없음" not in smF, smF)
secret = "Ab+C/dE%3D" + "z9"
runS = replace(run, errors=[f"https://apis.data.go.kr/x?serviceKey={secret}&LAWD_CD=11710", f"SERVICEKEY={secret}", f"token={secret} password={secret}"],
               cross_check_warnings=[f"api-key={secret}"])
out = R.render_report(runS, dict(base_ctx, failures=[{"complex_no": "C1", "stage": "molit_api", "detail": f"?serviceKey={secret}"}], collector_warnings=[f"api_key={secret}"])) + render_summary(runS, dict(base_ctx, failures=[]))
check("비밀값 파라미터 마스킹(serviceKey/SERVICEKEY/token/password/api-key/api_key)", secret not in out, [l for l in out.splitlines() if secret in l][:3])
samples = (Path(__file__).resolve().parents[2] / "docs" / "samples")
blob = "".join(f.read_text(encoding="utf-8") for f in samples.iterdir())
check("샘플에 이메일 주소·serviceKey 값 없음", "@" not in blob.replace("@import", "") and "serviceKey=" not in blob)

print(f"\n{len(fails)} FAIL" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)

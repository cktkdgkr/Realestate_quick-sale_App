"""검증자 반례: 2단계 급매 판정 (rules.py). 실행: .venv/bin/python tests/verifier/verify_stage2_rules.py
pytest 수집 대상이 아니다 (test_ 접두어 없음). 실패 항목을 FAIL로 출력하고 종료 코드 1."""
from __future__ import annotations
import random, sys
from datetime import date
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app.domain.models import Listing, Trade
from app.domain.rules import judge, area_summary, trade_base, _minus_months
from app.domain import normalize

AS_OF = date(2026, 10, 13); CX = "C1"; AK = 84.97
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def L(g, p, k, ak=AK, cx=CX):
    return Listing(f"A{k}", cx, ak, "101", {"NORMAL":"10/25","LOW":"1/25","UNKNOWN":"-"}[g], g, "남", p,
                   None, 1, [], k, "u")
def T(d, p, g="NORMAL", c=False, ak=AK, cx=CX):
    return Trade(cx, ak, ak, 10 if g=="NORMAL" else 1, g, p, d, c, "중개거래", "MOLIT")
def V(vs, k="X"): return [v for v in vs if v.listing.dedup_key == k][0]

# 1. 95% / 90% 경계 (비 정수배 기준)
for base in (100000, 98765, 123457, 1):
    t = [T(date(2026,9,1), base)]
    # 정확히 경계가 되는 최대 정수가: floor(base*95/100)
    p = base*95//100
    v = V(judge([L("NORMAL", p, "X")], t, AS_OF))
    check(f"NORMAL 95% 경계 base={base} p={p}", v.reasons == ["TRADE"], v.reasons)
    v = V(judge([L("NORMAL", p+1, "X")], t, AS_OF))
    check(f"NORMAL 95% 경계+1 base={base}", v.reasons == [], v.reasons)
    p = base*90//100
    v = V(judge([L("LOW", p, "X")], t, AS_OF))
    check(f"LOW 90% 경계 base={base} p={p}", v.reasons == ["TRADE"], v.reasons)
    v = V(judge([L("LOW", p+1, "X")], t, AS_OF))
    check(f"LOW 90% 경계+1 base={base}", v.reasons == [], v.reasons)
# LOW가 95% 구간에 있으면 급매 아님
v = V(judge([L("LOW", 92000, "X"), L("NORMAL", 100000, "N")], [T(date(2026,9,1),100000)], AS_OF))
check("LOW 92% 는 급매 아님", v.reasons == [], v.reasons)

# 2. 24개월 경계 (여러 as_of)
for as_of, inside, outside in [
    (date(2026,10,13), date(2024,10,13), date(2024,10,12)),
    (date(2026,8,31), date(2024,8,31), date(2024,8,30)),
    (date(2028,2,29), date(2026,2,28), date(2026,2,27)),
    (date(2026,3,31), date(2024,3,31), date(2024,3,30)),
    (date(2026,1,1), date(2024,1,1), date(2023,12,31)),
]:
    check(f"24개월 당일 포함 as_of={as_of}", trade_base([T(inside, 100000)], as_of) == (100000, True))
    check(f"24개월 하루 전 제외 as_of={as_of}", trade_base([T(outside, 100000)], as_of) == (None, True))
check("as_of 다음날 거래 제외", trade_base([T(date(2026,10,14), 1)], AS_OF) == (None, True))
# _minus_months 와 relativedelta 의미 대조 (손으로 만든 기대값)
for d, exp in [(date(2026,10,13), date(2024,10,13)), (date(2028,2,29), date(2026,2,28)),
               (date(2024,2,29), date(2022,2,28)), (date(2026,12,31), date(2024,12,31))]:
    check(f"_minus_months({d})", _minus_months(d, 24) == exp, _minus_months(d, 24))

# 3. 해제 거래: 최신이고 가격이 극단이어도 표본에 안 들어가고, 최신 3건 자리를 차지하지 않음
tr = [T(date(2026,10,10), 1, c=True), T(date(2026,10,9), 1, c=True), T(date(2026,10,8), 1, c=True),
      T(date(2026,9,1), 100000), T(date(2026,8,1), 100000), T(date(2026,7,1), 100000)]
check("해제 3건이 최신이어도 T_normal=100000, short=False", trade_base(tr, AS_OF) == (100000, False), trade_base(tr, AS_OF))

# 4. LOW 실거래 혼입
tr = [T(date(2026,10,10), 50000, g="LOW"), T(date(2026,10,9), 50000, g="LOW"), T(date(2026,9,1), 100000)]
check("LOW 실거래 2건 + NORMAL 1건 → (100000, True)", trade_base(tr, AS_OF) == (100000, True), trade_base(tr, AS_OF))
tr = [T(date(2026,10,10), 50000, g="LOW")]
check("LOW 실거래만 → (None, True)", trade_base(tr, AS_OF) == (None, True))
s = area_summary([], tr, AS_OF)
check("area_summary LOW 실거래만 → t_normal None, count 0", s["t_normal"] is None and s["trade_sample_count"] == 0, s)

# 5. UNKNOWN 매물이 기준가에 섞이는지
vs = judge([L("NORMAL", 95000, "X"), L("UNKNOWN", 1, "U"), L("NORMAL", 99000, "N")], [], AS_OF)
check("UNKNOWN 1만원이 L_normal(x)에 안 들어감", V(vs).listing_base == 99000, V(vs).listing_base)
vs = judge([L("LOW", 80000, "X"), L("UNKNOWN", 1, "U")], [], AS_OF)
check("UNKNOWN이 LOW의 L_normal_all에 안 들어감", V(vs).listing_base is None and V(vs).reasons == [])
vs = judge([L("LOW", 80000, "X"), L("LOW", 1, "L2"), L("NORMAL", 100000, "N")], [], AS_OF)
check("LOW 1만원이 L_normal_all에 안 들어감", V(vs).listing_base == 100000)
s = area_summary([L("UNKNOWN", 1, "U"), L("NORMAL", 5, "N")], [], AS_OF)
check("area_summary l_normal_min에 UNKNOWN 제외, listing_count에 포함",
      s["l_normal_min"] == 5 and s["listing_count"] == 2 and s["unknown_count"] == 1, s)

# 6. C5-3 결정성 (같은 계약일 3건 + 같은 계약일 4건)
tr = [T(date(2026,9,1), p) for p in (100000, 90000, 110000, 130000)] + [T(date(2026,8,1), 1)]
ls = [L("NORMAL", 95000, "X"), L("NORMAL", 99000, "A"), L("NORMAL", 99000, "B"), L("LOW", 1, "C"), L("UNKNOWN", 2, "D")]
ref = [(v.listing.dedup_key, v.is_bargain, v.reasons, v.trade_base, v.listing_base, v.discount_pct) for v in judge(ls, tr, AS_OF)]
ok = True; rng = random.Random(7)
for _ in range(10):
    t2, l2 = tr[:], ls[:]; rng.shuffle(t2); rng.shuffle(l2)
    ok &= [(v.listing.dedup_key, v.is_bargain, v.reasons, v.trade_base, v.listing_base, v.discount_pct) for v in judge(l2, t2, AS_OF)] == ref
check("셔플 10회 결정적", ok)
check("같은 날 4건 → 가격 내림차순 상위 3 → 중앙값 110000", ref and V(judge(ls, tr, AS_OF)).trade_base == 110000)
check("NORMAL 1건·실거래 0 → 급매 아님", judge([L("NORMAL", 1, "X")], [], AS_OF)[0].is_bargain is False)
check("LOW만·NORMAL/실거래 0 → 급매 아님", not any(v.is_bargain for v in judge([L("LOW", 1, "X"), L("LOW", 9, "Y")], [], AS_OF)))

# 7. 동일 가격 NORMAL 두 건 + 더 비싼 매물: 서로를 기준으로 삼아 급매 아님
vs = judge([L("NORMAL", 90000, "X"), L("NORMAL", 90000, "Y"), L("NORMAL", 200000, "Z")], [], AS_OF)
check("동일가 최저 2건 → 둘 다 급매 아님", not V(vs, "X").is_bargain and not V(vs, "Y").is_bargain)
check("비싼 매물 Z의 listing_base=90000", V(vs, "Z").listing_base == 90000)

# 8. discount_pct
v = V(judge([L("NORMAL", 90000, "X"), L("NORMAL", 110000, "N")], [T(date(2026,9,1),100000)], AS_OF))
check("discount_pct 큰 할인율(18.2)", v.discount_pct == 18.2 and v.reasons == ["TRADE","LISTING"], (v.discount_pct, v.reasons))
v = V(judge([L("NORMAL", 95000, "X"), L("NORMAL", 200000, "N")], [T(date(2026,9,1),100000)], AS_OF))
check("discount_pct LISTING 근거 base 200000 → 52.5", v.discount_pct == 52.5 and v.reasons == ["TRADE","LISTING"], (v.discount_pct, v.reasons))

# 9. 입력 범위 (§9 확정)
def raises(f):
    try: f(); return False
    except ValueError: return True
check("complex 혼합 ValueError", raises(lambda: judge([L("NORMAL",1,"X"), L("NORMAL",1,"Y",cx="C2")], [], AS_OF)))
check("trade complex 혼합 ValueError", raises(lambda: judge([L("NORMAL",1,"X")], [T(date(2026,9,1),1,cx="C2")], AS_OF)))
check("dedup_key 중복 ValueError", raises(lambda: judge([L("NORMAL",1,"X"), L("NORMAL",2,"X")], [], AS_OF)))
check("area_key 혼합 ValueError", raises(lambda: judge([L("NORMAL",1,"X"), L("NORMAL",1,"Y",ak=59.99)], [], AS_OF)))
# §9: area_key는 normalize.area_key로 반올림한 값끼리 비교 → 반올림하면 같은 값은 같은 평형
a1, a2 = 84.97, 84.9700001
assert normalize.area_key(a1) == normalize.area_key(a2)
try:
    vs = judge([L("NORMAL", 95000, "X", ak=a1)], [T(date(2026,9,1), 100000, ak=a2)], AS_OF)
    check("반올림 후 같은 area_key(84.97 vs 84.9700001)는 같은 평형으로 판정", V(vs).reasons == ["TRADE"], V(vs).reasons)
except ValueError as e:
    check("반올림 후 같은 area_key(84.97 vs 84.9700001)는 같은 평형으로 판정", False, f"ValueError: {e}")
try:
    s = area_summary([L("NORMAL", 1, "X", ak=59.994)], [T(date(2026,9,1), 100000, ak=59.99)], AS_OF)
    check("area_summary 59.994 vs 59.99 (반올림 동일) 허용", s["t_normal"] == 100000, s)
except ValueError as e:
    check("area_summary 59.994 vs 59.99 (반올림 동일) 허용", False, f"ValueError: {e}")

# 10. alert_kind
check("alert_kind 항상 None", all(v.alert_kind is None for v in judge(ls, tr, AS_OF)))

print(f"\n{len(fails)} FAIL"); sys.exit(1 if fails else 0)

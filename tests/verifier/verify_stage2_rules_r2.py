"""검증자 반례 2회차: CLAUDE.md §9 rules ①~④ 확정 사항. 실행: .venv/bin/python tests/verifier/verify_stage2_rules_r2.py"""
from __future__ import annotations
import random, sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app.domain.models import Listing, Trade
from app.domain.rules import judge, area_summary
from app.domain import rules

AS_OF = date(2026, 10, 13); fails = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + (f"  -- {d}" if d and not c else ""))
    if not c: fails.append(n)
def L(g, p, k, art=None, ak=84.97):
    return Listing(art or f"A{k}", "C1", ak, "101", "10/25", g, "남", p, None, 1, [], k, "u")
def T(d, p, g="NORMAL"):
    return Trade("C1", 84.97, 84.97, 10, g, p, d, False, "중개거래", "MOLIT")
def ref_pct(price, base):
    return float((Decimal(base - price) * 100 / Decimal(base)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

# ④ discount_pct: 판정 경로 전체에서 Decimal ROUND_HALF_UP 참값과 일치
rng = random.Random(20261008); bad = []
for _ in range(20000):
    base = rng.randint(1, 500000); price = rng.randint(1, base)
    got = rules._discount_pct(price, [base])
    if got != ref_pct(price, base) or not isinstance(got, float): bad.append((price, base, got, ref_pct(price, base)))
check("_discount_pct == Decimal HALF_UP (무작위 2만 건)", not bad, bad[:3])
# 정확히 .x5 경계를 직접 만든 사례 (base=1000*k, price가 .05 지점)
for base in (100000, 200000, 40000, 20):
    for tenths20 in range(1, 200, 2):  # (base-price)/base*1000 = tenths20/2 → x.x5
        num = base * tenths20
        if num % 2000: continue
        price = base - num // 2000
        if rules._discount_pct(price, [base]) != ref_pct(price, base): bad.append((price, base))
check("정확히 x.x5 경계는 올림", not bad, bad[:3])
# judge 경로: 근거 기준가 중 큰 쪽(할인율 큰 쪽) + HALF_UP
tr = [T(date(2026,9,1), 100000)]
v = [x for x in judge([L("NORMAL", 94950, "X"), L("NORMAL", 200000, "N")], tr, AS_OF) if x.listing.dedup_key == "X"][0]
check("judge TRADE+LISTING → base 200000 기준 52.5", v.reasons == ["TRADE","LISTING"] and v.discount_pct == ref_pct(94950, 200000) == 52.5, (v.reasons, v.discount_pct))
v = [x for x in judge([L("LOW", 89955, "X"), L("NORMAL", 99950, "N")], [], AS_OF) if x.listing.dedup_key == "X"][0]
check("LOW LISTING discount HALF_UP", v.reasons == ["LISTING"] and v.discount_pct == ref_pct(89955, 99950), (v.reasons, v.discount_pct, ref_pct(89955, 99950)))
v = [x for x in judge([L("NORMAL", 99000, "X")], tr, AS_OF)][0]
check("급매 아님 → discount_pct None", v.discount_pct is None)

# ① UNKNOWN Verdict
vs = judge([L("UNKNOWN", 1, "U"), L("NORMAL", 100000, "N"), L("LOW", 50000, "W")], tr, AS_OF)
u = [x for x in vs if x.listing.dedup_key == "U"][0]
check("UNKNOWN: is_bargain False, reasons [], listing_base None, discount None, alert None",
      (u.is_bargain, u.reasons, u.listing_base, u.discount_pct, u.alert_kind) == (False, [], None, None, None))
check("UNKNOWN: trade_base/short는 평형 공통값", (u.trade_base, u.trade_sample_short) == (100000, True))

# ② 반환 순서 (price, dedup_key, article_no) 오름차순, 입력 순서 무관
ls = [L("NORMAL", 90000, "b"), L("LOW", 90000, "a"), L("UNKNOWN", 50000, "z"), L("NORMAL", 120000, "c"),
      L("NORMAL", 90000, "B"), L("LOW", 70000, "y")]
exp = sorted(ls, key=lambda x: (x.price, x.dedup_key, x.article_no))
ok = True
for _ in range(10):
    l2 = ls[:]; rng.shuffle(l2)
    ok &= [v.listing for v in judge(l2, tr, AS_OF)] == exp
check("반환 순서 (price, dedup_key, article_no) 셔플 10회", ok)

# ③ 공개 보조 함수: §10 함수 존재 + trade_base 허용. 다른 모듈은 §10 함수만 사용하는지
check("judge/area_summary 공개", callable(rules.judge) and callable(rules.area_summary))
import pathlib, re
root = pathlib.Path(__file__).resolve().parents[2] / "app"
users = []
for p in root.rglob("*.py"):
    if p.name == "rules.py": continue
    s = p.read_text(encoding="utf-8")
    for m in re.finditer(r"from app\.domain\.rules import ([^\n]+)|rules\.(\w+)\(", s):
        names = m.group(1) or m.group(2)
        for n in re.split(r"[,\s()]+", names):
            if n and n not in ("judge", "area_summary"): users.append((p.name, n))
check("app/ 다른 모듈은 rules의 §10 함수만 사용", not users, users)

# 회귀: 정수 비교 경계
check("95% 경계 포함", judge([L("NORMAL", 95000, "X")], tr, AS_OF)[0].reasons == ["TRADE"])
check("90% 경계 포함", judge([L("LOW", 90000, "X")], tr, AS_OF)[0].reasons == ["TRADE"])
print(f"\n{len(fails)} FAIL"); sys.exit(1 if fails else 0)

"""검증자 반례: 2단계 매물 조사 모듈 (normalize, dedup, naver_listings, errors).
실행: cd <repo> && .venv/bin/python tests/verifier/verify_stage2_listing.py
외부 네트워크 사용 안 함 (httpx.MockTransport만).
"""
import sys, json, dataclasses, logging, random
from datetime import date
from pathlib import Path
sys.path.insert(0, ".")
import httpx
from app.domain.normalize import *
from app.domain.normalize import classify_floor, area_key, to_pyeong, is_target_area, match_area, parse_price, format_price
from app.domain.dedup import dedup, make_dedup_key
from app.domain.models import Listing, AreaType
from app.collectors.errors import CollectorError
from app.collectors import naver_listings as nl

fails = []
def check(label, got, exp):
    ok = got == exp
    print(("OK  " if ok else "FAIL"), label, "->", repr(got), "" if ok else f"(expected {exp!r})")
    if not ok: fails.append(label)

def raises(label, fn, exc=ValueError):
    try:
        r = fn(); print("FAIL", label, "-> returned", repr(r)); fails.append(label)
    except exc as e:
        print("OK  ", label, "->", type(e).__name__)

print("== C2-2 classify_floor")
for raw, exp in [("저 / 15","LOW"),("고","NORMAL"),("15","NORMAL"),("B2","LOW"),("옥탑","UNKNOWN"),
                 ("3층","NORMAL"),("저층","LOW"),("고층/20","NORMAL"),("옥탑/25","UNKNOWN"),
                 ("-/25","UNKNOWN"),("0","LOW"),("지하2/10","LOW"),("반지하","LOW"),(" ","UNKNOWN"),
                 ("1","LOW"),("2","LOW"),("3","NORMAL"),(0,"LOW"),(-2,"LOW"),(2,"LOW"),(3,"NORMAL"),
                 (None,"UNKNOWN"),("저\t/15","LOW"),("중/","NORMAL"),("3/","NORMAL"),("12층/25층","NORMAL")]:
    check(f"classify_floor({raw!r})", classify_floor(raw), exp)

print("== 면적 경계")
check("is_target_area(119.0)", is_target_area(119.0), True)
check("is_target_area(119.01)", is_target_area(119.01), False)
check("is_target_area(118.99)", is_target_area(118.99), True)
check("to_pyeong(112.4)", to_pyeong(112.4), 34)
check("to_pyeong(119.0)", to_pyeong(119.0), 36)
check("area_key(84.974)", area_key(84.974), 84.97)
def at(k): return AreaType("1", k, k, 110.0, 33, str(k))
def m(x, ks):
    r = match_area(x, [at(k) for k in ks]); return r.area_key if r else None
check("match_area(84.98,[84.97,59.99])", m(84.98,[84.97,59.99]), 84.97)
check("match_area(85.6,[84.97])", m(85.6,[84.97]), None)
check("match_area(84.5,[84.97,84.03])", m(84.5,[84.97,84.03]), 84.03)
check("match_area(84.5,[84.03,84.97]) order-indep", m(84.5,[84.03,84.97]), 84.03)
check("match_area(85.47,[84.97]) exactly 0.5", m(85.47,[84.97]), 84.97)
check("match_area(85.48,[84.97]) 0.51", m(85.48,[84.97]), None)
check("match_area(84.47,[84.97]) exactly -0.5", m(84.47,[84.97]), 84.97)

print("== 가격")
for s, exp in [("12억 5,000",125000),("9억",90000),("12억5000",125000),("8,500",8500),(" 125,000 ",125000),("10억 500",100500)]:
    check(f"parse_price({s!r})", parse_price(s), exp)
for s in ["가격문의","","억","12.5억","0","-5000","12억 -5", "1억2천"]:
    raises(f"parse_price({s!r}) ValueError", lambda s=s: parse_price(s))
check("format_price(125000)", format_price(125000), "12억 5,000")
check("format_price(90000)", format_price(90000), "9억")
check("format_price(8500)", format_price(8500), "8,500")
# 관찰용 (명세 밖): 억 뒤 숫자가 10000 이상
try: print("INFO parse_price('10억 15000') ->", parse_price("10억 15000"))
except ValueError as e: print("INFO parse_price('10억 15000') -> ValueError")

print("== dedup D01~D06")
def L(no, price, dong="101동", floor="10/25", d="남향", ak=84.97, conf=date(2026,10,1), cno="3009"):
    return Listing(no, cno, ak, dong, floor, classify_floor(floor), d, price, conf, 1, [], "", "u")
o = dedup([L("a",100000),L("b",98000),L("c",98000)])
check("D01 len/price/rc", (len(o), o[0].price, o[0].realtor_count), (1, 98000, 3))
check("D02", len(dedup([L("a",1,d="남향"),L("b",1,d="동향")])), 2)
check("D03", len(dedup([L("a",1,dong="101동"),L("b",1,dong="101 동")])), 1)
o = dedup([L("a",100000,conf=date(2026,9,1)),L("b",100000,conf=date(2026,10,5))])
check("D04 latest confirmed", o[0].article_no, "b")
o = dedup([L("b",100000,conf=date(2026,10,5)),L("a",100000,conf=date(2026,9,1))])
check("D04 reversed input", o[0].article_no, "b")
class H(logging.Handler):
    def __init__(s): super().__init__(); s.recs=[]
    def emit(s, r): s.recs.append(r)
h = H(); logging.getLogger("app.domain.dedup").addHandler(h)
o = dedup([L("a",100000,dong="",floor="저/15"),L("b",100000,dong="",floor="저/15")])
check("D05 len / no log", (len(o), len(h.recs)), (1, 0))
check("D06", len(dedup([L("a",1,ak=84.97),L("b",1,ak=84.96)])), 2)
h.recs.clear()
dedup([L("a",100000,dong="",floor="중/20",d=""),L("b",104999,dong="",floor="중/20",d=""),L("c",100000,dong="",floor="중/20",d="")])
check("warn: 3건, 4.999% -> 로그 없음", len(h.recs), 0)
dedup([L("a",100000,dong="",floor="중/20",d=""),L("b",105000,dong="",floor="중/20",d=""),L("c",100000,dong="",floor="중/20",d="")])
check("warn: 3건, 5.000% -> 로그 1", len(h.recs), 1)
h.recs.clear()
dedup([L("a",100000,dong="",floor="중/20",d=""),L("b",200000,dong="",floor="중/20",d="")])
check("warn: 2건, 100% -> 로그 없음", len(h.recs), 0)
# 키 형식
check("key fmt", make_dedup_key("3009", 84.9, " 101 동 ", "b1 / 15", None), "3009|84.90|101동|B1/15|")
# 순서 무관 결정성 (대표 article_no)
items = [L("x",98000,conf=None),L("y",98000,conf=None),L("z",98000,conf=date(2026,1,1))]
res = {dedup(random.Random(i).sample(items,3))[0].article_no for i in range(20)}
check("tie with None confirmed deterministic", res, {"z"})
# 재-dedup (idempotency 관찰)
o2 = dedup(dedup([L("a",100000),L("b",98000),L("c",98000)]))
print("INFO dedup(dedup(x)) realtor_count/alt_prices ->", o2[0].realtor_count, o2[0].alt_prices)

print("== naver_listings 반례 (MockTransport)")
FIX = Path("tests/fixtures/naver")
cx_json = json.loads((FIX/"complex_20261008.json").read_text())
def client_for(handler, sleeps):
    return nl.NaverClient(http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, rng=lambda: 0.0)
# (1) 5xx 후 429 -> 즉시 blocked, 이후 요청 없음
seq = [httpx.Response(503), httpx.Response(429)]; reqs=[]; sl=[]
def h1(r): reqs.append(r); return seq.pop(0)
c = client_for(h1, sl)
try: nl.fetch_complex(c, "99901"); print("FAIL no exception"); fails.append("5xx->429")
except CollectorError as e: check("503 then 429 -> blocked, 2 reqs", (e.stage, len(reqs)), ("blocked", 2))
# (2) text/plain 이지만 본문이 HTML -> blocked
c = client_for(lambda r: httpx.Response(200, content=b"\n<!DOCTYPE html><html>", headers={"content-type":"text/plain"}), [])
try: nl.fetch_complex(c, "99901"); fails.append("html-plain")
except CollectorError as e: check("text/plain HTML body -> blocked", e.stage, "blocked")
# (3) isMoreData 가 문자열 "false" -> schema_changed (조용히 무한/중단 안 함)
def h3(r):
    if "articles" in r.url.path: return httpx.Response(200, json={"isMoreData":"false","articleList":[]})
    return httpx.Response(200, json=cx_json)
c = client_for(h3, [])
cx, types = nl.fetch_complex(c, "99901")
try: nl.fetch_listings(c, cx, types); fails.append("isMoreData str")
except CollectorError as e: check("isMoreData='false' -> schema_changed", e.stage, "schema_changed")
# (4) articleList None -> schema_changed
def h4(r):
    if "articles" in r.url.path: return httpx.Response(200, json={"isMoreData":False,"articleList":None})
    return httpx.Response(200, json=cx_json)
c = client_for(h4, [])
cx, types = nl.fetch_complex(c, "99901")
try: nl.fetch_listings(c, cx, types); fails.append("articleList None")
except CollectorError as e: check("articleList None -> schema_changed", e.stage, "schema_changed")
# (5) 평형 목록 빈 배열 -> 정상 [] (대상 평형 0)
cj = dict(cx_json); cj["complexPyeongDetailList"] = []
c = client_for(lambda r: httpx.Response(200, json=cj), [])
cx, types = nl.fetch_complex(c, "99901"); check("평형 0개 -> []", types, [])
# (6) 전부 119 초과 단지 -> 매물 요청 없이 []
cj = json.loads(json.dumps(cx_json)); 
for p in cj["complexPyeongDetailList"]: p["supplyArea"] = "150.0"
reqs6=[]
def h6(r): reqs6.append(r.url.path); return httpx.Response(200, json=cj)
c = client_for(h6, [])
cx, types = nl.fetch_complex(c, "99901"); check("모두 초과 -> types []", types, [])
check("fetch_listings no request", (nl.fetch_listings(c, cx, types), len(reqs6)), ([], 1))
# (7) MAX_PAGES 무한 루프 방지
def h7(r):
    if "articles" in r.url.path: return httpx.Response(200, json={"isMoreData":True,"articleList":[]})
    return httpx.Response(200, json=cx_json)
c = client_for(h7, [])
cx, types = nl.fetch_complex(c, "99901")
try: nl.fetch_listings(c, cx, types); fails.append("maxpages")
except CollectorError as e: check("무한 isMoreData -> schema_changed", e.stage, "schema_changed")
# (8) 매물 가격이 숫자형 int(125000) -> parse_price(str) 처리
def h8(r):
    if "articles" in r.url.path: return httpx.Response(200, json={"isMoreData":False,"articleList":[
        {"articleNo":"1","dealOrWarrantPrc":"12억","areaName":"112A","floorInfo":"3층/25","buildingName":"1 01동","direction":" 남향 "}]})
    return httpx.Response(200, json=cx_json)
c = client_for(h8, [])
cx, types = nl.fetch_complex(c, "99901")
x = nl.fetch_listings(c, cx, types)[0]
check("3층/25 -> NORMAL, dong/direction trimmed", (x.floor_group, x.dong, x.direction, x.price), ("NORMAL","101동","남향",120000))
# (9) 대기 범위: rng 경계 0/1 -> 2.0~5.0
for v, exp in [(0.0, 2.0), (1.0, 5.0)]:
    sl=[]; c = nl.NaverClient(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=cx_json))), sleep=sl.append, rng=lambda v=v: v)
    nl.fetch_complex(c,"99901"); nl.fetch_complex(c,"99901")
    check(f"pace rng={v}", sl, [exp])
# (10) 기본 rng가 random.random, 기본 http timeout 15s / follow_redirects False
c = nl.NaverClient(sleep=lambda s: None)
check("default http timeout/redirect", (c._http.timeout.read, c._http.follow_redirects), (15.0, False))
c.close()
# (11) CollectorError stage 계약
for st in ["blocked","schema_changed","network","molit_api","molit_auth","complex_mapping"]:
    CollectorError(st)
raises("CollectorError('timeout') ValueError", lambda: CollectorError("timeout"))
e = CollectorError("network", complex_no=None); check("str no complex", str(e), "network")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)

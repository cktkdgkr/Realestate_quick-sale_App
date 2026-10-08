---
name: bargain-rules
description: 급매 판정 규칙의 정식 명세와 기준 테스트 케이스표. 판정 로직을 구현하거나, 테스트하거나, 리포트에 판정 근거를 표시하거나, 판정 결과를 검증할 때 읽는다.
---

# 급매 판정 규칙

CLAUDE.md §4를 구현 가능한 수준으로 풀어 쓴 것이다. 둘이 다르면 CLAUDE.md가 우선이며, 그 경우 Orchestrator에게 보고한다.

## 1. 입력 범위
- 판정은 항상 **한 단지 × 한 area_key** 단위로 한다.
- 입력 매물은 이미 중복 묶기가 끝난 **대표 매물**이다.
- 기준일 `as_of`는 실행일(KST 날짜)이다.

## 2. 실거래 기준가 `T_normal`
```
대상 = trades 중
       floor_group == "NORMAL"
       and not cancelled
       and contract_date >= as_of - 24개월 (같은 날짜 포함, dateutil.relativedelta(months=24))
       and contract_date <= as_of
정렬 = contract_date 내림차순 (같은 날이면 가격 내림차순으로 고정해 결과를 결정적으로 만든다)
표본 = 상위 3건
T_normal = 
   3건: 가운데 값 (가격 기준 정렬 후 2번째)
   2건: (a + b) // 2
   1건: 그 값
   0건: None
trade_sample_short = (표본 건수 < 3)
```
- `LOW` 실거래는 기준가에 쓰지 않는다.
- 직거래도 포함한다 (제외하지 않음). 리포트에는 거래 유형을 표시한다.

## 3. 매물 기준가
- `L_normal(x)` = NORMAL 대표 매물 중 **x 자신을 제외한** 최저가. 없으면 None.
- `L_normal_all` = NORMAL 대표 매물 전체의 최저가 (LOW 매물 판정용). 없으면 None.
- `UNKNOWN` 매물은 어느 기준가에도 포함하지 않는다.
- `LOW` 매물은 기준가에 포함하지 않는다 (LOW끼리 비교하지 않는다).

## 4. 판정식 (정수 연산, 경계 포함)
```
NORMAL x:
  TRADE   ← T_normal is not None and x.price * 100 <= T_normal * 95
  LISTING ← L_normal(x) is not None and x.price * 100 <= L_normal(x) * 95
LOW x:
  TRADE   ← T_normal is not None and x.price * 100 <= T_normal * 90
  LISTING ← L_normal_all is not None and x.price * 100 <= L_normal_all * 90
UNKNOWN x:
  판정 안 함. Verdict(is_bargain=False, reasons=[])
is_bargain = len(reasons) > 0
reasons 순서 = ["TRADE", "LISTING"] 중 해당하는 것, 이 순서로
```

## 5. Verdict 부가 필드
- `trade_base` = T_normal, `listing_base` = 판정에 쓴 매물 기준가 (NORMAL은 L_normal(x), LOW는 L_normal_all)
- `discount_pct` = 급매 근거가 된 기준가 중 **더 큰 할인율**, `round((1 - price / base) * 100, 1)`. 표시용이므로 여기서만 float 허용. 급매가 아니면 None.
- `alert_kind` = None (재알림 분류는 notifier 담당)

## 6. 기준 테스트 케이스

공통: `as_of = 2026-10-13`, 단지 C1, area_key 84.97. 가격 단위 만원.
"기본 실거래" = NORMAL 거래 [2026-09-20: 100000, 2026-08-11: 98000, 2026-07-05: 102000, 2025-12-01: 90000] → T_normal = 100000, short=False.

| ID | 실거래 | 판정 대상 x (그룹: 가격) | 다른 매물 | 기대 결과 | 검증 포인트 |
|---|---|---|---|---|---|
| R01 | 기본 | NORMAL: 95000 | NORMAL 100000, 99000 | 급매, [TRADE] | 95% 경계 포함 |
| R02 | 기본 | NORMAL: 95001 | NORMAL 100000, 99000 | 급매 아님 | 경계 바로 위 |
| R03 | 없음 | NORMAL: 94000 | NORMAL 99000, 100000 | 급매, [LISTING], listing_base=99000 | 매물 조건만 |
| R04 | 없음 | NORMAL: 90000 | 없음 | 급매 아님, listing_base=None | 자기 자신과 비교 금지 |
| R05 | 없음 | NORMAL: 90000 | NORMAL 90000 (다른 dedup 키) | 급매 아님 | 동일가는 급매 아님 |
| R06 | 기본 | NORMAL: 90000 | NORMAL 100000 | 급매, [TRADE, LISTING], discount_pct=10.0 | 두 조건 동시, 순서 |
| R07 | 기본 | LOW(1층): 90000 | NORMAL 99000 | 급매, [TRADE] | 90% 경계 포함 (매물 조건은 9,000,000 > 8,910,000 으로 불충족) |
| R08 | 기본 | LOW(2층): 90001 | NORMAL 100000 | 급매 아님 | LOW는 95%가 아니라 90% |
| R09 | 없음 | LOW(1층): 91000 | LOW 110000, NORMAL 100000 | 급매 아님 | LOW끼리 비교 안 함 |
| R10 | 없음 | LOW(`저`): 89000 | NORMAL 100000 | 급매, [LISTING] | 텍스트 '저' = LOW |
| R11 | 기본 | NORMAL(3층): 95000 | 없음 | 급매, [TRADE] | 3층은 NORMAL |
| R12 | 없음 | NORMAL: 95000 | UNKNOWN 80000 | 급매 아님 | UNKNOWN은 기준가 제외 |
| R13 | 기본 | UNKNOWN: 50000 | NORMAL 100000 | is_bargain=False, reasons=[] | UNKNOWN 판정 제외 |
| R14 | LOW 거래 2026-10-01: 70000 + 기본 | NORMAL: 95000 | 없음 | 급매, T_normal=100000 | LOW 실거래 기준가 제외 |
| R15 | NORMAL 2026-10-02: 80000 **해제** + 기본 | NORMAL: 95000 | 없음 | 급매, T_normal=100000 | 해제 거래 제외 |
| R16 | NORMAL 2024-10-12: 100000 한 건뿐 | NORMAL: 90000 | 없음 | 급매 아님, T_normal=None | 24개월 밖 (경계 하루 전) |
| R17 | NORMAL 2024-10-13: 100000 한 건뿐 | NORMAL: 95000 | 없음 | 급매, T_normal=100000, short=True | 24개월 경계 당일 포함 |
| R18 | NORMAL [2026-09-01: 100000, 2026-08-01: 97001] | NORMAL: 93575 | 없음 | 급매, T_normal=98500, short=True | 2건 = 평균 내림 (93575×100 = 9,357,500 ≤ 98500×95 = 9,357,500) |
| R19 | NORMAL [2026-09: 120000, 2026-08: 100000, 2026-07: 101000, 2026-06: 80000] | NORMAL: 95950 | 없음 | 급매, T_normal=101000 | 최신 3건만 사용, 중앙값 |
| R20 | 기본 | LOW(`B1`): 90000 | NORMAL 100000 | 급매, [TRADE, LISTING] | 지하 = LOW |
| R21 | — | area_key가 섞인 입력 | — | `ValueError` | 입력 범위 검사 |

R19 검산: 95950 × 100 = 9,595,000 ≤ 101000 × 95 = 9,595,000 → 급매 (경계).

## 7. 리포트 표시 문구 (notifier용)
- TRADE: `일반층 실거래 중앙값 {T_normal:,}만원 대비 {pct}% 낮음 (최근 {n}건{, 표본 부족})`
- LISTING (NORMAL): `일반층 다른 매물 최저가 {base:,}만원 대비 {pct}% 낮음`
- LISTING (LOW): `일반층 매물 최저가 {base:,}만원 대비 {pct}% 낮음 (저층 기준 90%)`

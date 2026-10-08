# handoff: bargain-judge (급매 판정)

담당: `app/domain/rules.py`, `tests/unit/test_rules.py`
실행: `.venv/bin/pytest tests/unit/test_rules.py -q` (57 passed, skip 없음), `.venv/bin/python tests/verifier/verify_stage2_rules.py`

## 1. 공개 함수 (CLAUDE.md §10)

| 함수 | 반환 | 비고 |
|---|---|---|
| `judge(listings, trades, as_of)` | `list[Verdict]` | 한 단지 × 한 area_key. `alert_kind`는 항상 `None` |
| `area_summary(listings, trades, as_of)` | `dict` | 키: `t_normal`, `trade_sample_count`, `trade_sample_short`, `l_normal_min`, `l_low_min`, `listing_count`, `unknown_count` |
| `trade_base(trades, as_of)` | `(int \| None, bool)` | agent 정의서에 있는 보조 공개 함수. (T_normal, short) |

- 순수 함수. import는 `calendar`, `datetime`, `typing`, `app.domain.models`, `app.domain.normalize`(area_key)뿐이다. `dateutil`은 설치돼 있지 않아서 `_minus_months`로 `relativedelta(months=24)`와 같은 동작(말일 보정)을 직접 구현했다.
- `judge` 반환 순서는 `(price, dedup_key, article_no)` 오름차순이다 (§9 확정, 테스트로 고정). 리포트는 이 순서에 의존하지 말고 스스로 정렬한다.

## 2. 규칙과 코드 위치 대응표

| 규칙 (출처) | 코드 |
|---|---|
| 입력 범위: 한 단지·한 area_key, 섞이면 ValueError (스킬 §1, R21). area_key는 `normalize.area_key`로 반올림한 값끼리 비교 (§9) | `_validate_scope` (매물과 실거래 모두 검사) |
| 24개월 창, 당일 포함, as_of 이후 제외 (스킬 §2, R16·R17) | `_window_start`, `_minus_months`, `_trade_sample` |
| NORMAL·미해제만 사용 (R14·R15) | `_trade_sample` 필터 |
| 계약일 내림차순, 같은 날이면 가격 내림차순, 최대 3건 | `_trade_sample` 정렬 |
| 중앙값: 3건은 가운데, 2건은 `(a+b)//2`, 1건은 그 값, 0건은 None (R18·R19) | `_median_int` |
| `trade_sample_short = 표본 < 3` | `trade_base`, `area_summary` |
| `L_normal(x)`: 자신을 뺀 NORMAL 최저가 (R03~R05) | `judge`의 NORMAL 분기 |
| `L_normal_all`: LOW 판정용 (R09·R10·R20) | `judge`의 `l_normal_all` |
| UNKNOWN은 판정과 기준가 모두에서 제외 (R12·R13) | `judge`의 UNKNOWN 분기, 기준가 계산이 NORMAL만 사용 |
| 정수 비교 `price*100 <= base*ratio`, 95/90 (§4.3) | `_is_below`, `NORMAL_RATIO`, `LOW_RATIO` |
| reasons 순서 TRADE → LISTING (스킬 §4) | `judge`에서 TRADE를 먼저 검사 |
| discount_pct: 근거 기준가 중 할인율이 큰 쪽, `(base-price)/base*100`을 정수 연산으로 소수 첫째 자리 ROUND_HALF_UP, 급매가 아니면 None (§9 rules ④, 스킬 §5) | `_discount_pct`. 기준가는 정수 `max`로 고르고, 0.1% 단위 정수를 반올림한 뒤 마지막에만 `/10`으로 float를 만든다 |

## 3. 테스트 구성

- R01~R20: `test_reference_cases`를 parametrize했고 테스트 ID가 케이스 ID다. R21은 `test_R21_mixed_area_key_raises`. 기대값은 스킬 §6 그대로다.
- C5-3: 입력을 섞어 10회 실행(같은 계약일 3건 포함), NORMAL 1건에 실거래 0건, LOW만 있는 경우.
- C5-4: reasons 순서, discount_pct가 더 큰 할인율(더 큰 기준가)을 쓰는지, 근거가 아닌 기준가는 쓰지 않는지.
- C5-2: rules.py의 import와 금지 호출을 정적으로 검사한다.
- 추가 케이스와 이유
  - E01 as_of 이후 계약일 제외: 스킬 §2의 `contract_date <= as_of`
  - E02 as_of 당일 포함: 위 조건의 경계
  - E03 윤일 as_of(2028-02-29): relativedelta 말일 보정을 직접 구현했으므로 검증
  - E04 실거래 0건이면 `(None, True)`
  - E05 LOW 매물의 listing_base는 NORMAL 전체 최저가. 90% 경계 포함 여부와 경계 바로 위
  - E06 자기 자신만 빼는지 (최저가 매물은 두 번째 최저가와 비교)
  - E07·E08 단지가 섞이거나 실거래의 area_key가 다르면 ValueError
  - E09 dedup_key가 중복되면 ValueError (대표 매물 계약 위반)
  - E10 빈 입력, E11 UNKNOWN Verdict 형태, E12 alert_kind는 항상 None
  - area_summary의 키와 값, 섞인 입력에서 ValueError
- §9 확정 사항 (verifier 반려 1회차 대응, `test_S9_*`)
  - 반올림하면 같아지는 area_key(84.97 / 84.9700001, 59.994 / 59.99)는 같은 평형으로 본다
  - 반올림해도 다른 area_key(84.97 / 84.98)는 judge와 area_summary 모두 ValueError
  - UNKNOWN Verdict 필드, judge 반환 순서(가격, dedup_key, article_no)
  - discount_pct ROUND_HALF_UP 경계: 5.05 → 5.1 (float `round`는 5.0이 된다), 5.049 → 5.0, 5.051 → 5.1

## 4. 확정된 결정 (CLAUDE.md §9, 2026-10-08)

이전 가정 1~7은 모두 §9에서 확정되었거나 수정되었다.
1. 실거래 0건이면 `T_normal=None`, `trade_sample_short=True`.
2. UNKNOWN Verdict: is_bargain=False, reasons=[], listing_base·discount_pct=None. trade_base·trade_sample_short에는 평형 공통값을 참고로 넣는다.
3. 단지 혼합, area_key 혼합, dedup_key 중복이면 ValueError.
4. **(수정)** area_key는 `normalize.area_key`로 반올림한 값끼리 비교한다. 이전 가정("float `==` 비교, normalize는 import하지 않음")은 폐기했다.
5. listing_count는 UNKNOWN을 포함하고, unknown_count는 따로 센다.
6. judge 반환 순서는 `(price, dedup_key, article_no)` 오름차순이다.
7. `trade_base` 공개 보조 함수는 허용된다. 다른 모듈은 §10 함수만 쓴다.
8. discount_pct는 정수 연산으로 ROUND_HALF_UP한다. 표시 전용이며 판정에는 쓰지 않는다.

## 5. 한계
- 판정 근거 문구(스킬 §7)는 notifier 몫이다. rules.py는 문구를 만들지 않는다. 문구에 필요한 값(T_normal, 표본 건수, listing_base, discount_pct)은 Verdict와 `area_summary`로 넘긴다.
- normalize.py에서는 `area_key`만 import한다 (범위 검사용). floor_group은 이미 채워진 입력을 받으므로 `classify_floor`는 쓰지 않는다.
- 판정 자체(같은 평형 안의 비교)는 반올림 전 원래 area_key 값을 바꾸지 않는다. 반환 Verdict의 listing은 입력 객체 그대로다.

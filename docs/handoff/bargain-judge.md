# handoff: bargain-judge (급매 판정)

담당: `app/domain/rules.py`, `tests/unit/test_rules.py`
실행: `.venv/bin/pytest tests/unit/test_rules.py -q` (45 passed, skip 없음)

## 1. 공개 함수 (CLAUDE.md §10)

| 함수 | 반환 | 비고 |
|---|---|---|
| `judge(listings, trades, as_of)` | `list[Verdict]` | 한 단지 × 한 area_key. `alert_kind`는 항상 `None` |
| `area_summary(listings, trades, as_of)` | `dict` | 키: `t_normal`, `trade_sample_count`, `trade_sample_short`, `l_normal_min`, `l_low_min`, `listing_count`, `unknown_count` |
| `trade_base(trades, as_of)` | `(int \| None, bool)` | agent 정의서에 있는 보조 공개 함수. (T_normal, short) |

- 순수 함수. import는 `calendar`, `datetime`, `typing`, `app.domain.models`뿐이다. `dateutil`은 설치돼 있지 않아서 `_minus_months`로 `relativedelta(months=24)`와 같은 동작(말일 보정)을 직접 구현했다.
- `judge` 반환 순서는 `(price, dedup_key, article_no)` 오름차순이다. 입력 순서와 상관없이 결정적이다.

## 2. 규칙과 코드 위치 대응표

| 규칙 (출처) | 코드 |
|---|---|
| 입력 범위: 한 단지·한 area_key, 섞이면 ValueError (스킬 §1, R21) | `_validate_scope` (매물과 실거래 모두 검사) |
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
| discount_pct: 근거 기준가 중 할인율이 큰 쪽, `round((1-p/base)*100, 1)`, 급매가 아니면 None (스킬 §5) | `_discount_pct`. 기준가는 정수 `max`로 고르고, float는 표시값 계산에만 쓴다 |

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

## 4. 가정 (명세 모호, Orchestrator 확인 필요)

1. **실거래 0건일 때 `trade_sample_short`**: 스킬 §2 식(`표본 < 3`)을 따라 `True`로 둔다. CLAUDE.md §4.1은 "1~2건이면 True"라고만 적었다. 리포트는 `trade_base is None`으로 "실거래 부족"을 판단하면 된다.
2. **UNKNOWN Verdict의 기준값**: `trade_base`와 `trade_sample_short`에는 평형 공통값을 넣고, `listing_base`와 `discount_pct`는 None으로 둔다. 판정은 하지 않는다.
3. **LOW·UNKNOWN 매물만 있을 때**: NORMAL 매물이 아니므로 L_normal 계산에 들어가지 않는다.
4. **입력 검사 범위를 넓힘**: area_key 외에 complex_no 혼합, 실거래 area_key 불일치, dedup_key 중복도 ValueError로 처리한다. 잘못된 입력을 조용히 판정하지 않기 위해서다. pipeline은 dedup 뒤에 평형별로 나눠서 호출해야 한다.
5. **area_key 동일성**: float `==`로 비교한다. 가격 비교가 아니고 normalize.area_key가 `round(.,2)`한 값이라 문제가 없다고 봤다. 상위에서 반올림을 하지 않은 값이 들어오면 ValueError가 난다.
6. **`listing_count`**: UNKNOWN을 포함한 대표 매물 전체 건수다.
7. **반환 순서**: 가격 오름차순이다. notifier는 필요하면 다시 정렬하면 된다.

## 5. 한계
- 판정 근거 문구(스킬 §7)는 notifier 몫이다. rules.py는 문구를 만들지 않는다. 문구에 필요한 값(T_normal, 표본 건수, listing_base, discount_pct)은 Verdict와 `area_summary`로 넘긴다.
- normalize.py는 import하지 않는다 (floor_group이 이미 채워진 입력을 받기 때문).

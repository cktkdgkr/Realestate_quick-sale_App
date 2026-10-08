---
name: floor-area-normalizer
description: 층 표기(숫자, 저/중/고, 지하)를 LOW/NORMAL/UNKNOWN 그룹으로 분류하고, 면적을 area_key·평으로 환산하며, 한국식 가격 문자열을 만원 정수로 바꾸는 규칙과 테스트 케이스. 매물·실거래 데이터를 정규화하거나 그 결과를 검증할 때 읽는다.
---

# 층·면적·가격 정규화

구현 위치: `app/domain/normalize.py` (순수 함수만). 소유자는 listing-collector, 사용자는 모든 agent.

## 1. 층 그룹 `classify_floor(raw: str | int | None) -> Literal["LOW","NORMAL","UNKNOWN"]`

처리 순서:
1. None, 빈 문자열 → `UNKNOWN`
2. 정수 입력(국토부): `<= 2` → `LOW` (0 이하 = 지하), `>= 3` → `NORMAL`
3. 문자열: 공백 제거 후 `/` 앞부분만 사용 (`"3/25"` → `"3"`)
4. 앞부분이 `저` → `LOW`, `중` 또는 `고` → `NORMAL`
5. `B`·`b`·`지하`·`반지하`로 시작 → `LOW`
6. 숫자로 변환 가능 → 2번 규칙
7. 그 외 → `UNKNOWN`

| 입력 | 기대 |
|---|---|
| `"1/25"` | LOW |
| `"2/15"` | LOW |
| `"3/25"` | NORMAL |
| `"저/15"` | LOW |
| `"중/20"` | NORMAL |
| `"고/20"` | NORMAL |
| `"B1/15"` | LOW |
| `"지하1/10"` | LOW |
| `" 12 / 25 "` | NORMAL |
| `""`, `None`, `"-/25"` | UNKNOWN |
| `1`, `2`, `0`, `-1` (int) | LOW |
| `3` (int) | NORMAL |

## 2. 면적
- `area_key(exclusive_m2: float) -> float` = `round(exclusive_m2, 2)`
- `to_pyeong(supply_m2: float) -> int` = `round(supply_m2 / 3.3058)`
- 조사 대상 판정: `supply_m2 <= 119.0`
- `match_area(exclusive_m2, area_types) -> AreaType | None`: 차이는 `round(abs(a - b), 2)`로 계산한다 (부동소수점 오차 방지). 차이 `<= 0.5`인 것 중 차이가 가장 작은 것. 차이가 같으면 area_key가 작은 쪽. 없으면 None.

| 입력 | 기대 |
|---|---|
| to_pyeong(112.4) | 34 |
| to_pyeong(119.0) | 36 |
| supply 119.0 → 대상 | True |
| supply 119.01 → 대상 | False |
| match_area(84.98, [84.97, 59.99]) | 84.97 |
| match_area(85.6, [84.97]) | None (차이 0.63) |
| match_area(84.5, [84.97, 84.03]) | 84.97과 차이 0.47, 84.03과 0.47 → area_key 작은 84.03 |

## 3. 가격 `parse_price(s: str) -> int` (만원 단위)
1. 공백·쉼표 제거
2. `억` 앞 숫자 × 10000 + `억` 뒤 숫자
3. `억`이 없으면 숫자 그대로 (만원으로 간주)
4. 숫자만 있는 국토부 `dealAmount`(예: `"125,000"`)도 같은 함수로 처리
5. 파싱 실패 → `ValueError` (0으로 대체 금지)

| 입력 | 기대 |
|---|---|
| `"12억 5,000"` | 125000 |
| `"9억"` | 90000 |
| `"12억5000"` | 125000 |
| `"8,500"` | 8500 |
| `" 125,000 "` | 125000 |
| `"10억 500"` | 100500 |
| `"가격문의"` | ValueError |

모든 테이블을 `pytest.mark.parametrize`로 그대로 옮긴다.

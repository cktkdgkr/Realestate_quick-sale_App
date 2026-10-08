# handoff: listing-collector (2단계)

작성일: 2026-10-08

## 1. 만든 파일
| 파일 | 내용 |
|---|---|
| `app/collectors/errors.py` | `CollectorError(stage, detail="", complex_no=None)`. stage가 §10 목록 밖이면 `ValueError` (오타 방지) |
| `app/domain/normalize.py` | `classify_floor`, `area_key`, `to_pyeong`, `is_target_area`, `match_area`, `parse_price`, `format_price` (§10 시그니처 그대로, 순수 함수) |
| `app/domain/dedup.py` | `dedup(listings)` + 보조 `make_dedup_key(...)`, `listing_key(listing)` |
| `app/collectors/naver_listings.py` | `NaverClient`, `fetch_complex`, `fetch_listings` (+ 보조 `NaverClient.get_json`, `article_url`) |
| `tests/unit/test_normalize.py` | 스킬 표 전부 parametrize + C2-2 반례 + CollectorError |
| `tests/unit/test_dedup.py` | D01~D06 + 키 형식, 경고 조건, 결정성 |
| `tests/unit/test_naver_listings.py` | 평형 필터, 3페이지 페이지네이션, blocked/schema_changed/network, 대기·백오프·타임아웃 |
| `tests/fixtures/naver/*_20261008.json`, `captcha_20261008.html` | **합성 fixture** (`_meta.synthetic=true`) |

테스트 결과: `pytest -q` 전체 257 passed (본인 테스트: normalize 71, dedup 12, naver_listings 29).

## 2. 엔드포인트 (미확인 — 반드시 4단계 전에 확인)
2026-10-08 현재 이 개발 환경은 네이버 접속이 막혀 있어 **실제 요청을 한 번도 보내지 않았다**. 아래는 naver-land-collector 스킬 §2의 후보 그대로다.

| 용도 | 요청 | 확인 날짜 |
|---|---|---|
| 단지·평형 | `GET https://new.land.naver.com/api/complexes/{complexNo}?sameAddressGroup=false` | 미확인 |
| 매매 매물 | `GET https://new.land.naver.com/api/articles/complex/{complexNo}?realEstateType=APT&tradeType=A1&page={n}&sameAddressGroup=false&order=prc` | 미확인 |

**남은 확인 절차 (스킬 §1)**: 브라우저로 `https://new.land.naver.com/complexes/3009`, `/22627`을 열어 Network 탭에서 위 두 요청의 URL·쿼리·헤더(`Authorization` 필요 여부 포함)를 확인 → 응답을 개인정보(중개사 이름·전화) 제거 후 `tests/fixtures/naver/<용도>_<YYYYMMDD>.json`으로 저장 → 합성 fixture 교체 → 이 표의 확인 날짜와 §3 필드명 갱신 → C3-1 재검증.

## 3. 응답 필드 매핑 (후보 기준)
단지 응답:
| 우리 필드 | 네이버 필드 | 필수 | 변환 |
|---|---|---|---|
| Complex.complex_no | `complexDetail.complexNo` | O | 요청 번호와 다르면 schema_changed |
| Complex.name | `complexDetail.complexName` | O | |
| Complex.lawd_cd | `complexDetail.cortarNo` | O | 앞 5자리 (법정동코드 10자리 가정) |
| Complex.address | `complexDetail.address` → 없으면 `roadAddress` | | 없으면 "" |
| Complex.max_floor | `complexDetail.highFloor` | | int, 없으면 None |
| Complex.molit_apt_seq | — | | 항상 None (complexes.yaml에서 채움) |
| AreaType.supply_m2 | `complexPyeongDetailList[].supplyArea` | O | float |
| AreaType.exclusive_m2 / area_key | `complexPyeongDetailList[].exclusiveArea` | O | float / round(,2) |
| AreaType.type_name | `complexPyeongDetailList[].pyeongName` (예 `112A`) | O | 매물의 `areaName`과 맞추는 데 쓴다 |
| AreaType.pyeong | — | | `round(supply/3.3058)` |

매물 응답 (`isMoreData`, `articleList` 둘 다 필수. `isMoreData`가 false일 때까지 page 증가):
| 우리 필드 | 네이버 필드 | 필수 | 변환 |
|---|---|---|---|
| article_no | `articleNo` | O | str. 페이지 경계 중복은 첫 건만 |
| price | `dealOrWarrantPrc` | O | `parse_price`, 실패 시 schema_changed |
| area_key | `areaName` → `area1`(공급) → `area2`(전용) | 셋 중 하나 | 아래 §4-2 |
| floor_raw / floor_group | `floorInfo` | | 없으면 "" → UNKNOWN |
| dong | `buildingName` | | 공백 제거, 없으면 "" |
| direction | `direction` | | 없으면 "" |
| confirmed_at | `articleConfirmYmd` (YYYYMMDD) | | 형식 이상이면 None |
| url | — | | `https://new.land.naver.com/complexes/{complexNo}?articleNo={articleNo}` |
| realtor_count / alt_prices / dedup_key | — | | 1 / [] / dedup 키 (dedup()이 다시 채움) |

## 4. 가정한 것
1. **CollectorError 시그니처**는 §10 (`stage, detail, complex_no`) 순서를 따랐다. 에이전트 정의 문서의 `(complex_no, stage, detail)` 순서는 §10에 의해 대체된 것으로 봤다.
2. **매물 ↔ 평형 매칭**: ① `areaName == AreaType.type_name` ② `area1`(공급)이 119.0 초과면 대상 외로 조용히 제외 ③ `area2`(전용) ±0.5㎡ `match_area`. 실제 응답의 `area1/area2`가 정수로 반올림돼 올 가능성이 있어 ①을 먼저 둔다. 어느 쪽으로도 못 찾은 공급 ≤119 (추정) 매물은 버리되 `client.warnings`와 로그에 건수를 남긴다 (실패로 보지 않음).
3. 같은 전용면적(area_key)의 평형이 둘 이상이면 첫 번째만 쓰고 `client.warnings`에 남긴다.
4. `fetch_listings`에 넘긴 대상 평형이 0개면 요청 없이 `[]`.
5. `alt_prices`: 그룹 내 **대표 가격과 다른** 호가만, 중복 제거·오름차순 (D01 → `[100000]`). 스킬 §2-4 "다른 가격 목록"을 이렇게 해석했다.
6. `dedup()`은 입력을 바꾸지 않고 사본을 돌려준다. `dedup_key`는 필드에서 다시 계산한다. 결과 순서는 그룹이 입력에 처음 나온 순서.
7. `parse_price`: 0 이하도 ValueError. 비문자열 입력도 ValueError. `"12억 5천"`, `"12.5억"`은 ValueError (명세 밖 형식).
8. `classify_floor`: 명세 처리 순서를 글자 그대로 따른다. 그래서 `"저층/15"`, `"3층"`은 UNKNOWN이다. 실제 응답에 이런 표기가 있으면 명세 변경이 필요하다.
9. 3xx 리다이렉트도 blocked로 본다 (API가 로그인·캡차 페이지로 보내는 경우). 404 등 그 밖의 4xx는 재시도 없이 `network`. 5xx·전송 오류만 재시도.
10. 대기: 두 번째 요청부터 매 요청 전 `2 + 3*rng()`초. 재시도 때는 백오프(5/15/45s)를 쉰 뒤 이 대기도 한 번 더 한다.
11. `Authorization` 토큰은 구현하지 않았다. 토큰이 필요하면 401이 나서 blocked로 실패가 드러난다 (조용히 0건이 되지 않음).

## 5. 다른 agent용 메모
- **trade-collector (`naver_trades`)**: 같은 `NaverClient` 인스턴스의 `client.get_json(path, params, complex_no=...)`을 쓰면 대기·재시도·blocked 감지가 그대로 적용된다. `client.blocked`가 설정되면 이후 모든 `get_json`은 요청 없이 `CollectorError("blocked")`.
- **pipeline**: `client.warnings`(list[str])에 실패는 아니지만 리포트에 보일 만한 경고가 쌓인다 (평형 미매칭 매물, 중복 평형). 필요하면 `cross_check_warnings`나 리포트 경고로 옮겨 달라. `NaverClient`는 `close()`·`with` 지원.
- dedup 경고(그룹 ≥3, 가격차 ≥5%)는 로그(`app.domain.dedup`)에만 남는다.

## 6. 알려진 한계·미해결
- **엔드포인트·필드명·토큰 요구 여부 모두 미확인** (§2). 합성 fixture로만 테스트했다 (C3-1 임시 완화). 4단계 전 실제 응답 교체와 C3-1·C8 재검증 필요.
- 스킬 §5 "매물 수가 지난 실행 대비 80% 이상 급감 시 경고"는 이전 실행 수를 알아야 하므로 이 모듈에 넣지 않았다. pipeline/history 쪽에서 구현할지 Orchestrator 결정 필요.
- 실제 `area1/area2`가 정수이고 `areaName`도 평형명과 다르게 오면 매칭이 대부분 실패한다. 이 경우 `client.warnings`에 건수가 크게 찍히므로 그걸 신호로 본다.
- 매물 `tradeTypeCode`가 A1인지 개별 확인하지 않는다 (요청 쿼리 `tradeType=A1`을 믿는다).
- 페이지 상한 200 (넘으면 schema_changed).

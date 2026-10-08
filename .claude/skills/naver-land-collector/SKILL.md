---
name: naver-land-collector
description: 네이버 부동산(new.land.naver.com)에서 아파트 단지 검색, 평형 목록, 매매 매물, 실거래 탭 데이터를 수집하는 방법. 요청 형식 확인 절차, 응답 필드 매핑, 요청 간격·재시도·실패 감지 규칙을 담는다. 네이버 수집 코드를 작성하거나 고칠 때 읽는다.
---

# 네이버 부동산 수집

## 0. 전제
- 네이버 부동산은 공식 공개 API가 없다. 웹 페이지가 내부적으로 호출하는 JSON 요청을 같은 방식으로 호출한다.
- 그 형식은 예고 없이 바뀐다. 아래 엔드포인트는 **후보**이며, 구현 시점에 반드시 직접 확인한다.
- 개인 참고용, 저빈도(주 1회) 수집을 전제로 한다. 이용약관상 자동 수집은 제한되므로 차단 회피 기법은 쓰지 않는다.

## 1. 요청 형식 확인 절차 (구현 시작 시 1회, 구조 변경 의심 시 매번)
1. 브라우저에서 `https://new.land.naver.com/complexes/<단지번호>` 를 열고 개발자도구 Network 탭에서 XHR/fetch만 본다.
2. 단지 정보, 매물 목록, 실거래 탭을 각각 눌러 호출되는 요청의 URL, 쿼리, 헤더(특히 `Authorization`, `Referer`)를 기록한다.
3. 응답 원본을 `tests/fixtures/naver/<용도>_<YYYYMMDD>.json`으로 저장한다. 중개사 전화번호·이름 등 개인정보는 지운다.
4. `docs/handoff/listing-collector.md`에 확인 날짜와 함께 기록한다.

## 2. 엔드포인트 후보 (확인 필요)
| 용도 | 후보 | 비고 |
|---|---|---|
| 단지 검색 | `GET /api/search?keyword={검색어}` | 결과에서 아파트 단지만 거른다 |
| 단지·평형 정보 | `GET /api/complexes/{complexNo}?sameAddressGroup=false` | 평형 목록(공급·전용 면적, 평형명), 법정동코드, 최고층 |
| 매매 매물 | `GET /api/articles/complex/{complexNo}?realEstateType=APT&tradeType=A1&page={n}&sameAddressGroup=false&order=prc` | `tradeType=A1`이 매매. `isMoreData`가 false가 될 때까지 page 증가 |
| 실거래 | `GET /api/complexes/{complexNo}/prices/real?tradeType=A1&areaNo={평형번호}&year=5` | 교차검증용 |

- 호스트: `https://new.land.naver.com`
- 페이지가 발급하는 `Authorization: Bearer ...` 토큰을 요구할 수 있다. 토큰은 단지 페이지 HTML을 받아 추출하는 방식이 흔하다. 확인한 방식대로 구현하되 토큰은 로그에 남기지 않는다.
- `sameAddressGroup=true`를 쓰면 네이버가 동일 매물을 묶어 주지만, 기준이 공개돼 있지 않으므로 **false로 받아서 우리 규칙(listing-dedup)으로 직접 묶는다.**

## 3. 응답 필드 매핑 (확인 후 실제 이름으로 갱신)
| 우리 필드 | 네이버 필드 후보 | 변환 |
|---|---|---|
| article_no | `articleNo` | 문자열 |
| supply_m2 / exclusive_m2 | `area1` / `area2` | float |
| floor_raw | `floorInfo` (예: `3/25`, `저/15`) | 그대로 저장, 그룹은 floor-area-normalizer |
| price | `dealOrWarrantPrc` (예: `12억 5,000`) | floor-area-normalizer의 가격 파서 |
| dong | `buildingName` (예: `101동`) | 공백 제거 |
| direction | `direction` | 그대로 |
| confirmed_at | `articleConfirmYmd` (YYYYMMDD) | date |
| url | — | `https://new.land.naver.com/complexes/{complexNo}?articleNo={articleNo}` |

평형 목록 응답에서 공급 면적이 **119.0㎡ 이하**인 타입만 `AreaType`으로 만든다.

## 4. 요청 정책
- 모든 요청은 **순차** 실행. 요청 사이 `random.uniform(2, 5)`초 대기.
- 일반 브라우저와 같은 `User-Agent`, `Referer: https://new.land.naver.com/` 사용. 그 외 위장(프록시 회전, 지문 조작, 캡차 우회)은 금지.
- 타임아웃 15초. 실패 시 지수 백오프(5s, 15s, 45s)로 최대 3회 재시도.
- HTTP 401/403/429 또는 캡차 페이지(HTML 응답) 감지 시: 재시도하지 말고 즉시 `CollectorError(stage="blocked")`. 같은 실행에서 남은 네이버 요청도 중단한다.
- 응답 JSON의 필수 필드가 없으면 `CollectorError(stage="schema_changed")`. 구조 변경 신호다.

## 5. 실패를 숨기지 않기
- 매물이 0건인 것과 수집에 실패한 것을 구분한다. 0건이면 정상 반환하고, 실패면 예외다.
- 매물 수가 지난 실행 대비 80% 이상 급감하면 경고를 남긴다 (구조 변경 의심).

## 6. 대안 (Orchestrator 승인 시에만)
JSON 요청이 막히면, Playwright로 페이지를 일반 브라우저처럼 열어 화면에 표시된 데이터를 읽는 방식을 검토한다. 이 경우에도 §4의 요청 정책과 금지 사항은 그대로 적용한다.

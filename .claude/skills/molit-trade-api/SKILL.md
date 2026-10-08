---
name: molit-trade-api
description: 공공데이터포털의 국토교통부 아파트 매매 실거래가 상세 자료 API로 단지별 매매 실거래를 수집하는 방법. 키 발급, 요청 파라미터, 응답 필드, 단지 식별, 해제 거래 처리, 평형 매칭 규칙을 담는다. 실거래 수집 코드를 작성·검증할 때 읽는다.
---

# 국토교통부 아파트 매매 실거래가 API

## 0. 확인 먼저
아래 엔드포인트·필드명은 작성 시점 기준 정보다. **구현 시작 시 공공데이터포털 API 상세 페이지와 실제 응답으로 반드시 재확인**하고, 다르면 실제 응답을 기준으로 구현한 뒤 `docs/handoff/trade-collector.md`에 차이를 기록한다.

## 1. 키 발급 (사용자 안내용)
1. data.go.kr 회원가입 → "국토교통부_아파트 매매 실거래가 상세 자료" 검색 → 활용신청
2. 마이페이지에서 일반 인증키(Decoding) 확인 → 서버의 `.env`에 `MOLIT_API_KEY`로 저장
3. 신청 직후에는 키가 활성화되기까지 시간이 걸릴 수 있다 (인증 오류가 나면 잠시 후 재시도)

## 2. 요청
```
GET https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev
  serviceKey = {MOLIT_API_KEY}      # Decoding 키를 넣고 httpx가 인코딩하게 한다 (이중 인코딩 주의)
  LAWD_CD    = {시군구 코드 5자리}   # 법정동코드 앞 5자리
  DEAL_YMD   = {YYYYMM}             # 계약년월
  pageNo     = 1..
  numOfRows  = 1000
```
- 최근 24개월 = 실행월 포함 25개 월을 조회한다 (월 경계 거래를 놓치지 않기 위해). 기간 필터는 판정 단계에서 정확히 한다.
- 같은 LAWD_CD의 여러 단지는 같은 응답을 공유하므로 `(LAWD_CD, DEAL_YMD)` 단위로 캐시한다.
- 응답 헤더 `resultCode`가 정상 코드가 아니면 `CollectorError(stage="molit_api", detail=resultMsg)`.
- `totalCount`와 받은 건수를 비교해 페이지를 끝까지 받는다.

## 3. 응답 필드 (재확인 필요)
| 우리 필드 | 응답 필드 후보 | 변환 |
|---|---|---|
| 단지 식별 | `aptSeq` (단지 일련번호), `aptNm`, `umdNm`, `jibun` | §4 |
| exclusive_m2 | `excluUseAr` | float |
| floor | `floor` | int → classify_floor |
| price | `dealAmount` (예: `125,000`) | parse_price |
| contract_date | `dealYear`, `dealMonth`, `dealDay` | date |
| cancelled | `cdealType` (해제 시 `O`), `cdealDay` | `cdealType == "O"` 이면 True |
| deal_type | `dealingGbn` (중개거래/직거래) | 문자열 |

## 4. 단지 식별 (네이버 단지 ↔ 국토부 거래)
국토부 데이터에는 네이버 단지번호가 없다. 다음 순서로 매칭한다.
1. **최초 1회 매핑**: 단지 등록 시 네이버 단지 정보의 법정동·지번·단지명으로 국토부 응답에서 후보를 찾고, 일치하는 `aptSeq`(또는 `umdNm`+`jibun` 조합)를 `complexes` 테이블에 저장한다.
2. 이후에는 저장된 식별자로만 거른다. 단지명 문자열 비교에 의존하지 않는다 (표기 차이가 잦다).
3. 매핑 후보가 0개 또는 2개 이상이면 자동 확정하지 말고 웹 화면에 "실거래 매칭 확인 필요"를 표시해 사용자가 고르게 한다.

## 5. 평형 매칭
- `floor-area-normalizer`의 `match_area` (±0.5㎡) 사용
- 매칭 실패 거래는 버리되 건수와 전용면적 값을 경고로 남긴다 (36평 초과 평형의 거래는 정상적으로 매칭 실패한다 — 이 경우는 경고하지 않는다)

## 6. 네이버 실거래 교차검증
- 같은 area_key, 같은 계약년월, 같은 층, 가격이 같은 거래를 짝짓는다.
- 경고 조건:
  - 국토부에만 있는 최근 2개월 이내 거래 → 정상일 수 있음 (네이버 반영 지연), info 수준
  - 네이버에만 있는 거래 → warning (국토부 매칭 실패 또는 단지 식별 오류 의심)
  - 같은 거래로 보이는데 가격이 다름 → warning
- 판정에는 항상 국토부 데이터만 쓴다.

## 7. 알려진 한계 (리포트·문서에 명시)
- 실거래 신고 기한은 계약 후 30일이므로 최근 거래가 늦게 나타난다.
- 해제 신고도 나중에 반영될 수 있어, 지난주 기준가와 이번 주 기준가가 달라질 수 있다.

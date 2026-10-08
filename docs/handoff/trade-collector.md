# trade-collector 인수인계 (2단계, 2026-10-08)

소유 파일: `app/collectors/molit_trades.py`, `app/collectors/naver_trades.py`,
`tests/unit/test_molit_trades.py`, `tests/unit/test_naver_trades.py`, `tests/unit/test_cross_check.py`,
`tests/fixtures/molit/*`, `tests/fixtures/naver/trades_*`.

**중요: 이 단계의 응답 형식은 전부 미확인이다.** 이 개발 환경은 국토부·네이버 접속이 막혀 있고 MOLIT 키도 아직 없다.
fixture는 스킬 문서(molit-trade-api §3, naver-land-collector §2)의 필드 후보로 만든 **합성 fixture**다
(XML 주석 `_meta synthetic=true`, JSON `"_meta": {"synthetic": true}`). 실제 응답 형식 확인과 fixture 교체는 **4단계로 미룬다**
(C3-1 임시 완화, CLAUDE.md §9). 확인 후 차이를 이 문서에 적는다.

## 1. 공개 함수 (CLAUDE.md §10 시그니처 그대로)
| 함수 | 동작 |
|---|---|
| `MolitClient(api_key, http=None, sleep=time.sleep)` | 순차 요청, 요청 간 1초 대기, 타임아웃 15초, 네트워크 오류·5xx·429는 5/15/45초 백오프로 최대 3회 재시도. `(LAWD_CD, DEAL_YMD)` 캐시는 인스턴스 안에만 (실행 1회 = 인스턴스 1개). `close()`/컨텍스트 매니저 지원(주입받은 http는 닫지 않음) |
| `fetch_trades(client, complex, area_types, as_of)` | as_of 달 포함 25개 월 조회, `totalCount`까지 페이지네이션, `aptSeq == complex.molit_apt_seq`만 사용, 해제 거래는 `cancelled=True`로 포함 |
| `find_apt_seq_candidates(client, complex, as_of)` | 같은 LAWD_CD 25개월 응답에서 `aptSeq`별 후보 dict 목록. **자동 확정하지 않는다** |
| `fetch_naver_trades(client: NaverClient, complex, area_types)` | 교차검증용 네이버 실거래. 실패는 `CollectorError` |
| `cross_check(molit, naver, as_of)` | 경고 문자열 목록 (아래 §5) |
| (보조) `fetch_naver_trades_or_warning(...)` | 실패 시 `(None, [경고])`. 현재 pipeline은 자체 try/except를 쓰므로 선택 사항 |

## 2. 국토부 엔드포인트와 필드 매핑 (가정, 4단계 확인 필요)
`GET https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev`
파라미터: `serviceKey`(Decoding 키를 그대로 넘기고 httpx가 1회 인코딩), `LAWD_CD`(lawd_cd 앞 5자리), `DEAL_YMD`(YYYYMM), `pageNo`, `numOfRows=1000`. 응답은 XML로 가정.

| Trade 필드 | 응답 필드 | 변환 |
|---|---|---|
| (단지 식별) | `aptSeq` | `complex.molit_apt_seq`와 문자열 일치 (앞뒤 공백 제거) |
| exclusive_m2 | `excluUseAr` | float |
| area_key | — | `normalize.match_area(excluUseAr, area_types).area_key` |
| floor / floor_group | `floor` | int / `classify_floor(int)` (0 이하·1·2 → LOW) |
| price | `dealAmount` (`"235,000"`) | `parse_price` → 만원 int |
| contract_date | `dealYear`,`dealMonth`,`dealDay` | date |
| cancelled | `cdealType` | `"O"`이면 True (`cdealDay`는 미사용) |
| deal_type | `dealingGbn` | 문자열 그대로 (`중개거래`/`직거래`) |
| source | — | `"MOLIT"` |

필수 필드(`aptSeq, excluUseAr, dealAmount, dealYear, dealMonth, dealDay`)가 대상 단지 거래에 없으면 `schema_changed`.
`header/resultCode`가 `00`/`000`이 아니면 오류. 후보 dict 키: `apt_seq, apt_nm, umd_nm, jibun, trade_count, last_contract, score, hints`.

### 오류 분류
| 상황 | stage |
|---|---|
| HTTP 401/403, resultCode·returnReasonCode 20/30/31/32, 본문에 `SERVICE_KEY`·`UNAUTHORIZED` | `molit_auth` |
| 그 밖의 resultCode 오류(예: 22 호출 한도 초과), 기타 4xx, XML 아님, totalCount보다 적게 받음 | `molit_api` |
| 재시도 3회 후에도 연결 실패·5xx | `network` |
| 필수 필드 누락·변환 실패 | `schema_changed` |
| `molit_apt_seq` None·빈 문자열·공백만, lawd_cd 형식 오류 | `complex_mapping` (요청 전에 발생) |

## 3. 단지 식별
- `Complex.molit_apt_seq`(= 응답 `aptSeq`)로만 거른다. 단지명은 비교하지 않는다 (테스트: 이름이 달라도 aptSeq가 같으면 포함, 이름이 같아도 aptSeq가 다르면 제외).
- None·빈 문자열·공백만이면 요청 없이 `CollectorError("complex_mapping")` (반려 1 수정). 후보는 `find_apt_seq_candidates`로 따로 얻는다.
- 후보 `score`는 참고용(법정동 일치 +1, 주소 토큰에 지번 일치 +2, 정규화 단지명 포함 관계 +1)이며 정렬에만 쓴다. 후보가 1개여도 확정하지 않는다 → 리포트에 후보 목록과 `complexes.yaml` 수정 방법 표시(pipeline 담당).

## 4. 평형 매칭과 경고 전달 방식
- `normalize.match_area` (±0.5㎡, 동률이면 작은 area_key).
- **36평 초과 판정(추정)**: 매칭 실패 거래의 전용면적이 `max(area_key) + 0.5`보다 크면 조사 대상 밖 큰 평형으로 보고 **경고 없이** 제외(debug 로그만). AreaType에는 공급 ≤119.0㎡ 평형만 있어 큰 평형의 전용면적을 알 수 없기 때문의 근사다. 공급이 커도 전용이 대상 평형보다 작은 특이 단지라면 경고가 나온다(오탐, 누락은 아님).
- 그 밖의 매칭 실패는 단지별 1줄로 `MolitClient.warnings`에 쌓는다. 예:
  `[WARN] 단지 3009 국토부 실거래 평형 매칭 실패 1건 제외 (전용 72.3㎡ 1건)`
- 층 값이 정수가 아닌 거래는 제외하고 `[WARN] ... 층 값 오류 N건 제외`.
- **pipeline 요청**: CLAUDE.md §9(수집기 경고 전달 방식)에 따라 `fetch_trades` 뒤에 `molit_client.drain_warnings()`를 호출해 리포트 context의 `collector_warnings`에 넣어 주세요. 현재 `app/pipeline.py`는 이 경고를 읽지 않는다. 네이버 실거래 층 오류 경고는 `NaverClient.warnings`에 들어간다. `cross_check` 결과는 `cross_check_warnings`용이다.

## 5. 네이버 실거래·교차검증
- 요청(가정): ① `GET /api/complexes/{no}?sameAddressGroup=false` 로 `pyeongName → pyeongNo` 매핑(AreaType.type_name = pyeongName), ② 평형마다 `GET /api/complexes/{no}/prices/real?complexNo=..&tradeType=A1&areaNo={pyeongNo}&year=5&type=table`. 모두 `NaverClient.get_json`(순차·2~5초 대기·차단 감지) 경유.
- 응답 필드(가정): `realPriceOnMonthList[].realPriceList[]` 의 `tradeType`(A1만), `tradeYear/tradeMonth/tradeDate`, `dealPrice`(만원 int, 없으면 `formattedPrice`를 parse_price), `floor`, `deleteYn`(Y → cancelled), `exclusiveArea`(없으면 AreaType 값).
- 필수 필드 `tradeType, tradeYear, tradeMonth, tradeDate, floor`(키) 중 하나라도 없으면 `schema_changed` (명세 확정 ④).
- 층: `parse_naver_trade_floor` — 지하 표기 `B1`/`b2`/`지하1`은 음의 정수(-1, -2), `B`·`지하`만 있으면 -1, 끝의 `층`은 뗀다. 그 밖에 정수가 아니면(`옥탑` 등) 제외하고 `NaverClient.warnings`에 건수 경고 (명세 확정 ③).
- `cross_check` 규칙: 비교 범위는 국토부 조회 범위(as_of 달 포함 25개 월의 1일 ~ as_of), 해제 거래 제외. 키 `(단지, area_key, 계약년월, 층)`, 같은 가격끼리 먼저 짝짓고 남은 것끼리 가격 불일치로 짝짓는다. 출력은 결정적 정렬.
  | 종류 | 수준 |
  |---|---|
  | 가격 불일치 | `[WARN] 교차검증 가격 불일치` |
  | 국토부에만 있음 | 기간과 상관없이 `[INFO]` (명세 확정 ①). 계약일 ≥ as_of−2개월이면 "네이버 반영 지연 가능"을 덧붙인다 |
  | 네이버에만 있음 | `[WARN]`. 같은 키·가격의 국토부 해제 거래가 있으면 "국토부에서는 해제 거래"로 표시 |
- 판정에는 국토부 Trade만 쓴다 (cross_check는 판정에 영향 없음).

## 6. 비밀값
- 키는 생성자 인자로만 받는다(환경변수 읽기는 config 담당). 예외·로그 메시지는 `mask_secrets`로 `serviceKey=***` 처리하고 키 원문·URL 인코딩형(대소문자 무관)도 지운다. 응답 본문은 파싱 전에 통째로 가린다. 오류 메시지를 80자로 자를 때 키 일부가 남지 않게 하려는 것이다(반려 2 수정). httpx/httpcore 로거에 마스킹 필터를 붙여 httpx INFO 요청 로그의 키도 가린다. `repr(MolitClient)`에 키 없음. fixture에 키 없음(테스트로 검사).

## 7. 가정·알려진 한계
- 실거래 신고 기한은 계약 후 30일 → 최근 거래가 늦게 나타난다. 해제 신고도 나중에 반영되어 주마다 기준가가 달라질 수 있다.
- 응답 형식(XML 여부, 필드명, resultCode 값 `000`/`00`, 게이트웨이 오류 형식)은 미확인. 네이버 실거래 엔드포인트·`areaNo`=pyeongNo 가정·페이지네이션 유무(`addedRowCount` 등)도 미확인.
- 네이버 실거래 수집은 단지 정보를 한 번 더 요청한다(pyeongNo를 얻기 위해, §5 스키마에 pyeongNo 없음). 요청 1회/단지 추가.
- resultCode `03`(NO_DATA)은 4단계 확인 전까지 오류(`molit_api`)로 처리한다 (명세 확정 ②).
- 36평 초과 판별 근사(max(area_key)+0.5)의 타당성은 4단계 C8에서 확인한다 (명세 확정 ⑤).
- `area_types`가 비어 있으면 `fetch_trades`·`fetch_naver_trades` 모두 **요청 없이** 빈 목록을 돌려준다(경고 없음). `fetch_trades`는 그 전에 molit_apt_seq·lawd_cd 검증을 먼저 한다.

## 8. 매칭 실패 사례 (합성 fixture 기준)
- 전용 72.30㎡ (59.99·84.97 어느 쪽과도 0.5 초과) → 경고 후 제외.
- 전용 114.50㎡ (최대 84.97+0.5 초과) → 36평 초과로 간주, 경고 없이 제외.
- 전용 59.96㎡ → 59.99에 매칭 (차이 0.03). 72.80 평형이 있으면 72.30은 정확히 0.5 차이로 매칭(경계 포함).
- 실제 단지(잠원동아 3009, 잠실엘스 22627) 사례는 4단계에서 추가.

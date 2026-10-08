# 프로젝트 명세: 네이버 부동산 급매 알림 앱 (naver-bargain-alert)

이 파일은 모든 agent가 공유하는 **단일 진실 원천(Single Source of Truth)** 이다.
명세를 바꿀 수 있는 것은 Orchestrator뿐이며, 바꿀 때는 아래 "변경 이력"에 기록한다.
명세와 코드가 다르면 명세가 옳다. 명세가 모호하면 추측하지 말고 Orchestrator에게 질문한다.

---

## 1. 앱이 하는 일

매주 **화요일 10:00 (Asia/Seoul)** 에 자동 실행된다.

1. 사용자가 웹 화면에서 등록한 아파트 단지 목록을 읽는다.
2. 각 단지의 **공급면적 36평(119.0㎡) 이하** 모든 평형에 대해:
   - 네이버 부동산에서 **매매** 매물을 수집한다.
   - 국토교통부 실거래가 API에서 **매매** 실거래를 수집하고, 네이버 실거래 탭과 교차검증한다.
3. 급매 판정 규칙(§4)에 따라 급매를 찾는다.
4. 신규 급매 또는 가격 인하된 급매가 있으면 리포트 맨 위 **"이번 주 알림"** 섹션에 올리고, 실행 요약(`out/summary.md`) 첫 줄에 건수를 적는다. (텔레그램 사용 안 함 — §9 참고)
5. 급매 유무와 상관없이 **HTML 주간 리포트 파일**을 만든다. (이메일 발송 안 함 — §9 참고)
6. 수집이 실패하면 "급매 없음"으로 처리하지 않고, 리포트와 실행 요약 맨 위에 **"수집 실패"** 를 표시한다.

### 1.1 실행 환경 (2026-10-08 확정)
- 서버 없이 **Claude Routine(정기 실행)** 으로 돌린다. Routine은 매주 화요일 10:00 KST에 새 클라우드 세션을 열고, 이 저장소에서 `python -m app.pipeline --once`를 실행한다.
- 실행 결과는 저장소의 **`reports` 브랜치**에 커밋·푸시해 보존한다.
  - `reports/YYYY-MM-DD.html`: 주간 리포트
  - `state/history.sqlite3`: 재알림 이력 DB (다음 실행이 이 파일을 읽는다)
- 사용자에게는 Routine 완료 알림(Claude 앱 푸시·메일)으로 `out/summary.md` 내용이 전달된다. 앱 코드는 메일·메신저를 직접 보내지 않는다.
- 같은 코드는 사용자 PC에서도 `python -m app.pipeline --once`로 똑같이 돌아가야 한다.

## 2. 기술 스택 (변경 시 Orchestrator 승인 필요)

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.12 |
| 단지 등록 | `config/complexes.yaml` (단지 URL 또는 complex_no 목록). 웹 서버 없음 |
| 리포트 | Jinja2로 만든 단일 HTML 파일 (외부 CSS·JS 없이 인라인) |
| DB | SQLite (SQLAlchemy 2.x, Alembic 마이그레이션) |
| HTTP | httpx (타임아웃·재시도 필수) |
| 스케줄 | Claude Routine, cron `CRON_TZ=Asia/Seoul 0 10 * * 2` (앱 안에 상주 스케줄러 없음) |
| 테스트 | pytest, 외부 호출은 전부 fixture/mock |
| 배포 | 없음. Routine 세션 또는 사용자 PC에서 실행 (Docker·VM 사용 안 함) |
| 비밀값 | `.env` (저장소에 커밋 금지, `.env.example`만 커밋) |

## 3. 용어와 데이터 정의

### 3.1 평형 (area_key)
- 평형은 **전용면적(㎡, 소수 둘째 자리 반올림)** 으로 식별한다. 공급면적은 표시·필터용이다.
  - 이유: 국토부 실거래 데이터에는 전용면적만 있다. 같은 공급평형에 전용면적이 다른 타입(예: 84A, 84B)이 있을 수 있다.
- 조사 대상: 네이버 단지 평형 목록에서 **공급면적 ≤ 119.0㎡** 인 모든 타입.
- 1평 = 3.3058㎡. 평 표기는 `공급㎡ / 3.3058`을 반올림한 정수.
- 실거래 ↔ 평형 매칭: 실거래 전용면적과 area_key의 차이가 **±0.5㎡ 이내**인 경우만 같은 평형으로 본다.
  - 두 평형에 동시에 매칭되면 차이가 가장 작은 쪽에 배정한다.

### 3.2 층 그룹 (floor_group)
| 원본 표기 | floor_group |
|---|---|
| 숫자 1, 2 | `LOW` (저층 그룹) |
| 지하·반지하 (B1, 지하 등) | `LOW` |
| 텍스트 `저` | `LOW` |
| 숫자 3 이상 | `NORMAL` (일반층) |
| 텍스트 `중`, `고` | `NORMAL` |
| 파싱 불가·누락 | `UNKNOWN` → **판정 제외**, 리포트에 "층 미상"으로 표시, 비교 기준(최저가)에도 포함하지 않음 |

- 네이버 층 표기 예시: `3/25`, `저/15`, `고/20`, `B1/15`. 슬래시 앞부분만 사용한다.
- 국토부 실거래의 층은 항상 숫자이므로 1·2층(또는 0 이하) → `LOW`, 3층 이상 → `NORMAL`.

### 3.3 가격
- 모든 가격은 **만원 단위 정수**로 저장한다. 예: `12억 5,000` → `125000`, `9억` → `90000`.
- 부동소수점으로 비교하지 않는다. 비율 비교는 정수 곱셈으로 한다 (§4.3).

### 3.4 매물 중복 묶기 (dedup)
- 같은 집이 여러 중개사를 통해 올라온 매물은 1건(대표 매물)으로 묶는다.
- 묶는 키: `(단지, area_key, 동, 층 원본 표기, 방향)`. 동·방향이 비어 있으면 그 필드는 키에서 빈 값으로 둔다.
- 같은 키 안에서 가격이 다르면 **최저가 매물**을 대표로 삼고, 나머지 가격·중개사 수는 메타데이터로 보관한다.
- 판정·최저가 계산은 **대표 매물 기준**으로만 한다.

## 4. 급매 판정 규칙 (가장 중요)

### 4.1 기준값
같은 단지·같은 area_key 안에서 계산한다.

- **실거래 기준가 `T_normal`**: `NORMAL` 그룹 실거래 중
  - 해제된 거래(해제 여부 표시가 있는 거래)를 제외하고,
  - 계약일이 실행일 기준 **최근 24개월 이내**인 거래를 대상으로,
  - 계약일 최신순으로 **최대 3건**을 뽑아 **중앙값**을 구한다.
  - 1~2건뿐이면 있는 건의 중앙값(2건이면 평균의 내림)을 쓰고 `trade_sample_short=True`로 표시한다.
  - 0건이면 `T_normal = None` (실거래 조건 생략, 리포트에 "실거래 부족").
- **매물 최저가 `L_normal(x)`**: `NORMAL` 그룹 대표 매물 중 **매물 x 자신을 제외한** 최저가. 비교할 매물이 없으면 `None`.
- 실거래 기준은 **일반층 실거래만** 쓴다. 저층 그룹 실거래는 기준값 계산에 쓰지 않는다 (리포트에는 참고로 표시).

### 4.2 판정
| 매물 그룹 | 급매 조건 (하나라도 참이면 급매) |
|---|---|
| `NORMAL` | ① `T_normal`이 있고 `가격 ≤ T_normal × 95%` ② `L_normal(x)`가 있고 `가격 ≤ L_normal(x) × 95%` |
| `LOW` | ① `T_normal`이 있고 `가격 ≤ T_normal × 90%` ② `NORMAL` 대표 매물 최저가가 있고 `가격 ≤ 그 최저가 × 90%` |
| `UNKNOWN` | 판정하지 않음 |

- `LOW` 매물의 ②는 일반층 매물과 비교하므로 자기 자신 제외 문제가 생기지 않는다.
- 판정 결과에는 **어떤 조건으로 급매가 됐는지**(`reasons`: `TRADE`, `LISTING` 중 해당 항목)와 기준값, 할인율을 함께 남긴다.

### 4.3 비교식 (정수 연산, 경계 포함)
```
NORMAL: price * 100 <= base * 95
LOW:    price * 100 <= base * 90
```
`≤` 이므로 정확히 95%·90%인 가격도 급매다.

### 4.4 재알림 규칙
- 급매 이력은 dedup 키 단위로 DB에 저장한다 (`first_alerted_at`, `last_alerted_price`).
- **신규 급매** → 리포트 "이번 주 알림" 섹션 + "신규" 표시.
- **이전에 알린 급매가 더 낮은 가격으로** 다시 급매 → "이번 주 알림" 섹션 + "가격 인하 (이전가 → 현재가)".
- **같거나 높은 가격으로 계속 급매** → "이번 주 알림"에 넣지 않고, 리포트 본문에 "지속 중"으로만 표시.
- 매물이 사라지면 이력은 유지하되 리포트에 "지난주 급매 중 내려간 매물"로 1회 표시.

## 5. 공통 데이터 스키마 (모듈 간 인터페이스)

모든 모듈은 아래 dataclass(또는 pydantic 모델)로만 데이터를 주고받는다. 필드 변경은 Orchestrator 승인 필요.

```python
Complex(complex_no: str, name: str, lawd_cd: str, address: str, max_floor: int | None,
        molit_apt_seq: str | None)   # 국토부 단지 식별자. None이면 "실거래 매칭 확인 필요"
AreaType(complex_no: str, area_key: float, exclusive_m2: float, supply_m2: float, pyeong: int, type_name: str)
Listing(article_no: str, complex_no: str, area_key: float, dong: str, floor_raw: str,
        floor_group: Literal["LOW","NORMAL","UNKNOWN"], direction: str, price: int,
        confirmed_at: date | None, realtor_count: int, alt_prices: list[int],
        dedup_key: str, url: str)
Trade(complex_no: str, area_key: float, exclusive_m2: float, floor: int,
      floor_group: Literal["LOW","NORMAL"], price: int, contract_date: date,
      cancelled: bool, deal_type: str, source: Literal["MOLIT","NAVER"])
Verdict(listing: Listing, is_bargain: bool, reasons: list[Literal["TRADE","LISTING"]],
        trade_base: int | None, listing_base: int | None, discount_pct: float | None,
        trade_sample_short: bool, alert_kind: Literal["NEW","PRICE_DROP","ONGOING", None])
RunResult(run_id: str, started_at: datetime, status: Literal["OK","PARTIAL","FAILED"],
          verdicts: list[Verdict], errors: list[str], cross_check_warnings: list[str])
```

## 6. 디렉터리 구조

```
app/
  collectors/naver_listings.py     # 매물 조사 agent
  collectors/naver_trades.py       # 실거래 조사 agent (교차검증용)
  collectors/molit_trades.py       # 실거래 조사 agent
  domain/normalize.py              # 층·면적·가격 정규화 (공용, 매물 조사 agent 소유)
  domain/dedup.py                  # 매물 조사 agent
  domain/rules.py                  # 급매 판정 agent (순수 함수, I/O 금지)
  notify/report.py, notify/summary.py, notify/history.py, notify/templates/   # 알림·리포트 agent
  config.py                        # 웹·인프라 agent (complexes.yaml·.env 로드)
  db/ , pipeline.py                # 웹·인프라 agent (pipeline은 Orchestrator와 공동)
config/complexes.yaml              # 조사 대상 단지 목록 (사용자 편집)
tests/
  fixtures/                        # 실제 응답을 저장한 JSON/XML (개인정보 제거)
  unit/, integration/
.env.example                      # MOLIT_API_KEY 등 (실제 .env는 커밋 금지)
docs/   RUNBOOK.md, USER_GUIDE.md, ROUTINE.md (Routine 설정·reports 브랜치 운영)
```

## 7. 실패 처리 원칙
- 단지 하나가 실패해도 나머지 단지는 계속 처리한다 → `status=PARTIAL`.
- 실패한 단지·평형은 리포트와 실행 요약에 **명시적으로** "수집 실패"로 표시한다. 절대 "급매 없음"으로 보이면 안 된다.
- 국토부와 네이버 실거래가 다르면 국토부를 기준으로 쓰고 차이를 `cross_check_warnings`에 남긴다.

## 10. 모듈 함수 인터페이스 (2단계 계약, 변경은 Orchestrator 승인)

모든 모듈은 §5 dataclass만 주고받는다. 아래 이름·시그니처를 그대로 쓴다. 내부 보조 함수는 자유다.

```python
# app/collectors/errors.py  (소유: listing-collector)
class CollectorError(Exception):
    def __init__(self, stage: str, detail: str = "", complex_no: str | None = None): ...
    # stage: "blocked" | "schema_changed" | "network" | "molit_api" | "molit_auth" | "complex_mapping"

# app/domain/normalize.py  (소유: listing-collector, 순수 함수)
classify_floor(raw: str | int | None) -> Literal["LOW","NORMAL","UNKNOWN"]
area_key(exclusive_m2: float) -> float
to_pyeong(supply_m2: float) -> int
is_target_area(supply_m2: float) -> bool            # supply_m2 <= 119.0
match_area(exclusive_m2: float, area_types: list[AreaType]) -> AreaType | None
parse_price(s: str) -> int                          # 만원 정수, 실패 시 ValueError
format_price(manwon: int) -> str                    # 125000 -> "12억 5,000", 90000 -> "9억", 8500 -> "8,500"

# app/domain/dedup.py  (소유: listing-collector)
dedup(listings: list[Listing]) -> list[Listing]      # 대표 매물만 반환

# app/collectors/naver_listings.py  (소유: listing-collector)
class NaverClient:                                   # 순차 요청·2~5초 대기·재시도·blocked 감지, sleep/clock 주입 가능
    def __init__(self, http: httpx.Client | None = None, sleep=time.sleep, rng=random.random): ...
fetch_complex(client, complex_no: str) -> tuple[Complex, list[AreaType]]   # AreaType은 공급 ≤119.0㎡만
fetch_listings(client, complex: Complex, area_types: list[AreaType]) -> list[Listing]  # dedup 전 원본 매물

# app/collectors/molit_trades.py  (소유: trade-collector)
class MolitClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, sleep=time.sleep): ...
fetch_trades(client, complex: Complex, area_types: list[AreaType], as_of: date) -> list[Trade]  # 25개월, 해제 거래 포함
find_apt_seq_candidates(client, complex: Complex, as_of: date) -> list[dict]   # molit_apt_seq 미설정 시 후보 제시용

# app/collectors/naver_trades.py  (소유: trade-collector)
fetch_naver_trades(client: NaverClient, complex: Complex, area_types: list[AreaType]) -> list[Trade]
cross_check(molit: list[Trade], naver: list[Trade], as_of: date) -> list[str]  # cross_check_warnings 문자열

# app/domain/rules.py  (소유: bargain-judge, 순수 함수, I/O·현재 시각 호출 금지)
judge(listings: list[Listing], trades: list[Trade], as_of: date) -> list[Verdict]
    # 한 단지 × 한 area_key 단위. 반환 Verdict.alert_kind는 항상 None (분류는 notify.history가 채운다)
area_summary(listings: list[Listing], trades: list[Trade], as_of: date) -> dict
    # 리포트 현황표용: t_normal, trade_sample_count, trade_sample_short, l_normal_min, l_low_min, listing_count, unknown_count

# app/notify/history.py  (소유: notifier)
classify_alerts(session, verdicts: list[Verdict], failed_complex_nos: set[str], run_id: str,
                dry_run: bool) -> tuple[list[Verdict], list[dict]]
    # alert_kind를 채운 Verdict 목록, 그리고 "지난주 급매 중 내려간 매물" 목록. dry_run이면 DB를 바꾸지 않는다
commit_history(session, ...)                         # 리포트·요약 파일 쓰기가 성공한 뒤에만 호출

# app/notify/report.py, app/notify/summary.py  (소유: notifier)
render_report(run: RunResult, context: dict) -> str  # 단일 HTML (인라인 스타일)
render_summary(run: RunResult, context: dict) -> str # out/summary.md 내용. 첫 줄 = 상태·신규/인하 건수·수집 실패 여부
    # context에는 단지별 Complex, AreaType, area_summary, 내려간 매물, 실패 단지 목록 등이 들어간다. 키 이름은 notifier가 정하고 handoff에 적는다

# app/pipeline.py  (소유: web-infra, Orchestrator 공동)
main(argv) -> int     # python -m app.pipeline --once [--dry-run]. 종료 코드: OK=0, PARTIAL=1, FAILED=2
```

## 8. 하지 말 것
- 짧은 간격의 대량 요청, 병렬 크롤링. 네이버 요청은 순차 실행, 요청 간 2~5초 랜덤 대기.
- 로그인·캡차 우회, 프록시 회전 등 차단 회피 기법.
- 비밀값(API 키, 토큰, 비밀번호, 사용자 이메일)을 코드·로그·fixture에 남기는 것.
- 테스트에서 실제 외부 서비스 호출 (통합 리허설 단계 제외).
- **과금 관련 일체 금지** (Orchestrator 포함 모든 agent에 적용, 예외 없음):
  - 결제·구독·요금제·크레딧·청구(billing) 설정을 조회·변경·생성하지 않는다.
  - 유료 서비스·유료 API·유료 요금제, 사용량에 따라 과금되는 클라우드 리소스(VM, 매니지드 DB, 유료 메일 발송 서비스 등)를 가입·생성·사용하지 않는다.
  - 결제 수단(카드 등) 등록이나 결제가 필요한 절차를 진행하거나 사용자에게 권유하지 않는다.
  - 무료 범위라도 초과 시 자동 과금될 수 있는 설정(종량제 전환, 자동 충전 등)을 켜지 않는다.
  - 어떤 작업이 과금을 일으킬 가능성이 조금이라도 있으면 실행하지 말고 Orchestrator → 사용자에게 먼저 묻는다.
  - 허용 범위: 무료로 제공되는 공공데이터포털 API, 텔레그램 Bot API, 사용자 본인 계정의 무료 메일 발송, 사용자 본인 PC에서의 실행.

## 9. 변경 이력
| 날짜 | 변경 | 사유 |
|---|---|---|
| (최초) | 명세 확정 | 사용자 인터뷰 결과 반영 |
| 2026-10-08 | §8에 "과금 관련 일체 금지" 추가 | 사용자 요청: 모든 agent(Orchestrator 포함)가 과금 관련 내용을 건드리거나 사용하지 않도록 |
| 2026-10-08 | §1.1 신설, §1·§2·§4.4·§6·§7 수정: 텔레그램·이메일·웹 서버·Docker·APScheduler 제거 → Claude Routine 실행 + HTML 리포트 파일 + `reports` 브랜치 보존 + `config/complexes.yaml` | 사용자 답변: 서버 없음, 실행은 Claude Routine, 리포트는 HTML 파일, 텔레그램 없음. 스킬 문서(notify-telegram-email, schedule-deploy 등)의 텔레그램·이메일·Docker·APScheduler 내용은 이 변경으로 **무효**이며, 충돌 시 이 명세를 따른다. §5 스키마는 변경 없음(`alert_kind`는 리포트 분류에 그대로 사용). |
| 2026-10-08 | 명세 확정: `DRY_RUN=true`이면 `alert_history`를 갱신하지 않고 `reports` 브랜치에도 올리지 않는다. 리포트와 요약은 `out/`에만 쓰고, 분류(NEW/PRICE_DROP/ONGOING)는 기존 이력을 읽기 전용으로 써서 계산한다 | 1단계 질문 해소 |
| 2026-10-08 | 명세 확정: `reports` 브랜치는 코드와 무관한 **orphan 브랜치**다. 코드 브랜치에서는 `state/`, `reports/`, `out/`을 ignore한다. Routine 실행 시 `git worktree`로 reports 브랜치를 열어 `state/history.sqlite3`를 읽고, 실행 후 리포트·DB를 그 브랜치에 커밋·푸시한다 | 1단계 질문 해소 |
| 2026-10-08 | 명세 확정: 업무 시간대는 코드 상수 `Asia/Seoul`로 **고정**한다. 설정 키·OS 환경변수(`TZ` 등)로 바꿀 수 없다. 기준일 `as_of`, 24개월 창, 리포트 파일명은 모두 KST 날짜다 | 1단계 검증 모호점 Q1 |
| 2026-10-08 | 명세 확정: `.env.example`에는 비밀값이 아닌 경로 설정 키(`DB_PATH`, `REPORT_DIR`, `OUT_DIR`, `COMPLEXES_FILE`)를 두어도 된다 | 1단계 검증 모호점 Q2 |
| 2026-10-08 | §10 모듈 함수 인터페이스 신설 | 2단계 병렬 구현 시 모듈 간 연결을 고정 |
| 2026-10-08 | 체크리스트 조정 (텔레그램·이메일·웹·Docker 제거에 따름): C6-3은 "리포트·요약 파일 쓰기 성공 후에만 이력 커밋"으로 바꾼다. C6-4는 "summary.md 첫 줄에 상태·신규/인하 건수·수집 실패 표시"로 바꾼다. C6-5는 "HTML 리포트에 notify-telegram-email §4 본문 1~7 순서(웹 링크 제외)"로 바꾼다. C7-1·C7-2·C7-6은 삭제한다. C7-4는 "docs/ROUTINE.md의 cron이 화요일 10:00 KST이고, as_of가 TZ=UTC 환경에서도 KST 날짜"로 바꾼다. C7-5는 `runs` 테이블 RUNNING 락으로 유지한다. C7-7의 RUNBOOK 증상에서 "메일 안 옴·서버 재시작"은 "Routine 실행 실패·reports 브랜치 푸시 실패"로 대체한다. molit 단지 매핑의 "웹 화면에 확인 필요"는 "리포트에 후보 목록과 complexes.yaml 수정 방법 표시"로 대체한다 | §1.1 변경의 후속 |
| 2026-10-08 | C3-1 임시 완화: 이 개발 환경은 네이버·국토부 접속이 막혀 있다. 2단계 fixture는 스킬 문서의 필드 후보로 만든 **합성 fixture**를 허용한다. 대신 파일에 `"_meta": {"synthetic": true}`(XML이면 주석)를 표시한다. 4단계 전에 실제 응답으로 교체하고 C3-1·C4를 재검증한다 | 네트워크 정책 |
| 2026-10-08 | 명세 확정(§4.1): 실거래 표본이 0건이면 `T_normal=None`이고 `trade_sample_short=True`(표본 < 3). `rules.judge`의 입력이 한 단지·한 area_key가 아니거나 dedup_key가 겹치면 ValueError를 낸다. area_key는 normalize.area_key로 반올림한 값끼리 비교한다. area_summary의 listing_count에는 UNKNOWN 매물을 포함하고, unknown_count는 따로 센다 | bargain-judge 2단계 가정 확정 |
| 2026-10-08 | 명세 확정(rules): ① UNKNOWN 매물 Verdict는 is_bargain=False, reasons=[], listing_base·discount_pct=None으로 둔다. trade_base·trade_sample_short에는 평형 공통값을 참고용으로 넣어도 된다. ② judge 반환 순서는 (price, dedup_key, article_no) 오름차순으로 고정한다. 리포트는 이 순서에 의존하지 말고 스스로 정렬한다. ③ §10에 없는 공개 보조 함수(예: trade_base)는 허용한다. 다른 모듈은 §10 함수만 쓴다. ④ discount_pct = (base − price) / base × 100을 정수 연산으로 소수 첫째 자리까지 ROUND_HALF_UP한 값으로 한다(표시 전용, 판정에는 쓰지 않음) | 판정 모듈 검증 모호점 해소 |
| 2026-10-08 | 통합 리허설 단지 확정: 잠원동아(complex_no 3009), 잠실엘스(complex_no 22627) | 사용자 답변 |

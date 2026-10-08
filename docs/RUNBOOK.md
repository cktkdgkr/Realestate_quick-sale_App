# RUNBOOK — 장애 대응 (증상 → 원인 → 조치)

먼저 볼 곳
- **실행 요약 첫 줄** (Routine 완료 알림 / `out/summary.md`): 상태 OK·PARTIAL·FAILED와 수집 실패 여부
- **HTML 리포트의 "경고·오류" 섹션**: 단지별 실패 단계(`blocked`, `schema_changed`, `network`, `molit_api`, `molit_auth`, `complex_mapping`)
- **Routine 세션 로그**: `scripts/routine_run.sh`의 stderr (`[routine] ...`, 파이프라인 로그)
- **실행 기록 DB**: `reports` 브랜치 `state/history.sqlite3`의 `runs` 테이블 (시작·종료 시각, 상태, 단지별 결과 `complex_results`, 오류 `errors`, 경고)
  ```bash
  git fetch origin reports && git show origin/reports:state/history.sqlite3 > /tmp/h.sqlite3
  sqlite3 /tmp/h.sqlite3 "select run_id, status, as_of, errors from runs order by started_at desc limit 5;"
  ```

비밀값 원칙: API 키를 로그·이슈·대화창에 붙여 넣지 않습니다. 키는 Routine 환경변수 또는 `.env`에서만 바꿉니다.

| 종료 코드 | 상태 |
|---|---|
| 0 / 1 / 2 | OK / PARTIAL / FAILED (파이프라인) |
| 3 | 준비 실패 (원격 조회·worktree) |
| 4 | reports 브랜치 푸시 실패 |

---

## 1. "수집 실패 (blocked)" — 네이버 접속 차단 의심
- **증상**: 요약에 `[수집 실패 있음]`, 리포트에 "네이버 접속 차단 의심". 첫 차단 이후 단지들은 "앞 단지에서 차단 감지되어 요청 중단"으로 함께 실패.
- **원인**: 네이버가 HTTP 401/403/429, 리다이렉트, 캡차(HTML) 페이지를 돌려줌. Routine 세션의 클라우드 IP가 막혔을 수 있음.
- **조치**:
  1. 아무것도 하지 말고 **한두 주 기다립니다.** 주 1회 저빈도 수집이라 대개 풀립니다.
  2. 같은 실행 안에서 재시도하지 않는 것이 정상 동작입니다. 프록시·헤더 위장·캡차 우회 등 **차단 회피는 하지 않습니다** (CLAUDE.md §8).
  3. 3주 이상 계속되면: 사용자 PC에서 드라이런(`python -m app.pipeline --once --dry-run`)으로 같은 증상인지 확인하고, Orchestrator에게 수집 방식 점검을 요청합니다(naver-land-collector §6 대안은 승인 필요).
  4. 이 기간 실패 단지의 급매 이력은 바뀌지 않습니다 (다음 성공 때 이어서 분류).

## 2. "schema_changed" — 네이버 구조 변경
- **증상**: "네이버 응답 형식 변경 의심", 또는 경고 "매물 수 급감 — 지난 실행 N건 → 이번 M건 (80% 이상 감소)", "평형을 특정하지 못한 매물 n건 제외"가 크게 늘어남.
- **원인**: 네이버 내부 JSON 요청의 주소·필드 이름이 바뀜.
- **조치**:
  1. naver-land-collector 스킬 §1 절차로 브라우저 개발자도구에서 현재 요청 형식을 다시 확인합니다.
  2. 실제 응답을 `tests/fixtures/naver/`에 개인정보를 지우고 저장한 뒤, `app/collectors/naver_listings.py`·`naver_trades.py`의 필드 매핑을 고치고 테스트를 통과시킵니다 (매물 조사 agent 담당).
  3. 고칠 때까지의 리포트는 해당 단지가 "수집 실패"로 표시되므로 "급매 없음"으로 오해할 일은 없습니다.

## 3. 국토부 API 인증 오류 / 키 만료 / 키 없음
- **증상**:
  - "국토부 API 인증키 오류"(`molit_auth`) → 해당 단지가 수집 실패(PARTIAL/FAILED).
  - "국토부 API 키 없음 — 실거래 기준 생략" → 키가 비어 있음. 매물끼리 비교만 하고 상태 **PARTIAL**.
  - "국토부 실거래 API 오류"(`molit_api`) → 서비스 장애·일일 호출 한도 등.
- **원인**: 키 만료·오타·활용신청 기간 종료, Routine 환경변수 누락, 공공데이터포털 장애.
- **조치**:
  1. 공공데이터포털(data.go.kr) 마이페이지 → 활용신청 현황에서 "국토교통부_아파트 매매 실거래가 상세 자료"의 상태·기간을 확인합니다. 만료면 연장 신청 (무료).
  2. 일반 인증키(**Decoding**)를 다시 복사해 **Routine 환경변수 `MOLIT_API_KEY`** (PC면 `.env`)에 넣습니다. 새로 발급한 키는 활성화까지 시간이 걸릴 수 있습니다.
  3. `molit_api`가 하루 이틀 반복되면 포털 공지를 확인하고 다음 주를 기다립니다.

## 4. "실거래 매칭 확인 필요" / "complex_mapping"
- **증상**: 리포트에 실거래 매칭 확인 필요와 후보 목록. 그 단지는 매물 기준으로만 판정.
- **원인**: `config/complexes.yaml`에 `molit_apt_seq`가 없음. 또는 후보 조회 실패(경고에 "국토부 단지 매핑 후보 조회 실패").
- **조치**: [USER_GUIDE §2](USER_GUIDE.md#2-실거래-매칭-확인-필요-처리)대로 후보를 골라 `molit_apt_seq`를 적습니다. 후보가 0개면 lawd_cd·단지명을 확인하고 Orchestrator에게 문의합니다. 자동 확정은 하지 않습니다.

## 5. Routine 실행 실패 (결과 알림이 안 옴 / 이상함)
- **증상**: 화요일 10시가 지나도 완료 알림이 없음, 또는 알림 첫 줄이 `[FAILED] 실행 요약(out/summary.md)이 만들어지지 않았습니다`, 또는 종료 코드 3.
- **원인과 조치**:
  | 원인 | 확인 | 조치 |
  |---|---|---|
  | Routine이 꺼져 있음·일정 오류 | Routine 화면의 다음 실행 시각 | cron `CRON_TZ=Asia/Seoul 0 10 * * 2` 확인 ([ROUTINE.md](ROUTINE.md)) |
  | 사용량 한도로 실행 안 됨 | Routine 실행 기록 | 한도가 풀린 뒤 "지금 실행"으로 수동 실행 (결제·요금제 변경은 하지 않음) |
  | 패키지 설치 실패 | 세션 로그의 pip 오류 | 네트워크 허용 목록에 PyPI가 있는지 확인 후 재실행 |
  | 네트워크 허용 도메인 누락 | 오류가 `network` 단계, 모든 단지 실패 | `new.land.naver.com`, `apis.data.go.kr` 허용 |
  | 종료 코드 3 (원격 조회 실패) | stderr `[routine] 실패: 원격 'origin' 조회 실패` | GitHub 접근 권한·네트워크 확인 후 재실행. 이때 새 reports 브랜치를 만들지 않으므로 이력은 안전 |
  | 설정 오류 (complexes.yaml) | 요약에 "설정 오류: ..." | 파일 수정 ([USER_GUIDE §1](USER_GUIDE.md#1-단지-추가삭제)) |
  | 파이프라인 오류 (템플릿·코드 버그) | 요약에 "파이프라인 오류: ..." | 이력은 커밋되지 않았으므로(파일 쓰기 성공 후에만 커밋) 고친 뒤 재실행하면 됨 |
- **놓친 주 처리**: 다음 정기 실행을 기다리거나 "지금 실행"으로 수동 실행합니다. 같은 주 두 번 실행해도 이력 분류는 안전합니다 (두 번째는 대부분 "지속 중").

## 6. reports 브랜치 푸시 실패 (종료 코드 4)
- **증상**: 요약 맨 위 `[FAILED] reports 브랜치 푸시 실패 — 이번 결과·이력이 저장소에 보존되지 않았습니다`.
- **원인**: Routine 세션의 푸시 권한이 `claude/` 접두 브랜치로 제한됨, 저장소 보호 규칙(branch protection), 네트워크 오류, 동시에 다른 곳에서 `reports`에 푸시해 충돌.
- **조치**:
  1. 세션 로그의 git 오류를 봅니다 (자격 증명은 `***`로 가려져 있음).
  2. 권한 문제면 Routine/GitHub 설정에서 `reports` 브랜치 푸시를 허용합니다.
  3. 충돌이면 다른 곳(사용자 PC 등)에서 `routine_run.sh`를 동시에 돌리지 않았는지 확인합니다. 다음 실행이 원격 최신본을 다시 받아 이어 갑니다.
  4. 이번 주 이력은 저장되지 않았으므로, 다음 실행에서 이번 주 급매가 다시 "신규"로 나올 수 있습니다 (알림 누락보다 중복이 낫다는 정책).

## 7. 중복 실행 / 락
- **증상**: 로그에 `다른 실행(...)이 진행 중입니다. 이번 실행은 중단합니다`, 종료 코드 2, `out/`은 그대로.
- **원인**: 같은 DB로 두 실행이 겹침 (`runs` 테이블에 RUNNING 행이 있음).
- **조치**: 기다렸다가 다시 실행합니다. 2시간이 넘은 RUNNING은 비정상 종료로 보고 다음 실행이 자동으로 FAILED 처리하고 진행합니다.

## 8. 사용자 PC에서 실행할 때 (서버 재시작에 해당)
- 서버가 없으므로 재시작할 것이 없습니다. PC에서는 `python -m app.pipeline --once --dry-run`으로 확인만 하고,
  정식 실행은 Routine에 맡기는 것을 권장합니다. PC에서 정식 실행하려면 `bash scripts/routine_run.sh`를 씁니다
  (원격 `reports` 브랜치의 DB를 받아서 쓰고 다시 푸시하므로 Routine과 이력이 갈라지지 않습니다).

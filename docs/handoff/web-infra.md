# Handoff: 웹·인프라 agent — 2단계 (pipeline, Routine 스크립트, 운영 문서)

기준: CLAUDE.md 2026-10-08 최신본 (§1.1 Routine, §9 확정 사항, §10 인터페이스).
schedule-deploy 스킬은 §3 비밀값, §4 runs 실행 기록, §5 문서 구성만 따랐다 (Docker·APScheduler·웹은 무효).
1단계 내용(DB 스키마, config)은 바꾸지 않았다. 스키마 변경 없음 (마이그레이션 0001 그대로).

## 만든·고친 파일
| 파일 | 내용 |
|---|---|
| `app/pipeline.py` | `main(argv) -> int`, `python -m app.pipeline --once [--dry-run]`. 종료 코드 OK=0, PARTIAL=1, FAILED=2 |
| `scripts/routine_run.sh` | reports orphan 브랜치 worktree → 파이프라인 → 커밋·푸시 → summary.md 출력 |
| `docs/ROUTINE.md`, `docs/USER_GUIDE.md`, `docs/RUNBOOK.md` | 2단계 작성 |
| `tests/integration/test_pipeline.py` | 35개: 실패 격리, PARTIAL·FAILED, blocked 중단, 교차검증 실패, 매핑 없음, 키 없음, 락, 드라이런, TZ=UTC as_of, 비밀값 가림, 매물 급감, 실제 rules·notify 모듈과 2주 연속 실행 |
| `tests/integration/test_routine_run.py` | 13개: 로컬 임시 bare 저장소로 스크립트 검증 (실제 원격 푸시 없음) |
| `tests/unit/test_docs.py` | ROUTINE cron, RUNBOOK 증상, USER_GUIDE 항목 |
| `tests/unit/test_layout.py` | 1단계 "stub 실패" 테스트를 `--once` 필수 확인으로 교체 (실제 수집이 돌지 않게) |
| `.gitignore` | `/.worktrees/` 추가 |

`pytest -q` (네트워크 차단 상태): 이 agent 담당 테스트는 전부 통과했다. 마지막 전체 실행은 434 passed, 1 failed였고, 실패한 1건은 trade-collector가 수정 중인 `tests/unit/test_naver_trades.py::test_fetch_naver_trades_parses_and_maps_area`다. 그 직전 전체 실행은 409 passed였다.

## pipeline 처리 순서
단지마다 (순차):
`fetch_complex` → (yaml의 `molit_apt_seq`가 있으면 Complex에 덮어씀) → `fetch_listings` → `dedup`
→ `fetch_trades` + `MolitClient.drain_warnings()` → `fetch_naver_trades_or_warning` → `cross_check`
→ area_key별(`normalize.area_key`로 묶음) `judge`·`area_summary`.
단지 처리 뒤 `NaverClient.warnings`도 비워서 `collector_warnings`에 옮긴다.

전체: 매물 수 급감 검사 → `classify_alerts(..., target_complex_nos=complexes.yaml 단지)` → `prior_alerts`
→ `report.write_outputs` (render_report·render_summary·파일 쓰기) → (드라이런이 아닐 때만) 스냅샷 저장 + `commit_history(..., dry_run=False, target_complex_nos=..., now=...)`.
`commit_history`는 **파일 쓰기가 예외 없이 끝난 뒤에만** 부른다. 렌더링·쓰기가 실패하면 세션을 커밋하지 않고 FAILED + 최소 요약.

### 실패 처리 (handoff 명시 요구 사항)
- **단지 격리**: 단지 처리 중 어떤 예외든 그 단지만 FAILED. 일부 실패 = PARTIAL(1), 전부 실패 = FAILED(2).
  실패 단지는 `failed_complex_nos`로 classify·commit에 넘겨 이력을 보호하고, context `failures`에 `{complex_no, name, stage, detail}`로 넣는다.
  평형 단위 실패는 따로 두지 않는다. 평형 하나에서 judge·area_summary가 실패해도 **단지 전체**가 실패로 처리된다 (Orchestrator 지시 4).
- **국토부 실거래 수집 실패** (`fetch_trades` 예외): 그 단지 전체 수집 실패. 실거래 조건 없이 판정하면 급매가 "내려간 매물"로 오인될 수 있어서다.
- **네이버 실거래 교차검증 실패**: 판정을 막지 않는다. `fetch_naver_trades_or_warning`이 준 경고(그 밖의 예외도)를 `cross_check_warnings`에 남기고, 판정은 국토부 실거래로 진행한다.
- **국토부 단지 매핑 없음** (yaml·수집기 모두 `molit_apt_seq` 없음): `fetch_trades`와 네이버 실거래를 부르지 않고 trades=[]로 판정한다(T_normal=None, **매물 기준으로만**).
  `find_apt_seq_candidates` 결과를 context `molit_candidates[complex_no]`에 넣고, 리포트가 "실거래 매칭 확인 필요"와 후보, complexes.yaml 수정 방법을 표시한다.
  후보 조회가 실패하면 `collector_warnings`에 남기고 판정은 계속한다. 매핑 없음은 수집 실패가 아니다(이력 정상 갱신).
- **MOLIT_API_KEY 없음** (Orchestrator 확정): 모든 단지를 매물 기준으로만 판정하고 status는 **PARTIAL**로 낸다(모든 단지가 다른 이유로 실패하면 FAILED).
  `errors`·`collector_warnings`에 "국토부 API 키 없음 — 실거래 기준 생략"을 넣고, summary.md 첫 줄 바로 아래에 `## 주의` 블록으로 같은 문장을 넣는다(첫 줄은 notifier 형식 그대로 둠).
  이때 모든 단지를 `failed_complex_nos`로 넘겨 이력을 보호한다. 실거래로만 급매였던 매물을 "내려감"으로 처리하지 않으려는 것이다. 대신 이번 주 신규 급매는 이력에 기록되지 않아 다음 실행에서 다시 "신규"로 나온다.
- **blocked**: 네이버 호출(fetch_complex, fetch_listings, 네이버 실거래)에서 `stage=="blocked"` 예외가 나거나 `NaverClient.blocked`가 설정되면, 남은 단지는 네이버 요청 없이 `stage="blocked"`로 실패 처리한다.
  교차검증 단계에서 차단되면 그 단지는 판정을 유지(OK)하고 다음 단지부터 중단한다.
- **매물 수 급감**: 이번에 성공한 단지마다, 그 단지 수집이 OK였던 직전 정식 실행(dry_run=False, status OK/PARTIAL, `runs.complex_results`)의 `listings_snapshot` 대표 매물 수와 비교한다.
  `cur*100 <= prev*20`(80% 이상 감소, 경계 포함)이면 `collector_warnings`에 넣는다.
- **비밀값**: 오류·경고 문자열은 `sanitize()`로 API 키 값, `serviceKey=`, `Bearer` 토큰, URL 자격 증명을 `***`로 바꾼 뒤 DB·리포트·요약에 넣는다.

### as_of와 시각
`as_of = datetime.now(UTC).astimezone(config.BUSINESS_TZ).date()`. OS의 TZ와 무관하다(TZ=UTC + `time.tzset()` 상태에서 4개 경계 시각으로 테스트).
`RunResult.started_at`은 Asia/Seoul aware. `run_id = YYYYMMDD-HHMMSS-<6hex>` (KST).

### 락 (C7-5)
`runs`에 RUNNING 행을 넣고 커밋한 뒤, 살아 있는 RUNNING 중 **rowid(삽입 순서)가 가장 작은** 실행만 진행한다. 진 쪽은 자기 행을 FAILED("중복 실행 차단")로 바꾸고 종료 코드 2를 낸다(`out/`은 건드리지 않음).
2시간 넘은 RUNNING은 FAILED("비정상 종료")로 바꾸고 진행한다. 드라이런도 락을 잡는다. 둘 다 `out/`에 쓰기 때문이다.

### 드라이런
`--dry-run` 또는 `DRY_RUN=true`. classify는 `dry_run=True`이고 commit_history와 스냅샷 저장은 하지 않는다.
리포트는 `out/YYYY-MM-DD.html`, 요약은 `out/summary.md`에 쓴다(`write_outputs` 규칙). `runs`에는 dry_run=True 행만 남는다(락·기록용, 급감 비교에서는 제외).

### DB 기록
정식 실행에서는 `complexes`·`area_types`를 갱신하고 `listings_snapshot`(대표 매물), `trades_snapshot`(국토부+네이버), `verdicts`(alert_kind 포함)를 저장한다.
`runs.cross_check_warnings`에는 교차검증 경고와 수집기 경고를 합쳐 저장하고, `complex_results`에는 단지별 status·stage·error·건수·mapping_needed를 저장한다.

## routine_run.sh
- `git ls-remote --exit-code`로 원격 reports 브랜치를 확인한다. 2(없음)일 때만 새 orphan을 만든다. 그 밖의 실패는 **종료 코드 3**이다(이력 단절 방지).
- worktree: `.worktrees/reports` (gitignore). `DB_PATH`·`REPORT_DIR`은 worktree를, `OUT_DIR`은 `<repo>/out`을 가리킨다.
- 파이프라인 종료 코드가 0/1/2이면 커밋·푸시한다(FAILED도 기록 보존). 그 밖의 코드(크래시)면 올리지 않고 2를 낸다.
- 푸시 실패는 **종료 코드 4**이고, 요약 맨 위에 `[FAILED] reports 브랜치 푸시 실패` 줄을 붙인다. summary.md가 없으면 `[FAILED] ...`를 출력하고 2를 낸다.
- git 출력의 URL 자격 증명과 serviceKey는 sed로 가린다. 커밋 작성자가 설정돼 있지 않으면 `naver-bargain-alert routine <routine@localhost>`를 쓴다.
- `PIPELINE_CMD` 환경변수로 파이프라인 명령을 바꿀 수 있다(테스트용).

## 다른 모듈과의 연결 확인 (최종 시점)
실제 모듈의 시그니처를 `inspect`로 확인했고, 모두 §10과 pipeline 호출이 일치한다:
NaverClient, fetch_complex, fetch_listings, MolitClient(+drain_warnings, close), fetch_trades, find_apt_seq_candidates,
fetch_naver_trades(_or_warning), cross_check, dedup, judge, area_summary, classify_alerts/commit_history(target_complex_nos 포함), prior_alerts, write_outputs, render_report/summary.
`test_end_to_end_with_real_rules_and_notify`는 수집기만 fake로 두고 실제 dedup·rules·history·report·summary로 2주를 실행한다. 1주차 신규 2, 2주차 인하 1·지속 1이 나오고, 실패 단지와 매핑 후보가 표시되는 것을 확인했다.

## 가정
1. 리포트 context 키는 notifier 계약(app/notify/report.py docstring)을 따른다. `naver_blocked`는 추가 키다(참고용).
2. yaml의 `molit_apt_seq`가 수집기가 준 값보다 우선한다.
3. 락에서 진 실행도 FAILED 행으로 `runs`에 남긴다(감사 기록용).
4. 실행 기록이 FAILED라도 reports 브랜치에 푸시한다(무엇이 실패했는지 보존).

## 한계 / 미해결
- Routine 세션이 `reports` 브랜치로 푸시할 수 있는지는 실제 Routine에서만 확인할 수 있다. `claude/` 접두 브랜치만 허용되는 환경이면 설정을 바꿔야 한다(ROUTINE.md, RUNBOOK §6).
- 실제 네이버·국토부 접속은 한 번도 하지 않았다(네트워크 정책). 통합 리허설(C8)에서 확인해야 한다.
- 개발 저장소의 `out/summary.md`(as_of 2026-10-09, FAILED)는 이 agent의 테스트가 만든 것이 아니다. 누군가 저장소에서 파이프라인을 직접 실행한 흔적이다. `out/`은 gitignore 대상이다.
- 키 없음 모드의 `## 주의` 블록은 pipeline이 summary.md를 후처리해서 넣는다. notifier가 이 경우를 직접 렌더링하도록 바꾸면 후처리를 빼는 것이 깔끔하다.

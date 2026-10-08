# Handoff: 웹·인프라 agent — 1단계 (골격과 인터페이스 고정)

기준: CLAUDE.md 2026-10-08 개정판 (§1.1 Routine 실행, 웹 서버·Docker·APScheduler·텔레그램·SMTP 없음).
`.claude/agents/web-infra.md`, `schedule-deploy` 스킬의 웹·Docker·스케줄러·텔레그램 내용은 CLAUDE.md §9에 따라 따르지 않았다.

## 한 일

| 파일 | 내용 |
|---|---|
| `pyproject.toml` | Python 3.12 (`>=3.12,<3.13`). 의존성 httpx, sqlalchemy>=2, alembic, jinja2, pyyaml, python-dotenv. dev: pytest, respx. fastapi·apscheduler·uvicorn 없음 |
| `app/domain/models.py` | CLAUDE.md §5 dataclass 6종. 필드명·타입·순서 그대로, 기본값 없음, 추가 필드 없음 |
| `app/config.py` | `load_settings()` (.env + 환경변수, 환경변수 우선), `load_complexes()` (complexes.yaml), `parse_complex_no()` (번호 또는 네이버 단지 URL) |
| `config/complexes.yaml` | 잠원동아(3009), 잠실엘스(22627) |
| `app/db/models.py`, `types.py`, `session.py`, `migrate.py` | ORM 7개 테이블, UTC 시각 타입, 엔진(외래키 ON), 마이그레이션 적용 함수 |
| `app/db/migrations/` + `alembic.ini` | Alembic 초기 리비전 `0001` (autogenerate 후 `alembic check`로 모델과 차이 없음 확인) |
| `app/pipeline.py` | 2단계용 자리. 지금 실행하면 exit 2로 **실패**한다 (아무것도 안 하고 성공하면 "급매 없음"으로 오인될 수 있어서) |
| 타 agent 모듈 | `collectors/*`, `domain/normalize.py`, `domain/dedup.py`, `domain/rules.py`, `notify/report.py`, `notify/summary.py`, `notify/history.py`: docstring 한 줄만. `notify/templates/`는 `.gitkeep`만 |
| `docs/RUNBOOK.md`, `USER_GUIDE.md`, `ROUTINE.md` | §6 구조용 자리 (2단계에서 작성한다고 명시) |
| `.env.example` | MOLIT_API_KEY(빈 값), DRY_RUN, DB_PATH, REPORT_DIR, OUT_DIR, COMPLEXES_FILE. TZ 키 없음 (업무 시간대는 코드 상수) |
| `tests/conftest.py` | 모든 테스트에서 AF_INET/AF_INET6 소켓 접속 차단 (autouse). `tmp_db_path`, `fixtures_dir` fixture |
| `tests/unit/`, `tests/integration/` | models(§5 일치), config, 디렉터리 구조(§6), 네트워크 차단, 마이그레이션·DB 왕복 테스트 |

### 사용법
```
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                 # 74 passed
.venv/bin/python -m app.db.migrate  # 설정의 DB_PATH에 마이그레이션 적용
.venv/bin/alembic upgrade head      # 같은 동작 (CLI)
```

### DB 테이블 요약
- `complexes`(complex_no PK): §5 Complex 필드 + updated_at. **조사 대상 원본은 complexes.yaml**이고 이 테이블은 수집기가 채운 메타데이터 캐시다.
- `area_types`: §5 AreaType 필드, (complex_no, area_key) 유니크.
- `runs`(run_id PK): started_at, finished_at, status(`RUNNING`|`OK`|`PARTIAL`|`FAILED`), dry_run, as_of(KST 실행일), errors(JSON), cross_check_warnings(JSON), complex_results(JSON: 단지별 상태·오류), report_path.
- `listings_snapshot`: run별 대표 매물, §5 Listing 필드 그대로. (run_id, dedup_key) 유니크.
- `trades_snapshot`: run별 실거래, §5 Trade 필드 그대로 (해제 거래 포함).
- `verdicts`: listing_snapshot_id(FK, 유니크) + dedup_key + §5 Verdict의 나머지 필드.
- `alert_history`(dedup_key PK): complex_no, area_key, first_alerted_at, last_alerted_at, last_alerted_price, last_seen_run_id, active, deactivated_run_id.

## 가정한 것 / 확정된 것
1. **시각 저장**: 모든 DB 시각 컬럼은 `UTCDateTime` — 시간대 있는 datetime만 받고 UTC로 저장, 읽을 때 UTC aware로 반환. naive datetime은 오류. `RunResult.started_at`은 파이프라인에서 Asia/Seoul aware로 만들 것을 전제.
2. **DRY_RUN 의미 (확정, CLAUDE.md §9)**: `DRY_RUN=true`이면 alert_history를 갱신하지 않고 reports 브랜치에도 올리지 않는다. 리포트·요약은 `out/`에만 쓴다. NEW/PRICE_DROP/ONGOING 분류는 기존 이력을 읽기 전용으로 써서 계산한다. 2단계 pipeline에서 이대로 구현한다.
3. **alert_history 상태 표현**: notify 스킬 §2의 `active` 불리언을 따르고, "내려간 매물 1회 표시"를 위해 `deactivated_run_id`(비활성화된 실행)를 추가했다. 이 열 의미를 알림·리포트 agent가 확인해야 한다. 필요하면 마이그레이션 0002로 조정한다.
4. **complexes.yaml 형식**: 최상위 `complexes:` 목록. 항목은 숫자, URL 문자열, 또는 `{complex_no|url, name, molit_apt_seq}` 맵. 알 수 없는 키·중복·complex_no/url 불일치·빈 목록은 `ConfigError` (조용히 건너뛰지 않음). `molit_apt_seq`를 yaml에 적을 수 있게 한 것은 "실거래 매칭 확인 필요"를 사용자가 해소할 수단이 필요해서다.
5. **경로**: 상대 경로는 저장소 루트 기준. OUT_DIR(기본 `out`, §1.1의 `out/summary.md`)와 COMPLEXES_FILE을 설정 키로 추가했다.
6. **reports 브랜치 운영 (확정, CLAUDE.md §9)**: `reports`는 코드와 무관한 orphan 브랜치다. 코드 브랜치에서는 `.gitignore`로 `/state/`, `/reports/`, `out/`을 제외한다. Routine 실행 때는 `git worktree`로 reports 브랜치를 따로 열어 `state/history.sqlite3`를 읽고, 실행 후 리포트와 DB를 그 브랜치에 커밋·푸시한다. 그래서 DB_PATH·REPORT_DIR이 worktree 경로를 가리키게 설정한다. 구체적인 절차는 2단계에서 docs/ROUTINE.md에 적는다. `.env`가 ignore되는 것도 확인했다.
8. **업무 시간대 (확정, CLAUDE.md §9)**: `app/config.py`의 상수 `BUSINESS_TZ = ZoneInfo("Asia/Seoul")`로 고정했다. `Settings.tz`와 `Settings.zoneinfo`는 이 상수를 돌려주는 읽기 전용 속성이고, `.env`나 OS 환경변수 `TZ`는 무시한다. 반려 R1을 반영한 것이며, `environ={"TZ":"UTC"}`에서도 Asia/Seoul이 되는지와 08:00 KST의 기준일이 KST 날짜인지를 테스트한다.
7. `discount_pct`는 §5가 float이므로 Float 컬럼. 판정 비교에는 쓰지 않는 표시용이다. 가격 컬럼은 전부 Integer.

## 한계 / 미해결
- pipeline 본체, Routine 설정, RUNBOOK·USER_GUIDE·ROUTINE 문서는 2단계 범위라 비어 있다.
- "중복 실행 방지 락"은 Routine 방식에서 필요성이 낮다고 보고 `runs.status=RUNNING` 열만 마련했다. 2단계에서 필요 여부를 정한다.
- `tests/fixtures/`는 비어 있다 (수집 agent들이 채운다).

## 질문 (Orchestrator)
- 이전 질문 1(DRY_RUN 의미)과 2(reports 브랜치 운영)는 CLAUDE.md §9에 확정되어 해소됐다.
- §5 스키마 변경 요청은 없다.
- 남은 확인 사항: `alert_history.deactivated_run_id`의 의미(가정 3)는 알림·리포트 agent가 확인해야 한다.

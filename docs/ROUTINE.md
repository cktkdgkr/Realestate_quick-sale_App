# Claude Routine 설정과 reports 브랜치 운영

이 앱은 서버 없이 **Claude Routine(정기 실행)** 으로 돌아갑니다 (CLAUDE.md §1.1).
매주 화요일 10:00(한국 시간)에 Routine이 새 클라우드 세션을 열고, 이 저장소에서
`scripts/routine_run.sh`를 실행합니다. 결과는 저장소의 `reports` 브랜치에 쌓이고,
실행 요약(`out/summary.md`)이 Routine 완료 알림으로 전달됩니다.

> Routine 실행은 사용자 본인의 Claude 사용량을 씁니다. 주 1회, 짧은 셸 실행 위주라 사용량이 크지 않지만,
> 사용량 한도에 걸리면 그 주 실행이 안 될 수 있습니다. 이 문서는 요금제·결제 설정을 다루지 않습니다.

---

## 1. Routine 만들기

| 항목 | 값 |
|---|---|
| 저장소 | 이 저장소 (코드 기본 브랜치) |
| 일정 (cron) | `CRON_TZ=Asia/Seoul 0 10 * * 2` → 매주 **화요일 10:00 KST** |
| 네트워크 허용 도메인 | `new.land.naver.com` (네이버 부동산), `apis.data.go.kr` (국토부 실거래 API). 그 밖에 패키지 설치용 PyPI와 GitHub 푸시가 막혀 있으면 함께 허용 |
| 환경변수 | `MOLIT_API_KEY` = 공공데이터포털 일반 인증키(Decoding). Routine 환경 설정의 비밀값/환경변수 칸에 직접 넣습니다. 프롬프트·저장소·대화창에는 적지 않습니다 |
| 브랜치 푸시 | Routine 세션이 `reports` 브랜치에 푸시할 수 있어야 합니다. 세션이 `claude/` 접두 브랜치에만 푸시하도록 제한되어 있으면 `reports` 브랜치 푸시를 허용하세요 (안 되면 [RUNBOOK](RUNBOOK.md) "reports 브랜치 푸시 실패") |

- cron 표현식에 시간대를 붙일 수 없는 화면이면, 시간대 선택 칸에서 `Asia/Seoul`을 고르고 `0 10 * * 2`를 넣습니다.
  UTC 기준만 지원하면 `0 1 * * 2` (UTC 화요일 01:00 = KST 화요일 10:00)입니다.
- 앱 코드는 서버 시간대와 상관없이 **기준일(as_of)을 한국 날짜로** 계산합니다. Routine 세션이 UTC여도 리포트 날짜는 KST입니다.

### 환경 준비 (세션 시작 시)
Routine의 환경 설정(setup) 단계에 다음을 넣습니다. 프롬프트 안에서 해도 됩니다.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -q -e .
```

## 2. Routine 프롬프트 초안

```text
이 저장소의 주간 급매 리포트를 실행한다.

1. 저장소 루트에서 다음을 실행한다 (가상환경이 없으면 먼저 만든다):
   python3.12 -m venv .venv && .venv/bin/pip install -q -e .
   bash scripts/routine_run.sh
2. 스크립트가 표준출력으로 낸 내용(out/summary.md)을 **그대로** 이번 실행의 최종 답변으로 보고한다.
   요약하거나 고치지 않는다. 첫 줄의 상태(OK / PARTIAL / FAILED)를 바꾸지 않는다.
3. 종료 코드가 0이 아니면 마지막 줄에 "종료 코드 N"과 stderr의 마지막 20줄을 덧붙인다.
   단, 비밀값(MOLIT_API_KEY 등)이 보이면 지우고 보고한다.
4. 코드 파일을 고치거나, 다른 브랜치에 커밋·푸시하거나, 스크립트를 다시 실행하지 않는다.
   네이버 차단(blocked)이 나와도 재시도하거나 우회하지 않는다.
```

## 3. scripts/routine_run.sh 가 하는 일

1. 원격(`origin`)에 `reports` 브랜치가 있는지 확인합니다.
   - 있으면 가져와 `.worktrees/reports`에 git worktree로 엽니다.
   - 없으면(첫 실행) 코드와 무관한 **orphan 브랜치**로 새로 만듭니다.
   - 원격 조회 자체가 실패하면(네트워크·권한) 새 브랜치를 만들지 않고 **종료 코드 3**으로 멈춥니다.
     이전 이력을 잃지 않기 위해서입니다.
2. `DB_PATH=.worktrees/reports/state/history.sqlite3`, `REPORT_DIR=.worktrees/reports/reports`로
   `python -m app.pipeline --once`를 실행합니다.
3. 드라이런이 아니면 `reports/YYYY-MM-DD.html`과 `state/history.sqlite3`를 커밋하고 `reports` 브랜치에 푸시합니다.
   커밋 메시지: `report: 2026-10-13 OK` (상태 포함). 상태가 FAILED여도 실행 기록 보존을 위해 푸시합니다.
4. `out/summary.md`를 표준출력으로 냅니다. 푸시가 실패하면 맨 위에 `[FAILED] reports 브랜치 푸시 실패` 줄을 붙입니다.

| 종료 코드 | 뜻 |
|---|---|
| 0 | OK — 모든 단지 수집 성공, 푸시 성공 |
| 1 | PARTIAL — 일부 단지 수집 실패(또는 국토부 키 없음), 푸시 성공 |
| 2 | FAILED — 전부 실패·설정 오류·요약 없음·파이프라인 비정상 종료 |
| 3 | 준비 실패 — 원격 조회·worktree 생성 실패. 파이프라인을 실행하지 않음 |
| 4 | reports 브랜치 커밋·푸시 실패 — 이번 이력이 보존되지 않음 |

옵션: `--dry-run` 또는 `DRY_RUN=true` → 이력 DB를 바꾸지 않고, 리포트·요약은 `out/`에만 쓰고, 푸시하지 않습니다.
환경변수 `REPORTS_REMOTE`, `REPORTS_BRANCH`, `REPORTS_WORKTREE`, `PYTHON`으로 기본값을 바꿀 수 있습니다.

## 4. reports 브랜치 보는 법

- GitHub 웹: 저장소 → 브랜치 선택에서 `reports` → `reports/` 폴더의 `YYYY-MM-DD.html`을 내려받아 브라우저로 엽니다.
  (GitHub 화면은 HTML을 소스로 보여 주므로 "Download raw file"로 받아서 엽니다.)
- 내 PC:
  ```bash
  git fetch origin reports
  git worktree add ../bargain-reports origin/reports   # 처음 한 번
  # 이후에는: cd ../bargain-reports && git pull  (또는 git checkout --detach origin/reports)
  ```
  `../bargain-reports/reports/` 의 HTML 파일을 브라우저로 엽니다.
- 커밋 기록: `git log origin/reports --oneline` → 주마다 `report: 날짜 상태` 한 줄.
- `state/history.sqlite3`는 재알림 이력 DB입니다. **직접 고치거나 지우지 마세요.** 지우면 다음 실행에서 모든 급매가 다시 "신규"로 나옵니다.

## 5. 동작 확인 (처음 설정한 뒤)

1. Routine을 수동으로 한 번 실행합니다 (Routine 화면의 "지금 실행").
2. 완료 알림 첫 줄이 `[급매 리포트] 날짜 — 상태 ...` 형태인지 봅니다.
3. 저장소에 `reports` 브랜치가 생겼고 `reports/날짜.html`, `state/history.sqlite3`가 있는지 확인합니다.
4. 다음 화요일 10:00 KST에 자동 실행되는지 확인합니다.

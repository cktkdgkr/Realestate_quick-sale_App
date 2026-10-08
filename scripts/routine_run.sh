#!/usr/bin/env bash
# 주간 Routine 실행 스크립트 (CLAUDE.md §1.1, §9). 사용법은 docs/ROUTINE.md.
#
#   scripts/routine_run.sh [--dry-run]
#
# 1) origin의 orphan 브랜치 `reports`를 git worktree로 연다 (원격에 없으면 새 orphan 브랜치를 만든다).
# 2) <worktree>/state/history.sqlite3 를 DB_PATH로, <worktree>/reports 를 REPORT_DIR로 연결해
#    `python -m app.pipeline --once` 를 실행한다.
# 3) (드라이런이 아니면) 리포트와 DB를 reports 브랜치에 커밋·푸시한다.
# 4) out/summary.md 를 표준출력으로 낸다.
#
# 종료 코드
#   0 OK / 1 PARTIAL / 2 FAILED (파이프라인 종료 코드 그대로, 푸시까지 성공한 경우)
#   3 준비 실패 (git 원격 조회·worktree 생성 실패 등)  — 파이프라인을 실행하지 않음
#   4 reports 브랜치 커밋·푸시 실패 — 파이프라인 결과와 무관하게 실패로 본다
#
# 환경변수 (모두 선택)
#   REPORTS_REMOTE   기본 origin
#   REPORTS_BRANCH   기본 reports
#   REPORTS_WORKTREE 기본 <저장소>/.worktrees/reports
#   PYTHON           기본 <저장소>/.venv/bin/python 이 있으면 그것, 없으면 python3
#   PIPELINE_CMD     파이프라인 명령 대체 (테스트용). 기본 "$PYTHON -m app.pipeline"
#   DRY_RUN=true     --dry-run 과 같음
#
# 비밀값(MOLIT_API_KEY 등)은 출력하지 않는다. git 출력의 URL 자격 증명은 가린다.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${REPORTS_REMOTE:-origin}"
BRANCH="${REPORTS_BRANCH:-reports}"
WT="${REPORTS_WORKTREE:-$REPO_ROOT/.worktrees/reports}"
OUT_DIR_ABS="${OUT_DIR:-$REPO_ROOT/out}"
case "$OUT_DIR_ABS" in /*) ;; *) OUT_DIR_ABS="$REPO_ROOT/$OUT_DIR_ABS" ;; esac

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "알 수 없는 인자: $arg" >&2; exit 3 ;;
  esac
done
case "$(printf '%s' "${DRY_RUN:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|y|on) DRY=1 ;;
esac

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then PYTHON="$REPO_ROOT/.venv/bin/python"; else PYTHON="python3"; fi
fi
PIPELINE_CMD="${PIPELINE_CMD:-$PYTHON -m app.pipeline}"

log() { printf '[routine] %s\n' "$*" >&2; }

# URL 안의 자격 증명(https://user:token@host)과 토큰 형태를 가린다.
redact() { sed -E -e 's#(://)[^/@[:space:]]+@#\1***@#g' -e 's#(serviceKey=)[^&[:space:]]+#\1***#g'; }

# git 명령 실행. 출력은 가려서 stderr로. 종료 코드는 그대로 돌려준다.
g() {
  local rc=0 out
  out="$(git "$@" 2>&1)" || rc=$?
  [[ -n "$out" ]] && printf '%s\n' "$out" | redact >&2
  return $rc
}

die() { log "실패: $1"; exit "$2"; }

cd "$REPO_ROOT"

# ---------------------------------------------------------------- 1) reports worktree
# 이전 실행이 남긴 worktree는 정리하고 새로 연다 (원격이 기준).
if [[ -e "$WT" ]]; then
  g worktree remove --force "$WT" || rm -rf "$WT"
fi
g worktree prune || true

set +e
git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>"$REPO_ROOT/.routine-lsremote.err"
LS_RC=$?
set -e
if [[ $LS_RC -ne 0 && $LS_RC -ne 2 ]]; then
  redact <"$REPO_ROOT/.routine-lsremote.err" >&2 || true
  rm -f "$REPO_ROOT/.routine-lsremote.err"
  # 원격 조회 실패를 "브랜치 없음"으로 오인해 새 orphan을 만들면 이력이 끊긴다. 여기서 멈춘다.
  die "원격 '$REMOTE' 조회 실패 (종료 코드 $LS_RC). 네트워크·권한을 확인하세요." 3
fi
rm -f "$REPO_ROOT/.routine-lsremote.err"

mkdir -p "$(dirname "$WT")"
if [[ $LS_RC -eq 0 ]]; then
  log "원격 $REMOTE/$BRANCH 를 가져옵니다."
  g fetch --no-tags "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH" \
    || die "git fetch 실패" 3
  g worktree add --force -B "$BRANCH" "$WT" "refs/remotes/$REMOTE/$BRANCH" \
    || die "worktree 생성 실패" 3
else
  log "원격에 $BRANCH 브랜치가 없어 새 orphan 브랜치를 만듭니다 (첫 실행)."
  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    # 로컬에만 남은 같은 이름의 브랜치는 원격과 무관하므로 새로 만든다
    g branch -D "$BRANCH" || die "로컬 $BRANCH 브랜치 정리 실패" 3
  fi
  g worktree add --detach "$WT" HEAD || die "worktree 생성 실패" 3
  (
    cd "$WT"
    g checkout --orphan "$BRANCH"
    g rm -rf --quiet --cached . >/dev/null
    find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  ) || die "orphan 브랜치 준비 실패" 3
fi
mkdir -p "$WT/state" "$WT/reports"

# ---------------------------------------------------------------- 2) 파이프라인
export DB_PATH="$WT/state/history.sqlite3"
export REPORT_DIR="$WT/reports"
export OUT_DIR="$OUT_DIR_ABS"
rm -f "$OUT_DIR/summary.md"  # 지난 실행의 요약을 이번 결과로 오인하지 않게

PIPE_ARGS=(--once)
[[ $DRY -eq 1 ]] && PIPE_ARGS+=(--dry-run)
log "파이프라인 실행 (드라이런=$DRY)"
set +e
# shellcheck disable=SC2086
$PIPELINE_CMD "${PIPE_ARGS[@]}"
PIPE_RC=$?
set -e
log "파이프라인 종료 코드: $PIPE_RC"

case $PIPE_RC in
  0) STATUS=OK ;;
  1) STATUS=PARTIAL ;;
  2) STATUS=FAILED ;;
  *) STATUS=CRASHED ;;
esac

# ---------------------------------------------------------------- 3) 커밋·푸시
PUSH_RC=0
if [[ $DRY -eq 1 ]]; then
  log "드라이런: reports 브랜치에 커밋·푸시하지 않습니다."
elif [[ "$STATUS" == "CRASHED" ]]; then
  log "파이프라인이 비정상 종료해 결과를 올리지 않습니다."
else
  (
    cd "$WT"
    to_add=()
    [[ -f state/history.sqlite3 ]] && to_add+=(state/history.sqlite3)
    [[ -d reports ]] && to_add+=(reports)
    if [[ ${#to_add[@]} -gt 0 ]]; then g add -f -- "${to_add[@]}"; fi
    if git diff --cached --quiet; then
      log "커밋할 변경이 없습니다."
    else
      ident=()
      git config user.name >/dev/null 2>&1 || ident+=(-c "user.name=naver-bargain-alert routine")
      git config user.email >/dev/null 2>&1 || ident+=(-c "user.email=routine@localhost")
      g "${ident[@]}" commit --quiet -m "report: $(TZ=Asia/Seoul date +%Y-%m-%d) $STATUS"
    fi
    # 원격에 브랜치가 없거나 로컬이 앞서 있으면 푸시
    g push "$REMOTE" "HEAD:refs/heads/$BRANCH"
  ) || PUSH_RC=$?
  if [[ $PUSH_RC -ne 0 ]]; then
    log "실패: reports 브랜치 커밋·푸시 실패 (종료 코드 $PUSH_RC). 이번 이력 DB가 보존되지 않았습니다. docs/RUNBOOK.md 참고."
  else
    log "reports 브랜치에 푸시했습니다."
  fi
fi

# ---------------------------------------------------------------- 4) 요약 출력
if [[ -f "$OUT_DIR/summary.md" ]]; then
  if [[ $PUSH_RC -ne 0 ]]; then
    echo "[FAILED] reports 브랜치 푸시 실패 — 이번 결과·이력이 저장소에 보존되지 않았습니다 (docs/RUNBOOK.md)"
    echo
  fi
  cat "$OUT_DIR/summary.md"
else
  echo "[FAILED] 실행 요약(out/summary.md)이 만들어지지 않았습니다. 파이프라인 종료 코드 $PIPE_RC. 로그를 확인하세요 (docs/RUNBOOK.md)."
fi

if [[ $PUSH_RC -ne 0 ]]; then exit 4; fi
if [[ "$STATUS" == "CRASHED" || ! -f "$OUT_DIR/summary.md" ]]; then exit 2; fi
exit "$PIPE_RC"

#!/usr/bin/env bash
# 검증 agent 반례: scripts/routine_run.sh 를 임시 저장소 + 로컬 bare 원격으로 실행 (실제 원격 푸시 없음).
# 실행: bash tests/verifier/verify_stage2_routine.sh
set -u
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
T="$(mktemp -d)"; PASS=0; FAIL=0
ok(){ if eval "$2"; then echo "PASS $1"; PASS=$((PASS+1)); else echo "FAIL $1"; FAIL=$((FAIL+1)); fi; }
SECRET="VERIF-ROUTINE-SECRET-123abc"
export MOLIT_API_KEY="$SECRET" GIT_AUTHOR_NAME=v GIT_AUTHOR_EMAIL=v@localhost GIT_COMMITTER_NAME=v GIT_COMMITTER_EMAIL=v@localhost
git init -q --bare "$T/remote.git"
git init -q -b main "$T/repo"; mkdir -p "$T/repo/scripts"
cp "$SRC/scripts/routine_run.sh" "$T/repo/scripts/"; cp "$SRC/.gitignore" "$T/repo/"
echo code > "$T/repo/code.txt"
( cd "$T/repo" && git add -A && git commit -qm init && git remote add origin "$T/remote.git" && git push -q origin main )
cat > "$T/fake.py" <<'PY'
import os, sqlite3, sys, pathlib
db = pathlib.Path(os.environ["DB_PATH"]); db.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(db); con.execute("create table if not exists runs(n)"); con.execute("insert into runs values(1)"); con.commit()
n = con.execute("select count(*) from runs").fetchone()[0]
if "--dry-run" not in sys.argv:
    rd = pathlib.Path(os.environ["REPORT_DIR"]); rd.mkdir(parents=True, exist_ok=True); (rd / f"r{n}.html").write_text("x")
out = pathlib.Path(os.environ["OUT_DIR"]); out.mkdir(parents=True, exist_ok=True)
(out / "summary.md").write_text(f"SUMMARY runs={n}\n")
print("stderr noise", file=sys.stderr)
sys.exit(int(os.environ.get("RC", "0")))
PY
export PIPELINE_CMD="python3 $T/fake.py"
R="$T/repo/scripts/routine_run.sh"
# 1) 첫 실행: orphan 생성
out="$(bash "$R" 2>"$T/err1")"; rc=$?
ok "첫 실행 rc=0" "[[ $rc -eq 0 ]]"
ok "첫 실행 summary stdout" "[[ \"\$out\" == *'SUMMARY runs=1'* ]]"
ok "원격 reports 브랜치 생성" "git --git-dir=$T/remote.git rev-parse -q --verify refs/heads/reports >/dev/null"
ok "reports는 orphan(main과 공통 조상 없음)" "! git --git-dir=$T/remote.git merge-base main reports >/dev/null 2>&1"
ok "reports에 code.txt 없음" "! git --git-dir=$T/remote.git cat-file -e reports:code.txt 2>/dev/null"
ok "reports에 DB·리포트" "git --git-dir=$T/remote.git cat-file -e reports:state/history.sqlite3 && git --git-dir=$T/remote.git cat-file -e reports:reports/r1.html"
ok "코드 브랜치 작업트리 깨끗" "[[ -z \$(cd $T/repo && git status --porcelain) ]]"
# 2) 두번째 실행: 기존 브랜치 재사용, DB 이어받음
out="$(bash "$R" 2>"$T/err2")"; rc=$?
ok "둘째 실행 DB 이어받음(runs=2)" "[[ \"\$out\" == *'runs=2'* && $rc -eq 0 ]]"
ok "reports 커밋 2개" "[[ \$(git --git-dir=$T/remote.git rev-list --count reports) -eq 2 ]]"
# 3) PARTIAL 종료코드 전달
out="$(RC=1 bash "$R" 2>/dev/null)"; rc=$?
ok "PARTIAL rc=1 + 푸시" "[[ $rc -eq 1 && \$(git --git-dir=$T/remote.git rev-list --count reports) -eq 3 ]]"
# 4) 드라이런: 푸시 없음
out="$(bash "$R" --dry-run 2>/dev/null)"; rc=$?
ok "드라이런 푸시 없음" "[[ \$(git --git-dir=$T/remote.git rev-list --count reports) -eq 3 && $rc -eq 0 ]]"
# 5) 푸시 실패: pre-receive 훅으로 거부
printf '#!/bin/sh\nexit 1\n' > "$T/remote.git/hooks/pre-receive"; chmod +x "$T/remote.git/hooks/pre-receive"
out="$(bash "$R" 2>"$T/err5")"; rc=$?
ok "푸시 실패 rc!=0 (rc=$rc)" "[[ $rc -ne 0 ]]"
ok "푸시 실패 stdout 첫 줄 FAILED" "[[ \"\$(printf '%s' \"\$out\" | head -1)\" == *FAILED* ]]"
rm "$T/remote.git/hooks/pre-receive"
# 6) 비밀값 미출력
ok "비밀값 stdout/stderr 미출력" "! grep -q '$SECRET' $T/err1 $T/err2 $T/err5 && [[ \"\$out\" != *'$SECRET'* ]]"
# 7) 자격증명 URL 원격 + 조회 실패 -> 3, 자격증명 가림, 새 orphan 안 만듦
( cd "$T/repo" && git remote set-url origin "https://user:TOKSECRET999@127.0.0.1:9/x.git" )
out="$(GIT_TERMINAL_PROMPT=0 bash "$R" 2>"$T/err7")"; rc=$?
ok "원격 조회 실패 rc=3" "[[ $rc -eq 3 ]]"
ok "원격 자격증명 가림" "! grep -q TOKSECRET999 $T/err7 && [[ \"\$out\" != *TOKSECRET999* ]]"
# 8) 첫 실행 + 푸시 실패 (빈 원격, 훅 거부)
git init -q --bare "$T/remote2.git"; printf '#!/bin/sh\nexit 1\n' > "$T/remote2.git/hooks/pre-receive"; chmod +x "$T/remote2.git/hooks/pre-receive"
( cd "$T/repo" && git remote set-url origin "$T/remote2.git" )
out="$(bash "$R" 2>/dev/null)"; rc=$?
ok "첫 실행 푸시 실패 rc=4" "[[ $rc -eq 4 ]]"
echo "$PASS PASS / $FAIL FAIL  (tmp $T)"
[[ $FAIL -eq 0 ]]

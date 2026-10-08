"""scripts/routine_run.sh 테스트. 원격은 로컬 임시 bare 저장소 (실제 원격 푸시 없음).

파이프라인은 PIPELINE_CMD로 바꿔 끼운다.
- fake: DB·리포트·요약 파일만 쓰는 작은 스크립트 (종료 코드 지정 가능)
- real: 실제 app.pipeline.main을 수집·판정·알림 fake와 함께 실행 (worktree DB 경로 연결 확인)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "routine_run.sh"
SECRET = "SECRET-ROUTINE-KEY-0a1b2c"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="git/bash 필요")

FAKE_PIPELINE = textwrap.dedent(
    """
    import os, sqlite3, sys, pathlib
    db = pathlib.Path(os.environ["DB_PATH"]); db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db); con.execute("create table if not exists runs(n integer)")
    con.execute("insert into runs values (1)"); con.commit()
    n = con.execute("select count(*) from runs").fetchone()[0]; con.close()
    dry = "--dry-run" in sys.argv
    if not dry:
        rd = pathlib.Path(os.environ["REPORT_DIR"]); rd.mkdir(parents=True, exist_ok=True)
        (rd / f"run{n}.html").write_text("<html>ok</html>")
    out = pathlib.Path(os.environ["OUT_DIR"]); out.mkdir(parents=True, exist_ok=True)
    rc = int(os.environ.get("FAKE_RC", "0"))
    if os.environ.get("FAKE_NO_SUMMARY") != "1":
        (out / "summary.md").write_text(f"[FAKE rc={rc}] runs={n} dry={dry}\\n")
    print("fake pipeline argv", sys.argv[1:], file=sys.stderr)
    sys.exit(rc)
    """
)

REAL_PIPELINE_DRIVER = textwrap.dedent(
    """
    import sys
    from app import pipeline
    from app.collectors import molit_trades, naver_listings, naver_trades
    from app.domain import dedup, rules
    from app.domain.models import AreaType, Complex, Listing, Verdict
    from app.notify import history, report, summary

    class C:
        def __init__(self, *a, **k): pass

    def fetch_complex(c, no):
        return Complex(no, "단지" + no, "11650", "addr", 20, None), [AreaType(no, 84.97, 84.97, 112.0, 34, "A")]

    def fetch_listings(c, cx, ats):
        return [Listing("1", cx.complex_no, 84.97, "101동", "5/20", "NORMAL", "남", 90000, None, 1, [],
                        cx.complex_no + "|k", "https://example.invalid")]

    def classify(session, verdicts, failed, run_id, dry_run):
        from sqlalchemy import text
        prev = session.execute(text("select count(*) from runs")).scalar()
        for v in verdicts:
            v.alert_kind = "NEW"
        return verdicts, [{"prev_runs": prev}]

    def render_summary(run, ctx):
        return f"[{run.status}] prev_runs={ctx['gone'][0]['prev_runs']} dry={ctx['dry_run']}\\n"

    for mod, name, fn in [
        (naver_listings, "NaverClient", C), (molit_trades, "MolitClient", C),
        (naver_listings, "fetch_complex", fetch_complex), (naver_listings, "fetch_listings", fetch_listings),
        (molit_trades, "fetch_trades", lambda *a: []),
        (molit_trades, "find_apt_seq_candidates", lambda *a: []),
        (naver_trades, "fetch_naver_trades", lambda *a: []), (naver_trades, "cross_check", lambda *a: []),
        (dedup, "dedup", list),
        (rules, "judge", lambda ls, ts, d: [Verdict(l, True, ["LISTING"], None, 95000, 5.3, False, None) for l in ls]),
        (rules, "area_summary", lambda *a: {}),
        (history, "classify_alerts", classify), (history, "commit_history", lambda s, *a, **k: s.commit()),
        (report, "render_report", lambda run, ctx: "<html>" + run.status + "</html>"),
        (summary, "render_summary", render_summary),
    ]:
        setattr(mod, name, fn)
    sys.exit(pipeline.main(sys.argv[1:]))
    """
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
                          env=_git_env())


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


@pytest.fixture
def repos(tmp_path: Path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(bare))
    code = tmp_path / "code"
    (code / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, code / "scripts" / "routine_run.sh")
    (code / ".gitignore").write_text("/.worktrees/\nout/\n")
    (code / "README").write_text("code branch\n")
    _git(code, "init", "-q", "-b", "main")
    _git(code, "add", ".")
    _git(code, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "code")
    _git(code, "remote", "add", "origin", str(bare))
    _git(code, "push", "-q", "origin", "main")
    (tmp_path / "fake_pipeline.py").write_text(FAKE_PIPELINE)
    (tmp_path / "real_driver.py").write_text(REAL_PIPELINE_DRIVER)
    cfile = tmp_path / "complexes.yaml"
    cfile.write_text("complexes:\n  - '111'\n")
    return tmp_path, bare, code


def _run(code: Path, tmp: Path, *args: str, pipeline: str = "fake", **extra: str) -> subprocess.CompletedProcess:
    env = _git_env()
    for k in ("DB_PATH", "REPORT_DIR", "OUT_DIR", "DRY_RUN", "PYTHON"):
        env.pop(k, None)
    driver = tmp / ("fake_pipeline.py" if pipeline == "fake" else "real_driver.py")
    env.update(PIPELINE_CMD=f"{sys.executable} {driver}", MOLIT_API_KEY=SECRET,
               COMPLEXES_FILE=str(tmp / "complexes.yaml"), PYTHONPATH=str(PROJECT_ROOT))
    env.update(extra)
    return subprocess.run(["bash", str(code / "scripts" / "routine_run.sh"), *args], cwd=tmp,
                          capture_output=True, text=True, env=env, timeout=120)


def _ls_branch(bare: Path, branch: str = "reports") -> list[str]:
    return _git(bare, "ls-tree", "-r", "--name-only", branch).stdout.split()


def _db_runs(bare: Path, tmp: Path) -> int:
    blob = subprocess.run(["git", "show", "reports:state/history.sqlite3"], cwd=bare, capture_output=True,
                          check=True, env=_git_env()).stdout
    p = tmp / "check.sqlite3"
    p.write_bytes(blob)
    con = sqlite3.connect(p)
    try:
        return con.execute("select count(*) from runs").fetchone()[0]
    finally:
        con.close()


def test_first_run_creates_orphan_branch_and_pushes(repos) -> None:
    tmp, bare, code = repos
    r = _run(code, tmp)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("[FAKE rc=0] runs=1 dry=False")
    assert set(_ls_branch(bare)) == {"state/history.sqlite3", "reports/run1.html"}
    # orphan: main과 공통 조상이 없고 커밋 1개
    assert _git(bare, "merge-base", "main", "reports", check=False).returncode != 0
    assert _git(bare, "rev-list", "--count", "reports").stdout.strip() == "1"
    # 코드 브랜치는 그대로
    assert _git(code, "status", "--porcelain").stdout.strip() == ""


def test_second_run_reuses_db_from_reports_branch(repos) -> None:
    tmp, bare, code = repos
    assert _run(code, tmp).returncode == 0
    r = _run(code, tmp)
    assert r.returncode == 0, r.stderr
    assert "runs=2" in r.stdout  # 첫 실행 DB를 이어서 씀
    assert _git(bare, "rev-list", "--count", "reports").stdout.strip() == "2"
    assert _db_runs(bare, tmp) == 2
    assert {"reports/run1.html", "reports/run2.html"} <= set(_ls_branch(bare))


def test_fresh_clone_continues_history(repos) -> None:
    """Routine처럼 매번 새로 clone한 세션에서도 원격 reports 브랜치의 DB를 이어 쓴다."""
    tmp, bare, code = repos
    assert _run(code, tmp).returncode == 0
    clone = tmp / "clone2"
    _git(tmp, "clone", "-q", "--single-branch", "-b", "main", str(bare), str(clone))
    r = _run(clone, tmp)
    assert r.returncode == 0, r.stderr
    assert "runs=2" in r.stdout


@pytest.mark.parametrize("rc", [1, 2])
def test_partial_and_failed_are_pushed_and_exit_nonzero(repos, rc: int) -> None:
    tmp, bare, code = repos
    r = _run(code, tmp, FAKE_RC=str(rc))
    assert r.returncode == rc
    assert f"[FAKE rc={rc}]" in r.stdout
    assert "state/history.sqlite3" in _ls_branch(bare)  # 실패 기록도 보존
    assert _git(bare, "log", "-1", "--format=%s", "reports").stdout.strip().endswith(
        {1: "PARTIAL", 2: "FAILED"}[rc])


def test_dry_run_does_not_push(repos) -> None:
    tmp, bare, code = repos
    r = _run(code, tmp, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "dry=True" in r.stdout
    assert _git(bare, "show-ref", "--verify", "refs/heads/reports", check=False).returncode != 0


def test_dry_run_env(repos) -> None:
    tmp, bare, code = repos
    assert _run(code, tmp).returncode == 0
    r = _run(code, tmp, DRY_RUN="true")
    assert r.returncode == 0 and "dry=True" in r.stdout
    assert _git(bare, "rev-list", "--count", "reports").stdout.strip() == "1"


def test_push_failure_is_failure(repos) -> None:
    tmp, bare, code = repos
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected by test >&2\nexit 1\n")
    hook.chmod(0o755)
    r = _run(code, tmp)
    assert r.returncode == 4
    assert "푸시 실패" in r.stdout and "푸시 실패" in r.stderr
    assert _git(bare, "show-ref", "--verify", "refs/heads/reports", check=False).returncode != 0


def test_unreachable_remote_does_not_create_new_orphan(repos) -> None:
    tmp, bare, code = repos
    _git(code, "remote", "set-url", "origin", str(tmp / "does-not-exist.git"))
    r = _run(code, tmp)
    assert r.returncode == 3
    assert "fake pipeline" not in r.stderr  # 파이프라인을 실행하지 않음
    assert _git(code, "show-ref", "--verify", "refs/heads/reports", check=False).returncode != 0


def test_missing_summary_is_failure(repos) -> None:
    tmp, bare, code = repos
    r = _run(code, tmp, FAKE_NO_SUMMARY="1")
    assert r.returncode == 2
    assert r.stdout.startswith("[FAILED]")


def test_crash_exit_code_is_not_pushed(repos) -> None:
    tmp, bare, code = repos
    r = _run(code, tmp, FAKE_RC="7")
    assert r.returncode == 2
    assert _git(bare, "show-ref", "--verify", "refs/heads/reports", check=False).returncode != 0


def test_secrets_not_printed(repos) -> None:
    tmp, bare, code = repos
    # 자격 증명이 든 URL도 출력에서 가려진다
    _git(code, "remote", "set-url", "origin", f"file://user:{SECRET}@/nonexistent/x.git")
    r = _run(code, tmp)
    for stream in (r.stdout, r.stderr):
        assert SECRET not in stream
    r2 = _run(code, tmp, REPORTS_REMOTE="origin")
    assert SECRET not in r2.stdout + r2.stderr


def test_real_pipeline_with_worktree_db(repos) -> None:
    tmp, bare, code = repos
    r1 = _run(code, tmp, pipeline="real")
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.startswith("[OK] prev_runs=1 dry=False")  # 이번 실행의 RUNNING 행 1개
    files = set(_ls_branch(bare))
    assert "state/history.sqlite3" in files
    assert any(f.startswith("reports/") and f.endswith(".html") for f in files)
    r2 = _run(code, tmp, pipeline="real")
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.startswith("[OK] prev_runs=2")  # reports 브랜치의 DB를 읽음
    assert _db_runs(bare, tmp) == 2
    for r in (r1, r2):
        assert SECRET not in r.stdout + r.stderr

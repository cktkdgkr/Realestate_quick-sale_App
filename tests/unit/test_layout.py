"""CLAUDE.md §6 디렉터리 구조 확인 (검증 C1-1)."""

import subprocess
import sys

import pytest

from app.config import PROJECT_ROOT

REQUIRED = [
    "app/collectors/naver_listings.py",
    "app/collectors/naver_trades.py",
    "app/collectors/molit_trades.py",
    "app/domain/normalize.py",
    "app/domain/dedup.py",
    "app/domain/rules.py",
    "app/domain/models.py",
    "app/notify/report.py",
    "app/notify/summary.py",
    "app/notify/history.py",
    "app/notify/templates",
    "app/config.py",
    "app/db",
    "app/pipeline.py",
    "config/complexes.yaml",
    "tests/fixtures",
    "tests/unit",
    "tests/integration",
    ".env.example",
    "docs/RUNBOOK.md",
    "docs/USER_GUIDE.md",
    "docs/ROUTINE.md",
]


@pytest.mark.parametrize("rel", REQUIRED)
def test_path_exists(rel: str) -> None:
    assert (PROJECT_ROOT / rel).exists(), rel


@pytest.mark.parametrize("rel", ["app/web", "app/scheduler.py", "deploy", "app/notify/telegram.py", "app/notify/email.py"])
def test_removed_components_absent(rel: str) -> None:
    assert not (PROJECT_ROOT / rel).exists(), rel


def test_pipeline_cli_requires_once() -> None:
    # --once 없이 실행하면 아무것도 하지 않고 실패(2)한다. 실제 수집은 하지 않는다 (외부 접속 금지).
    r = subprocess.run([sys.executable, "-m", "app.pipeline"], cwd=PROJECT_ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "--once" in r.stderr

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import (
    PROJECT_ROOT,
    ComplexEntry,
    ConfigError,
    load_complexes,
    load_settings,
    parse_complex_no,
)


def test_defaults_without_env(tmp_path: Path) -> None:
    s = load_settings(env_file=tmp_path / "none.env", environ={}, root=tmp_path)
    assert s.molit_api_key is None
    assert s.dry_run is False
    assert s.tz == "Asia/Seoul"
    assert s.db_path == tmp_path / "state" / "history.sqlite3"
    assert s.report_dir == tmp_path / "reports"
    assert s.out_dir == tmp_path / "out"
    assert s.complexes_file == tmp_path / "config" / "complexes.yaml"
    with pytest.raises(ConfigError):
        s.require_molit_api_key()


def test_env_file_and_environ_precedence(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("MOLIT_API_KEY=from-file\nDRY_RUN=true\nDB_PATH=x/y.sqlite3\n", encoding="utf-8")
    s = load_settings(env_file=env, environ={"DRY_RUN": "0"}, root=tmp_path)
    assert s.require_molit_api_key() == "from-file"
    assert s.dry_run is False  # 환경변수가 .env보다 우선
    assert s.db_path == tmp_path / "x" / "y.sqlite3"


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Mars/Base", ""])
def test_business_timezone_fixed_regardless_of_os_tz(tmp_path: Path, tz: str) -> None:
    env = tmp_path / ".env"
    env.write_text(f"TZ={tz}\n", encoding="utf-8")
    s = load_settings(env_file=env, environ={"TZ": tz}, root=tmp_path)
    assert s.tz == "Asia/Seoul"
    assert s.zoneinfo == ZoneInfo("Asia/Seoul")
    # 08:00 KST (= 전날 23:00 UTC)에도 기준일은 KST 날짜
    morning = datetime(2026, 10, 12, 23, 0, tzinfo=UTC)
    assert morning.astimezone(s.zoneinfo).date() == date(2026, 10, 13)


def test_secret_not_in_repr(tmp_path: Path) -> None:
    s = load_settings(env_file=tmp_path / "none", environ={"MOLIT_API_KEY": "SECRET-VALUE-123"}, root=tmp_path)
    assert "SECRET-VALUE-123" not in repr(s)


@pytest.mark.parametrize("bad", [{"DRY_RUN": "maybe"}])
def test_invalid_values_raise(tmp_path: Path, bad: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env_file=tmp_path / "none", environ=bad, root=tmp_path)


def test_env_example_lists_required_keys_without_values() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip()
    assert {"MOLIT_API_KEY", "DRY_RUN", "DB_PATH", "REPORT_DIR", "OUT_DIR", "COMPLEXES_FILE"} <= set(keys)
    assert keys["MOLIT_API_KEY"] == ""
    assert "TZ" not in keys  # 업무 시간대는 코드 상수 (CLAUDE.md §9)
    for banned in ("TELEGRAM", "SMTP", "EMAIL", "WEB_"):
        assert not any(k.startswith(banned) for k in keys)


def test_gitignore_excludes_env() -> None:
    lines = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [ln.strip() for ln in lines]


def test_repo_complexes_yaml() -> None:
    entries = load_complexes(PROJECT_ROOT / "config" / "complexes.yaml")
    assert [e.complex_no for e in entries] == ["3009", "22627"]
    assert entries[0].name == "잠원동아"
    assert entries[1].name == "잠실엘스"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3009", "3009"),
        (22627, "22627"),
        ("https://new.land.naver.com/complexes/3009?ms=37.5,127.0,17&a=APT", "3009"),
        ("https://fin.land.naver.com/complexes/22627", "22627"),
        ("https://m.land.naver.com/complex/info/3009", "3009"),
    ],
)
def test_parse_complex_no(raw: str | int, expected: str) -> None:
    assert parse_complex_no(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "https://new.land.naver.com/", "https://new.land.naver.com/complexes/30x9"])
def test_parse_complex_no_invalid(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_complex_no(raw)


def test_load_complexes_mixed_forms(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "complexes:\n"
        "  - 3009\n"
        "  - url: https://new.land.naver.com/complexes/22627\n"
        "    molit_apt_seq: 11710-1234\n",
        encoding="utf-8",
    )
    assert load_complexes(p) == [
        ComplexEntry("3009"),
        ComplexEntry("22627", None, "11710-1234"),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "complexes: []\n",
        "other: 1\n",
        "complexes:\n  - 3009\n  - '3009'\n",
        "complexes:\n  - name: 이름만\n",
        "complexes:\n  - complex_no: 3009\n    typo_key: 1\n",
        "complexes:\n  - complex_no: 3009\n    url: https://new.land.naver.com/complexes/22627\n",
        "complexes: [\n",
    ],
)
def test_load_complexes_invalid(tmp_path: Path, content: str) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_complexes(p)


def test_load_complexes_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_complexes(tmp_path / "nope.yaml")

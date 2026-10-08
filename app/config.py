"""설정 로드: `.env`(환경변수)와 `config/complexes.yaml`.

- 우선순위: 실제 환경변수 > `.env` 파일 > 기본값.
- 상대 경로는 저장소 루트(PROJECT_ROOT) 기준으로 해석한다. Routine 세션과 사용자 PC에서 같은 결과를 내기 위해서다.
- 비밀값(MOLIT_API_KEY)은 repr·로그에 나오지 않게 한다 (CLAUDE.md §8).
- 잘못된 설정은 조용히 건너뛰지 않고 ConfigError를 던진다 (CLAUDE.md §7: 실패를 빈 결과로 숨기지 않음).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB_PATH = "state/history.sqlite3"
DEFAULT_REPORT_DIR = "reports"
DEFAULT_OUT_DIR = "out"
DEFAULT_COMPLEXES_FILE = "config/complexes.yaml"
DEFAULT_TZ = "Asia/Seoul"

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}

# new.land.naver.com/complexes/3009, fin.land.naver.com/complexes/3009, m.land.naver.com/complex/info/3009
_COMPLEX_URL_RE = re.compile(r"/complex(?:es)?/(?:info/)?(\d+)(?:[/?#]|$)")
_COMPLEX_NO_RE = re.compile(r"^\d+$")


class ConfigError(ValueError):
    """설정값이 없거나 형식이 틀렸을 때."""


@dataclass(frozen=True)
class Settings:
    molit_api_key: str | None = field(repr=False)
    dry_run: bool
    tz: str
    db_path: Path
    report_dir: Path
    out_dir: Path
    complexes_file: Path

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def require_molit_api_key(self) -> str:
        if not self.molit_api_key:
            raise ConfigError("MOLIT_API_KEY가 설정되지 않았습니다 (.env 또는 환경변수).")
        return self.molit_api_key


@dataclass(frozen=True)
class ComplexEntry:
    """config/complexes.yaml의 한 줄. 단지 메타데이터(lawd_cd 등)는 수집기가 채운다."""

    complex_no: str
    name: str | None = None
    molit_apt_seq: str | None = None


def _resolve_path(value: str, root: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


def _parse_bool(key: str, value: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{key} 값은 true/false 여야 합니다: {value!r}")


def load_settings(
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> Settings:
    """설정을 읽는다.

    env_file: 기본값은 `<root>/.env`. 파일이 없으면 무시한다(환경변수만 사용).
    environ:  기본값은 os.environ. 테스트에서 주입용.
    """
    root = root or PROJECT_ROOT
    env_path = Path(env_file) if env_file is not None else root / ".env"
    merged: dict[str, str] = {}
    if env_path.is_file():
        merged.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
    merged.update(os.environ if environ is None else environ)

    def get(key: str, default: str) -> str:
        v = merged.get(key)
        return default if v is None or v.strip() == "" else v.strip()

    tz = get("TZ", DEFAULT_TZ)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"TZ 값이 올바른 시간대가 아닙니다: {tz!r}") from e

    api_key = merged.get("MOLIT_API_KEY")
    return Settings(
        molit_api_key=api_key.strip() if api_key and api_key.strip() else None,
        dry_run=_parse_bool("DRY_RUN", merged.get("DRY_RUN", "false")),
        tz=tz,
        db_path=_resolve_path(get("DB_PATH", DEFAULT_DB_PATH), root),
        report_dir=_resolve_path(get("REPORT_DIR", DEFAULT_REPORT_DIR), root),
        out_dir=_resolve_path(get("OUT_DIR", DEFAULT_OUT_DIR), root),
        complexes_file=_resolve_path(get("COMPLEXES_FILE", DEFAULT_COMPLEXES_FILE), root),
    )


def parse_complex_no(value: str | int) -> str:
    """complex_no 숫자 또는 네이버 부동산 단지 URL에서 complex_no를 꺼낸다."""
    s = str(value).strip()
    if _COMPLEX_NO_RE.match(s):
        return s
    m = _COMPLEX_URL_RE.search(s)
    if m:
        return m.group(1)
    raise ConfigError(f"단지 URL/번호를 해석할 수 없습니다: {s!r}")


def load_complexes(path: Path | str) -> list[ComplexEntry]:
    """complexes.yaml을 읽어 단지 목록을 돌려준다. 순서를 유지하고 중복은 오류."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"단지 목록 파일이 없습니다: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"단지 목록 YAML 형식 오류: {path}: {e}") from e

    if not isinstance(data, dict) or "complexes" not in data:
        raise ConfigError(f"{path}: 최상위에 'complexes:' 목록이 있어야 합니다.")
    items = data["complexes"]
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path}: 'complexes'는 비어 있지 않은 목록이어야 합니다.")

    result: list[ComplexEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        if isinstance(item, (str, int)):
            entry = ComplexEntry(complex_no=parse_complex_no(item))
        elif isinstance(item, dict):
            raw = item.get("complex_no", item.get("url"))
            if raw is None:
                raise ConfigError(f"{path}: {i}번째 항목에 complex_no 또는 url이 없습니다.")
            unknown = set(item) - {"complex_no", "url", "name", "molit_apt_seq"}
            if unknown:
                raise ConfigError(f"{path}: {i}번째 항목에 알 수 없는 키: {sorted(unknown)}")
            if "complex_no" in item and "url" in item:
                if parse_complex_no(item["complex_no"]) != parse_complex_no(item["url"]):
                    raise ConfigError(f"{path}: {i}번째 항목의 complex_no와 url이 서로 다릅니다.")
            seq = item.get("molit_apt_seq")
            name = item.get("name")
            entry = ComplexEntry(
                complex_no=parse_complex_no(raw),
                name=str(name) if name is not None else None,
                molit_apt_seq=str(seq).strip() if seq not in (None, "") else None,
            )
        else:
            raise ConfigError(f"{path}: {i}번째 항목 형식을 알 수 없습니다: {item!r}")
        if entry.complex_no in seen:
            raise ConfigError(f"{path}: 단지 {entry.complex_no}가 중복 등록되어 있습니다.")
        seen.add(entry.complex_no)
        result.append(entry)
    return result

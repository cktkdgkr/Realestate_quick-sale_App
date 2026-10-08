"""Alembic 마이그레이션을 코드에서 적용한다.

사용: `python -m app.db.migrate` (설정의 DB_PATH에 적용) 또는 `upgrade_db(path)`.
파이프라인은 실행 시작 시 upgrade_db()를 호출해 DB를 최신 스키마로 맞춘다 (2단계).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.db.session import sqlite_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(db_path: Path | str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    return cfg


def upgrade_db(db_path: Path | str, revision: str = "head") -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)


def main() -> int:
    from app.config import load_settings

    settings = load_settings()
    upgrade_db(settings.db_path)
    print(f"DB 마이그레이션 완료: {settings.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

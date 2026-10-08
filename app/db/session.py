"""DB 엔진·세션 생성."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def sqlite_url(db_path: Path | str) -> str:
    return f"sqlite:///{Path(db_path).resolve()}"


def make_engine(db_path: Path | str) -> Engine:
    """SQLite 엔진. 상위 폴더가 없으면 만든다. 외래키 제약을 켠다."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(path), future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):  # pragma: no cover - 단순 설정
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)

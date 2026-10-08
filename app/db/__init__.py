"""DB 계층: ORM 모델, 세션, 마이그레이션. 담당: 웹·인프라 agent."""

from app.db.models import (
    AlertHistoryRow,
    AreaTypeRow,
    Base,
    ComplexRow,
    ListingSnapshotRow,
    RunRow,
    TradeSnapshotRow,
    VerdictRow,
)
from app.db.session import make_engine, make_session_factory


def upgrade_db(db_path, revision: str = "head") -> None:
    """app.db.migrate.upgrade_db (지연 import: `python -m app.db.migrate` 경고 방지)."""
    from app.db.migrate import upgrade_db as _upgrade

    _upgrade(db_path, revision)

__all__ = [
    "AlertHistoryRow",
    "AreaTypeRow",
    "Base",
    "ComplexRow",
    "ListingSnapshotRow",
    "RunRow",
    "TradeSnapshotRow",
    "VerdictRow",
    "make_engine",
    "make_session_factory",
    "upgrade_db",
]

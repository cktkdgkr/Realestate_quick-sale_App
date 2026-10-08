"""DB 공용 컬럼 타입."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """시간대가 있는 datetime만 받아 UTC(naive)로 저장하고, 읽을 때 UTC aware로 돌려준다.

    naive datetime을 넣으면 오류: KST/UTC 혼동으로 9시간 어긋나는 버그를 막기 위함.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTCDateTime에는 시간대 정보가 있는 datetime만 저장할 수 있습니다.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)

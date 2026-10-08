"""재알림 이력 분류 (CLAUDE.md §4.4, notify 스킬 §2). 담당: 알림·리포트 agent.

흐름 (pipeline이 지킬 순서)
1. ``classify_alerts(...)``: DB를 **읽기만** 한다. Verdict.alert_kind를 채우고 "내려간 매물" 목록을 만든다.
2. ``prior_alerts(...)``: 가격 인하 표시용 이전 알림가를 읽는다 (읽기 전용). 반드시 commit 전에 부른다.
3. 리포트·요약 파일을 쓴다 (``app.notify.report.write_outputs``).
4. 파일 쓰기가 **성공한 뒤에만** ``commit_history(...)``를 부른다. 파일 쓰기가 실패하면 부르지 않는다
   → 다음 실행에서 같은 급매가 다시 NEW로 잡힌다 (알림 누락보다 중복이 낫다).

dry_run이면 어느 함수도 DB를 바꾸지 않는다 (CLAUDE.md §9).
수집 실패 단지(failed_complex_nos)의 이력은 읽기만 하고 절대 바꾸지 않는다. 그 단지의 active 이력은
"내려간 매물"로도 잡지 않는다 (실패를 "매물 사라짐"으로 오인하지 않기 위해).

alert_history 열 의미 (app/db/models.py AlertHistoryRow)
- active=True: 지금 급매로 알려진 상태. 다음 실행에서 급매면 PRICE_DROP/ONGOING 비교 대상.
- active=False: 급매가 아니게 되었거나 매물이 사라짐. 다시 급매가 되면 NEW로 다시 알린다.
- deactivated_run_id: active가 False로 바뀐 실행. 그 실행의 리포트에 "내려간 매물"로 1회 표시됐다는 기록.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AlertHistoryRow
from app.domain.models import Verdict

AlertKind = Literal["NEW", "PRICE_DROP", "ONGOING"]


@dataclass(frozen=True)
class _Plan:
    kinds: dict[str, AlertKind]                     # dedup_key -> 분류 (급매만)
    gone: list[dict]                                # 내려간 매물
    bargains: dict[str, Verdict]                    # dedup_key -> 급매 Verdict (실패 단지 포함)


def _check_unique(verdicts: Iterable[Verdict]) -> None:
    seen: set[str] = set()
    for v in verdicts:
        key = v.listing.dedup_key
        if key in seen:
            # dedup 이후 대표 매물만 들어와야 한다. 조용히 하나를 고르면 이력이 틀어진다.
            raise ValueError(f"같은 dedup_key의 Verdict가 두 번 들어왔습니다: {key!r}")
        seen.add(key)


def _load_rows(session: Session, keys: Iterable[str] | None = None) -> dict[str, AlertHistoryRow]:
    stmt = select(AlertHistoryRow)
    if keys is not None:
        keys = list(keys)
        if not keys:
            return {}
        stmt = stmt.where(AlertHistoryRow.dedup_key.in_(keys))
    return {r.dedup_key: r for r in session.scalars(stmt)}


def _plan(session: Session, verdicts: list[Verdict], failed_complex_nos: set[str]) -> _Plan:
    _check_unique(verdicts)
    with session.no_autoflush:
        rows = _load_rows(session)

    bargains = {v.listing.dedup_key: v for v in verdicts if v.is_bargain}
    by_key = {v.listing.dedup_key: v for v in verdicts}

    kinds: dict[str, AlertKind] = {}
    for key, v in bargains.items():
        row = rows.get(key)
        if row is None or not row.active:
            kinds[key] = "NEW"
        elif v.listing.price < row.last_alerted_price:
            kinds[key] = "PRICE_DROP"
        else:
            kinds[key] = "ONGOING"

    gone: list[dict] = []
    for key in sorted(rows):
        row = rows[key]
        if not row.active or key in bargains:
            continue
        if row.complex_no in failed_complex_nos:
            continue  # 수집 실패 단지: 사라졌는지 알 수 없다
        current = by_key.get(key)
        gone.append(
            {
                "dedup_key": key,
                "complex_no": row.complex_no,
                "area_key": row.area_key,
                "last_alerted_price": row.last_alerted_price,
                "first_alerted_at": row.first_alerted_at,
                "last_alerted_at": row.last_alerted_at,
                # GONE: 이번 수집에 매물이 없음 / NOT_BARGAIN: 매물은 있으나 급매 조건을 벗어남
                "reason": "GONE" if current is None else "NOT_BARGAIN",
                "listing": current.listing if current is not None else None,
            }
        )
    return _Plan(kinds=kinds, gone=gone, bargains=bargains)


def classify_alerts(
    session: Session,
    verdicts: list[Verdict],
    failed_complex_nos: set[str],
    run_id: str,
    dry_run: bool,
) -> tuple[list[Verdict], list[dict]]:
    """alert_kind를 채운 Verdict 목록과 "지난주 급매 중 내려간 매물" 목록을 돌려준다.

    - DB는 읽기만 한다 (dry_run 여부와 무관). 이력 갱신은 commit_history가 한다.
    - 급매가 아닌 Verdict의 alert_kind는 None.
    - 입력 Verdict는 바꾸지 않고 복사본을 돌려준다. 순서는 입력과 같다.
    - 내려간 매물 dict 키: dedup_key, complex_no, area_key, last_alerted_price, first_alerted_at,
      last_alerted_at, reason("GONE"|"NOT_BARGAIN"), listing(Listing|None, NOT_BARGAIN일 때 현재 매물).
      수집 실패 단지의 이력은 포함하지 않는다.
    """
    del run_id, dry_run  # 읽기 전용이라 쓰지 않는다. §10 시그니처 유지용.
    plan = _plan(session, verdicts, set(failed_complex_nos))
    out = [
        replace(v, alert_kind=plan.kinds.get(v.listing.dedup_key) if v.is_bargain else None)
        for v in verdicts
    ]
    return out, plan.gone


def prior_alerts(session: Session, dedup_keys: Iterable[str]) -> dict[str, dict]:
    """이전 알림 정보 (읽기 전용). 리포트의 "가격 인하 (이전가 → 현재가)" 표시용.

    반환: {dedup_key: {"last_alerted_price": int, "first_alerted_at": datetime, "active": bool}}
    commit_history 전에 불러야 이전가가 남아 있다.
    """
    with session.no_autoflush:
        rows = _load_rows(session, dedup_keys)
    return {
        k: {
            "last_alerted_price": r.last_alerted_price,
            "first_alerted_at": r.first_alerted_at,
            "active": r.active,
        }
        for k, r in rows.items()
    }


def commit_history(
    session: Session,
    verdicts: list[Verdict],
    failed_complex_nos: set[str],
    run_id: str,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, int]:
    """이번 실행 결과로 alert_history를 갱신하고 session.commit()한다.

    **리포트·요약 파일 쓰기가 성공한 뒤에만 호출한다.** (C6-3 조정판)
    분류는 classify_alerts와 같은 규칙으로 DB에서 다시 계산한다 (입력 alert_kind에 의존하지 않음).

    | 이번 판정 | 이력 상태 | 갱신 |
    |---|---|---|
    | 급매 | 없음 / active=False | NEW: 생성 또는 재활성화, first/last_alerted_at=now, last_alerted_price=현재가 |
    | 급매 | active, 현재가 < 이전 알림가 | PRICE_DROP: last_alerted_price=현재가, last_alerted_at=now |
    | 급매 | active, 현재가 ≥ 이전 알림가 | ONGOING: last_seen_run_id만 갱신 |
    | 급매 아님 / 매물 없음 | active | active=False, deactivated_run_id=run_id |

    수집 실패 단지(failed_complex_nos)의 행은 만들지도 바꾸지도 않는다.
    dry_run이면 아무것도 바꾸지 않고 집계만 돌려준다.
    반환: {"NEW": n, "PRICE_DROP": n, "ONGOING": n, "DEACTIVATED": n, "SKIPPED_FAILED": n, "dry_run": 0|1}
    """
    failed = set(failed_complex_nos)
    plan = _plan(session, verdicts, failed)
    stats = {"NEW": 0, "PRICE_DROP": 0, "ONGOING": 0, "DEACTIVATED": 0, "SKIPPED_FAILED": 0,
             "dry_run": int(dry_run)}

    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("commit_history의 now에는 시간대가 있는 datetime이 필요합니다.")

    rows = _load_rows(session, plan.bargains.keys())
    for key, v in plan.bargains.items():
        if v.listing.complex_no in failed:
            stats["SKIPPED_FAILED"] += 1
            continue
        kind = plan.kinds[key]
        stats[kind] += 1
        if dry_run:
            continue
        row = rows.get(key)
        price = v.listing.price
        if kind == "NEW":
            if row is None:
                row = AlertHistoryRow(dedup_key=key)
                session.add(row)
            row.complex_no = v.listing.complex_no
            row.area_key = v.listing.area_key
            # 비활성 → 재활성은 새 알림 회차로 본다: first_alerted_at도 이번 시각으로 바꾼다.
            row.first_alerted_at = now
            row.last_alerted_at = now
            row.last_alerted_price = price
            row.active = True
            row.deactivated_run_id = None
        elif kind == "PRICE_DROP":
            row.last_alerted_price = price
            row.last_alerted_at = now
        row.last_seen_run_id = run_id

    gone_rows = _load_rows(session, [g["dedup_key"] for g in plan.gone])
    for g in plan.gone:
        stats["DEACTIVATED"] += 1
        if dry_run:
            continue
        row = gone_rows[g["dedup_key"]]
        row.active = False
        row.deactivated_run_id = run_id

    if not dry_run:
        session.commit()
    return stats

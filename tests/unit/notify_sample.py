"""알림·리포트 테스트용 고정 입력 (스냅샷·샘플 공용).

신규, 가격 인하, 지속 중, 내려간 매물(GONE·NOT_BARGAIN), 실거래 부족, 층 미상, 수집 실패,
실거래 매칭 확인 필요가 모두 들어 있다. 모든 값은 합성 데이터다 (실제 매물·개인정보 아님).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import AlertHistoryRow, Base
from app.domain import normalize
from app.domain.models import AreaType, Complex, Listing, RunResult, Trade, Verdict
from app.notify.history import classify_alerts, prior_alerts

KST = ZoneInfo("Asia/Seoul")
STARTED_AT = datetime(2026, 10, 13, 10, 0, tzinfo=KST)
PREV_RUN_AT = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
RUN_ID = "20261013-100000"

ELS, DONGA, HELIO = "22627", "3009", "111515"


def stub_format_price(manwon: int) -> str:
    """CLAUDE.md §10 format_price 명세 예시대로 동작하는 스텁 (실제 모듈이 없을 때만 사용)."""
    eok, rest = divmod(manwon, 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}"
    if eok:
        return f"{eok}억"
    return f"{rest:,}"


def ensure_format_price(monkeypatch) -> None:
    if not hasattr(normalize, "format_price"):
        monkeypatch.setattr(normalize, "format_price", stub_format_price, raising=False)


def new_session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def listing(cno: str, ak: float, dong: str, floor_raw: str, group: str, price: int, art: str,
            direction: str = "남향", realtors: int = 1, alt: list[int] | None = None) -> Listing:
    return Listing(
        article_no=art, complex_no=cno, area_key=ak, dong=dong, floor_raw=floor_raw,
        floor_group=group, direction=direction, price=price, confirmed_at=date(2026, 10, 10),
        realtor_count=realtors, alt_prices=alt or [],
        dedup_key=f"{cno}|{ak}|{dong}|{floor_raw}|{direction}",
        url=f"https://new.land.naver.com/complexes/{cno}?articleNo={art}",
    )


def verdict(lst: Listing, reasons: list[str], tb: int | None, lb: int | None,
            pct: float | None, short: bool = False) -> Verdict:
    return Verdict(listing=lst, is_bargain=bool(reasons), reasons=reasons, trade_base=tb,
                   listing_base=lb, discount_pct=pct, trade_sample_short=short, alert_kind=None)


def hist(key: str, cno: str, ak: float, price: int, active: bool = True) -> AlertHistoryRow:
    return AlertHistoryRow(
        dedup_key=key, complex_no=cno, area_key=ak, first_alerted_at=PREV_RUN_AT,
        last_alerted_at=PREV_RUN_AT, last_alerted_price=price, last_seen_run_id="20261006-100000",
        active=active, deactivated_run_id=None,
    )


def sample_verdicts() -> list[Verdict]:
    return [
        # 잠실엘스 84.88: 신규(일반층, 실거래+매물), 가격 인하(저층, 실거래), 급매 아님, 층 미상
        verdict(listing(ELS, 84.88, "101동", "15/25", "NORMAL", 250000, "2650000001", realtors=2,
                        alt=[252000]), ["TRADE", "LISTING"], 270000, 268000, 7.4),
        verdict(listing(ELS, 84.88, "105동", "2/25", "LOW", 238000, "2650000002"),
                ["TRADE"], 270000, 250000, 11.9),
        verdict(listing(ELS, 84.88, "110동", "12/25", "NORMAL", 268000, "2650000003"),
                [], 270000, 250000, None),
        verdict(listing(ELS, 84.88, "112동", "", "UNKNOWN", 240000, "2650000004"),
                [], None, None, None),
        # 잠실엘스 59.96: 지속 중 (실거래 2건, 표본 부족)
        verdict(listing(ELS, 59.96, "120동", "7/25", "NORMAL", 180000, "2650000005", direction="동향"),
                ["TRADE"], 190000, 186000, 5.3, short=True),
        verdict(listing(ELS, 59.96, "121동", "9/25", "NORMAL", 186000, "2650000006"),
                [], 190000, 180000, None, short=True),
        # 잠원동아 84.69: 국토부 매핑 없음 → 실거래 부족, 매물 조건만으로 신규
        verdict(listing(DONGA, 84.69, "3동", "5/15", "NORMAL", 160000, "2650000007"),
                ["LISTING"], None, 175000, 8.6, short=True),
        verdict(listing(DONGA, 84.69, "4동", "저/15", "LOW", 150000, "2650000008"),
                [], None, 160000, None, short=True),
        verdict(listing(DONGA, 84.69, "1동", "10/15", "NORMAL", 175000, "2650000009"),
                [], None, 160000, None, short=True),
    ]


def seed_history(session: Session) -> None:
    v = {x.listing.article_no: x.listing.dedup_key for x in sample_verdicts()}
    session.add_all([
        hist(v["2650000002"], ELS, 84.88, 243000),            # → 가격 인하
        hist(v["2650000005"], ELS, 59.96, 180000),            # → 지속 중 (같은 가격)
        hist(v["2650000003"], ELS, 84.88, 255000),            # → 급매 조건 벗어남 (NOT_BARGAIN)
        hist(f"{DONGA}|84.69|2동|8/15|남향", DONGA, 84.69, 158000),  # → 매물 내려감 (GONE)
        hist(f"{HELIO}|84.95|301동|20/35|남향", HELIO, 84.95, 200000),  # 수집 실패 단지: 건드리지 않음
    ])
    session.commit()


def sample_context_base() -> dict:
    return {
        "complexes": [
            Complex(ELS, "잠실엘스", "11710", "서울 송파구 잠실동", 35, "11710-0001"),
            Complex(DONGA, "잠원동아", "11650", "서울 서초구 잠원동", 15, None),
        ],
        "area_types": {
            ELS: [AreaType(ELS, 59.96, 59.96, 82.6, 25, "82A"),
                  AreaType(ELS, 84.88, 84.88, 112.4, 34, "112B")],
            DONGA: [AreaType(DONGA, 84.69, 84.69, 105.8, 32, "105")],
        },
        "area_summaries": {
            (ELS, 84.88): dict(t_normal=270000, trade_sample_count=3, trade_sample_short=False,
                               l_normal_min=250000, l_low_min=238000, listing_count=4, unknown_count=1),
            (ELS, 59.96): dict(t_normal=190000, trade_sample_count=2, trade_sample_short=True,
                               l_normal_min=180000, l_low_min=None, listing_count=2, unknown_count=0),
            (DONGA, 84.69): dict(t_normal=None, trade_sample_count=0, trade_sample_short=True,
                                 l_normal_min=160000, l_low_min=150000, listing_count=3, unknown_count=0),
        },
        "failures": [
            {"complex_no": HELIO, "name": "헬리오시티", "stage": "blocked",
             "detail": "HTTP 429 연속 3회"},
        ],
        "molit_candidates": {
            DONGA: [
                {"aptSeq": "11650-0101", "aptNm": "동아", "umdNm": "잠원동", "jibun": "65"},
                {"aptSeq": "11650-0102", "aptNm": "동아(2차)", "umdNm": "잠원동", "jibun": "70"},
            ],
        },
        "trades": {
            (ELS, 84.88): [
                Trade(ELS, 84.88, 84.88, 2, "LOW", 245000, date(2026, 8, 20), False, "중개거래", "MOLIT"),
                Trade(ELS, 84.88, 84.88, 15, "NORMAL", 270000, date(2026, 9, 12), False, "중개거래", "MOLIT"),
            ],
        },
        "complexes_file": "config/complexes.yaml",
        "dry_run": False,
        "report_path": "reports/2026-10-13.html",
    }


def build_sample(session: Session | None = None) -> tuple[RunResult, dict, Session]:
    """이력 DB를 채우고 classify_alerts를 실제로 돌려 리포트 입력을 만든다."""
    session = session or new_session()
    seed_history(session)
    ctx = sample_context_base()
    failed = {f["complex_no"] for f in ctx["failures"]}
    verdicts, gone = classify_alerts(session, sample_verdicts(), failed, RUN_ID, dry_run=False)
    ctx["gone"] = gone
    ctx["prior_alerts"] = prior_alerts(
        session, [v.listing.dedup_key for v in verdicts if v.alert_kind == "PRICE_DROP"]
    )
    run = RunResult(
        run_id=RUN_ID, started_at=STARTED_AT, status="PARTIAL", verdicts=verdicts,
        errors=["헬리오시티(111515) blocked: HTTP 429 연속 3회 (요청 URL ...?api_key=DUMMY000&page=1)"],
        cross_check_warnings=["잠실엘스 84.88㎡ 2026-09-12 15층: 국토부 270,000 / 네이버 268,000 (국토부 기준 사용)"],
    )
    return run, ctx, session

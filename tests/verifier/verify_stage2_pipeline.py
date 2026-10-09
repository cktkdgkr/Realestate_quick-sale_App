"""검증 agent 반례: 2단계 pipeline (C7-3/4/5, blocked, 드라이런, 키 없음, 경고 전달, 급감, 대상 평형 0개).

수집기 함수만 fake로 바꾸고 dedup·rules·history·report·summary는 실제 코드를 쓴다.
실행: .venv/bin/python tests/verifier/verify_stage2_pipeline.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import pipeline  # noqa: E402
from app.collectors import molit_trades, naver_listings, naver_trades  # noqa: E402
from app.collectors.errors import CollectorError  # noqa: E402
from app.domain.models import AreaType, Complex, Listing, Trade  # noqa: E402
from app.notify import history  # noqa: E402

SECRET = "VERIFIER-SECRET-KEY-77aa55"
AREA = 84.97
NOW = datetime(2026, 10, 12, 16, 30, tzinfo=UTC)  # = 2026-10-13(화) 01:30 KST
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))


def L(cno, n, price, fg="NORMAL"):
    return Listing(f"{cno}-{n}", cno, AREA, f"10{n}동", f"{n + 3}/20", fg, "남향", price, None, 1, [],
                   f"{cno}|{AREA}|10{n}동|{n + 3}/20|남향", f"https://example.invalid/{cno}/{n}")


def T(cno, price, d=date(2026, 9, 1)):
    return Trade(cno, AREA, AREA, 10, "NORMAL", price, d, False, "중개거래", "MOLIT")


class Harness:
    def __init__(self, cnos=("111", "222", "333"), seqs=True):
        self.tmp = Path(tempfile.mkdtemp(prefix="verif_pipe_"))
        lines = ["complexes:"]
        for c in cnos:
            lines.append(f"  - complex_no: '{c}'\n    name: 단지{c}")
            if seqs:
                lines.append(f"    molit_apt_seq: 'SEQ-{c}'")
        (self.tmp / "c.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.env = {
            "MOLIT_API_KEY": SECRET, "DRY_RUN": "false",
            "DB_PATH": str(self.tmp / "wt/state/history.sqlite3"),
            "REPORT_DIR": str(self.tmp / "wt/reports"), "OUT_DIR": str(self.tmp / "out"),
            "COMPLEXES_FILE": str(self.tmp / "c.yaml"),
        }
        self.fail: dict[tuple[str, str], Exception] = {}
        self.listings: dict[str, list[Listing]] = {}
        self.trades: dict[str, list[Trade]] = {}
        self.area_types: dict[str, list[AreaType]] = {}
        self.naver_calls: list[tuple[str, str]] = []
        self.spy: dict[str, list] = {"classify": [], "prior": [], "commit": [], "write": []}
        self.events: list[str] = []
        self.naver_warn: dict[str, str] = {}
        self.molit_warn: dict[str, str] = {}

    def install(self):
        h = self
        os.environ.update(self.env)

        class FakeNaver:
            def __init__(self, *a, **k):
                self.warnings, self.blocked = [], None
            def close(self): pass

        class FakeMolit:
            def __init__(self, api_key, *a, **k):
                self.warnings = []
            def drain_warnings(self):
                o, self.warnings = self.warnings, []
                return o
            def close(self): pass

        def maybe(stage, cno, client=None):
            e = h.fail.get((stage, cno))
            if e:
                if client is not None and getattr(e, "stage", None) == "blocked":
                    client.blocked = e
                raise e

        def fetch_complex(client, cno):
            h.naver_calls.append(("fetch_complex", cno)); maybe("fetch_complex", cno, client)
            ats = h.area_types.get(cno, [AreaType(cno, AREA, AREA, 112.4, 34, "112A")])
            return Complex(cno, f"단지{cno}", "11650", "서울", 20, None), ats

        def fetch_listings(client, cx, ats):
            h.naver_calls.append(("fetch_listings", cx.complex_no)); maybe("fetch_listings", cx.complex_no, client)
            if cx.complex_no in h.naver_warn:
                client.warnings.append(h.naver_warn[cx.complex_no])
            return list(h.listings.get(cx.complex_no, [L(cx.complex_no, 1, 100000), L(cx.complex_no, 2, 99000)]))

        def fetch_trades(client, cx, ats, as_of):
            maybe("fetch_trades", cx.complex_no)
            h.events.append(f"as_of={as_of}")
            if cx.complex_no in h.molit_warn:
                client.warnings.append(h.molit_warn[cx.complex_no])
            return list(h.trades.get(cx.complex_no, [T(cx.complex_no, 100000)]))

        def fetch_naver_trades(client, cx, ats):
            h.naver_calls.append(("fetch_naver_trades", cx.complex_no)); maybe("fetch_naver_trades", cx.complex_no, client)
            return []

        real_classify, real_prior, real_commit = ORIG["classify"], ORIG["prior"], ORIG["commit"]
        from app.notify import report as rep
        real_write = ORIG["write"]

        def classify(*a, **k):
            h.spy["classify"].append((a, k)); h.events.append("classify")
            return real_classify(*a, **k)

        def prior(*a, **k):
            h.spy["prior"].append((a, k)); h.events.append("prior")
            return real_prior(*a, **k)

        def commit(*a, **k):
            h.spy["commit"].append((a, k)); h.events.append("commit")
            return real_commit(*a, **k)

        def write(*a, **k):
            h.events.append("write_start")
            if h.fail.get(("write", "*")):
                raise h.fail[("write", "*")]
            r = real_write(*a, **k)
            h.events.append("write_done")
            return r

        pipeline._utcnow = lambda: NOW
        naver_listings.NaverClient = FakeNaver
        naver_listings.fetch_complex = fetch_complex
        naver_listings.fetch_listings = fetch_listings
        molit_trades.MolitClient = FakeMolit
        molit_trades.fetch_trades = fetch_trades
        molit_trades.find_apt_seq_candidates = lambda c, cx, a: [
            {"apt_seq": "CAND", "apt_nm": "x", "umd_nm": "y", "jibun": "1", "trade_count": 1,
             "last_contract": "2026-09-01"}]
        naver_trades.fetch_naver_trades = fetch_naver_trades
        history.classify_alerts, history.prior_alerts, history.commit_history = classify, prior, commit
        rep.write_outputs = write
        return self

    def run(self, *args):
        return pipeline.main(["--once", *args])

    def summary(self):
        p = Path(self.env["OUT_DIR"]) / "summary.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def report(self, d="2026-10-13", where="REPORT_DIR"):
        p = Path(self.env[where]) / f"{d}.html"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def db(self, sql):
        con = sqlite3.connect(self.env["DB_PATH"])
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()


ORIG = {
    "NaverClient": naver_listings.NaverClient, "fetch_complex": naver_listings.fetch_complex,
    "fetch_listings": naver_listings.fetch_listings, "MolitClient": molit_trades.MolitClient,
    "fetch_trades": molit_trades.fetch_trades,
    "classify": history.classify_alerts, "prior": history.prior_alerts, "commit": history.commit_history,
}
from app.notify import report as _rep  # noqa: E402
ORIG["write"] = _rep.write_outputs


def scenario(fn):
    def wrapper():
        try:
            fn()
        except Exception:
            check(fn.__name__ + " (예외 없이 실행)", False, traceback.format_exc())
    wrapper.__name__ = fn.__name__
    return wrapper


@scenario
def c73_one_complex_raises():
    h = Harness().install()
    h.fail[("fetch_listings", "222")] = KeyError("unexpected")
    rc = h.run()
    s, r = h.summary(), h.report()
    check("C7-3 단지 하나 예외 -> 종료코드 1", rc == 1, f"rc={rc}")
    check("C7-3 summary 첫 줄 PARTIAL·수집 실패", "수집 실패" in s.splitlines()[0], s.splitlines()[0] if s else s)
    check("C7-3 summary에 실패 단지명 + 수집 실패", "단지222" in s and "수집 실패" in s, s)
    check("C7-3 리포트에 단지222 수집 실패", "단지222" in r and "수집 실패" in r)
    ok_verdicts = h.db("select count(*) from verdicts")[0][0]
    check("C7-3 나머지 단지 판정 결과 저장(>=4)", ok_verdicts >= 4, f"verdicts={ok_verdicts}")
    st = h.db("select status from runs")
    check("C7-3 runs.status=PARTIAL", st == [("PARTIAL",)], str(st))


@scenario
def c73_all_fail():
    h = Harness().install()
    for c in ("111", "222", "333"):
        h.fail[("fetch_complex", c)] = CollectorError("network", "boom", c)
    rc = h.run()
    s = h.summary()
    check("C7-3 전부 실패 -> 종료코드 2", rc == 2, f"rc={rc}")
    check("C7-3 전부 실패 summary 첫 줄 수집 실패", s and "수집 실패" in s.splitlines()[0], s[:200])
    check("C7-3 전부 실패 summary에 '급매 없음' 단정 문구 없음",
          "- 신규·가격 인하 급매 없음" not in s, s)
    check("C7-3 전부 실패 리포트도 생성(정책 5)", bool(h.report()))


@scenario
def molit_failure_fails_complex():
    h = Harness().install()
    h.fail[("fetch_trades", "111")] = CollectorError("molit_api", "500", "111")
    rc = h.run()
    s = h.summary()
    check("정책1 국토부 실패 -> 단지 수집 실패 PARTIAL", rc == 1 and "단지111" in s and "수집 실패" in s, s[:300])


@scenario
def c74_tz_utc():
    os.environ["TZ"] = "UTC"
    import time
    time.tzset()
    h = Harness().install()
    rc = h.run()
    check("C7-4 TZ=UTC as_of가 KST 날짜(2026-10-13)", "as_of=2026-10-13" in h.events, str(h.events))
    check("C7-4 리포트 파일명 KST 날짜", (Path(h.env["REPORT_DIR"]) / "2026-10-13.html").exists(),
          str(list(Path(h.env["REPORT_DIR"]).glob("*"))))


@scenario
def c75_lock():
    h = Harness().install()
    assert h.run() == 0
    con = sqlite3.connect(h.env["DB_PATH"])
    started = (NOW - timedelta(minutes=30)).replace(tzinfo=None).isoformat(sep=" ")
    con.execute("insert into runs(run_id, started_at, status, dry_run, as_of, errors, cross_check_warnings, complex_results)"
                " values ('other', ?, 'RUNNING', 0, '2026-10-13', '[]', '[]', '{}')", (started,))
    con.commit(); con.close()
    n_hist = h.db("select count(*), group_concat(last_seen_run_id) from alert_history")
    Path(h.env["OUT_DIR"], "summary.md").unlink()
    rc = h.run()
    check("C7-5 RUNNING(30분) 있으면 종료코드 2", rc == 2, f"rc={rc}")
    check("C7-5 락 실패 시 이력 변경 없음", h.db("select count(*), group_concat(last_seen_run_id) from alert_history") == n_hist)
    check("C7-5 락 실패 시 summary 없음(=routine_run.sh가 FAILED 출력)", not Path(h.env["OUT_DIR"], "summary.md").exists())
    # stale
    con = sqlite3.connect(h.env["DB_PATH"])
    old = (NOW - timedelta(hours=2, minutes=1)).replace(tzinfo=None).isoformat(sep=" ")
    con.execute("update runs set started_at=? where run_id='other'", (old,))
    con.commit(); con.close()
    rc = h.run()
    st = h.db("select status from runs where run_id='other'")
    check("C7-5 2시간 넘은 RUNNING 해제 후 실행", rc == 0 and st == [("FAILED",)], f"rc={rc} st={st}")


@scenario
def c75_concurrent_processes():
    """두 프로세스 동시 acquire_run_lock: 승자는 정확히 1개."""
    import subprocess
    tmp = Path(tempfile.mkdtemp(prefix="verif_lock_"))
    db = tmp / "h.sqlite3"
    from app.db import upgrade_db
    upgrade_db(db)
    code = (
        "import sys,time;sys.path.insert(0,%r)\n"
        "from datetime import datetime,UTC,date\n"
        "from app import pipeline\nfrom app.db import make_engine,make_session_factory\n"
        "S=make_session_factory(make_engine(%r))\n"
        "t=float(sys.argv[2])\n"
        "while time.time()<t: pass\n"
        "try:\n pipeline.acquire_run_lock(S,sys.argv[1],datetime.now(UTC),date(2026,10,13),False);print('WIN')\n"
        "except pipeline.LockBusy: print('BUSY')\n" % (str(ROOT), str(db))
    )
    import time
    wins = 0
    for trial in range(5):
        con = sqlite3.connect(db); con.execute("delete from runs"); con.commit(); con.close()
        t = time.time() + 1.5
        ps = [subprocess.Popen([sys.executable, "-c", code, f"r{trial}-{i}", str(t)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True) for i in range(3)]
        outs = [p.communicate(timeout=60) for p in ps]
        res = [o[0].strip() for o in outs]
        if res.count("WIN") != 1:
            check(f"C7-5 동시 3프로세스 승자 1개 (trial {trial})", False, f"{res} {[o[1][-300:] for o in outs]}")
            return
        wins += 1
    check("C7-5 동시 3프로세스 승자 1개 (5회)", wins == 5)


@scenario
def blocked_stops_naver():
    h = Harness().install()
    h.fail[("fetch_listings", "111")] = CollectorError("blocked", "HTTP 403", "111")
    rc = h.run()
    after = [c for c in h.naver_calls if c != ("fetch_complex", "111") and c != ("fetch_listings", "111")]
    check("blocked 이후 네이버 요청 0건", after == [], str(h.naver_calls))
    s = h.summary()
    check("blocked: 전부 실패 FAILED, 나머지 단지도 수집 실패 표시",
          rc == 2 and all(f"단지{c}" in s for c in ("111", "222", "333")), f"rc={rc}\n{s}")


@scenario
def blocked_real_client_http_count():
    """실제 NaverClient + MockTransport(403): 이후 단지에서 HTTP 요청이 0건인지."""
    h = Harness().install()
    hits: list[str] = []

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(403, text="forbidden")

    RealNaver = ORIG["NaverClient"]
    naver_listings.NaverClient = lambda *a, **k: RealNaver(http=httpx.Client(transport=httpx.MockTransport(handler)),
                                                          sleep=lambda s: None)
    naver_listings.fetch_complex = ORIG["fetch_complex"]
    naver_listings.fetch_listings = ORIG["fetch_listings"]
    rc = h.run()
    check("blocked 실제 클라이언트: HTTP 요청 1건에서 멈춤", len(hits) == 1, f"hits={hits}")
    check("blocked 실제 클라이언트: FAILED(2)", rc == 2, f"rc={rc}")


@scenario
def dry_run_history_untouched():
    h = Harness().install()
    assert h.run() == 0
    before = h.db("select * from alert_history order by dedup_key")
    snaps = h.db("select count(*) from listings_snapshot")
    h.listings["111"] = [L("111", 1, 80000), L("111", 2, 99000)]  # 가격 인하
    h.listings["222"] = []  # 사라짐
    reports_before = sorted(p.name for p in Path(h.env["REPORT_DIR"]).iterdir())
    rc = h.run("--dry-run")
    after = h.db("select * from alert_history order by dedup_key")
    check("드라이런 alert_history 불변", before == after, f"{before}\n{after}")
    check("드라이런 스냅샷 불변", snaps == h.db("select count(*) from listings_snapshot"))
    check("드라이런 reports/ 불변", reports_before == sorted(p.name for p in Path(h.env["REPORT_DIR"]).iterdir()))
    check("드라이런 out/에 리포트", (Path(h.env["OUT_DIR"]) / "2026-10-13.html").exists())
    check("드라이런 commit_history 미호출", len(h.spy["commit"]) == 1, str(len(h.spy["commit"])))
    s = h.summary()
    check("드라이런 분류는 기존 이력 사용(가격 인하)", "가격 인하" in s, s)


@scenario
def no_molit_key():
    h = Harness().install()
    assert h.run() == 0  # 실거래 기준으로 급매 이력 생성
    h.env["MOLIT_API_KEY"] = ""
    os.environ["MOLIT_API_KEY"] = ""
    hist_before = h.db("select dedup_key, active, last_alerted_price, deactivated_run_id from alert_history order by 1")
    rc = h.run()
    s, r = h.summary(), h.report()
    check("키 없음 -> PARTIAL(1)", rc == 1, f"rc={rc}")
    check("키 없음 summary 표시", "키 없음" in s, s)
    check("키 없음 summary 첫 줄이 정상(OK)처럼 보이지 않음", "PARTIAL" in s.splitlines()[0] or "일부" in s.splitlines()[0]
          or "실패" in s.splitlines()[0], s.splitlines()[0])
    check("키 없음 리포트 표시", "키 없음" in r)
    a, k = h.spy["commit"][-1]
    prot = a[2] if len(a) > 2 else k.get("failed_complex_nos")
    check("키 없음 모든 단지 이력 보호", set(prot) == {"111", "222", "333"}, str(prot))
    hist_after = h.db("select dedup_key, active, last_alerted_price, deactivated_run_id from alert_history order by 1")
    check("키 없음 이력 불변", hist_before == hist_after, f"{hist_before}\n{hist_after}")
    check("키 없음 '내려간 매물' 0", "내려간 매물 0" in s, s)
    check("키 없음 비밀값 미노출", SECRET not in s and SECRET not in r)


@scenario
def collector_warnings_passed():
    h = Harness().install()
    h.naver_warn["111"] = "NAVERWARN-평형 미매칭 3건"
    h.molit_warn["222"] = "MOLITWARN-매칭 실패 2건"
    h.run()
    r = h.report()
    ctx_ok = "NAVERWARN" in r and "MOLITWARN" in r
    check("수집기 경고(NaverClient.warnings, molit drain) 리포트 전달", ctx_ok)
    w = h.db("select cross_check_warnings from runs")[0][0]
    check("수집기 경고 runs 기록", "NAVERWARN" in w and "MOLITWARN" in w, w)


@scenario
def listing_drop():
    h = Harness().install()
    h.listings["111"] = [L("111", i, 100000 + i) for i in range(1, 11)]
    assert h.run() == 0
    h.listings["111"] = [L("111", 1, 100001), L("111", 2, 100002)]  # 10 -> 2 (80%)
    h.listings["222"] = [L("222", 1, 100000)]  # 2 -> 1 (50%) 경고 없음
    h.run()
    r = h.report()
    check("매물 80% 급감 경고(10->2)", "급감" in r and "단지111" in r.split("급감")[0][-300:] + r, "")
    check("50% 감소는 경고 없음", "단지222(222): 매물 수 급감" not in r)
    s = h.summary()
    check("급감 경고 summary에 '수집 경고' 표시", "수집 경고" in s, s)


@scenario
def target_prior_commit_order():
    h = Harness().install()
    h.run()
    a, k = h.spy["classify"][0]
    check("classify_alerts target_complex_nos 전달", k.get("target_complex_nos") == {"111", "222", "333"}, str(k))
    check("prior_alerts 호출", len(h.spy["prior"]) == 1)
    ev = h.events
    check("순서 classify<prior<write_done<commit",
          ev.index("classify") < ev.index("prior") < ev.index("write_done") < ev.index("commit"), str(ev))
    ck = h.spy["commit"][0][1]
    check("commit_history target_complex_nos 전달", ck.get("target_complex_nos") == {"111", "222", "333"}, str(ck))
    # 쓰기 실패
    h2 = Harness().install()
    h2.fail[("write", "*")] = OSError("disk full")
    rc = h2.run()
    check("파일 쓰기 실패 -> commit_history 미호출", h2.spy["commit"] == [], str(h2.spy["commit"]))
    check("파일 쓰기 실패 -> FAILED, alert_history 0행", rc == 2 and h2.db("select count(*) from alert_history") == [(0,)])
    check("파일 쓰기 실패 summary 수집 실패", "수집 실패" in h2.summary().splitlines()[0])


@scenario
def zero_target_areas():
    """§9 2026-10-09 ①: 대상 평형 0개 단지는 실패 아님(status 그대로), 확인 필요에 '대상 평형 없음: 단지명(번호)'."""
    h = Harness().install()
    h.area_types["222"] = []
    h.listings["222"] = []
    rc = h.run()
    s, r = h.summary(), h.report()
    chk = s.split("## 확인 필요", 1)[1] if "## 확인 필요" in s else ""
    check("대상 평형 0개: status 그대로 OK(rc=0)", rc == 0 and h.db("select status from runs") == [("OK",)], f"rc={rc}")
    check("대상 평형 0개: summary 확인 필요에 '대상 평형 없음: 단지222(222)'", "대상 평형 없음: 단지222(222)" in chk, s)
    check("대상 평형 0개: summary 첫 줄에 수집 실패 표시 없음(실패 아님)", "수집 실패 없음" in s.splitlines()[0] or
          "수집 실패" not in s.splitlines()[0], s.splitlines()[0])
    # 리포트에는 '확인 필요' 제목이 따로 없고, 상단 요약 상자(실거래 매칭 확인 필요·실거래 부족·층 미상과 같은 곳)에 나온다
    i = r.find("대상 평형 없음: 단지222(222)")
    check("대상 평형 0개: 리포트 상단 요약(확인 항목 상자)에 '대상 평형 없음: 단지222(222)'",
          i >= 0 and i < r.find("이번 주 알림"), "")
    # 수집 실패 단지는 '수집 실패'로만 (대상 평형 없음으로 보이면 안 됨)
    for stage in ("fetch_complex", "fetch_listings"):
        h2 = Harness().install()
        h2.fail[(stage, "222")] = CollectorError("network", "boom", "222")
        rc2 = h2.run()
        s2, r2 = h2.summary(), h2.report()
        check(f"실패 단지({stage}): '대상 평형 없음' 표시 없음, '수집 실패' 표시",
              rc2 == 1 and "대상 평형 없음" not in s2 and "대상 평형 없음" not in r2
              and "평형(공급 119.0㎡ 이하)이 없습니다" not in r2 and "단지222" in s2 and "수집 실패" in s2, s2)
    # 대상 평형 0개 + 다른 단지 실패 동시
    h3 = Harness().install()
    h3.area_types["222"] = []; h3.listings["222"] = []
    h3.fail[("fetch_listings", "333")] = CollectorError("network", "boom", "333")
    rc3 = h3.run(); s3 = h3.summary()
    check("대상 평형 0개 + 다른 단지 실패: PARTIAL, 222는 대상 평형 없음, 333은 수집 실패",
          rc3 == 1 and "대상 평형 없음: 단지222(222)" in s3 and "대상 평형 없음: 단지333" not in s3
          and "단지333(333): 수집 실패" in s3, s3)


@scenario
def failure_unit_area():
    """평형 하나(두 번째 area_key)의 판정이 실패하면 단지 전체 실패, 그 단지 Verdict는 넘기지 않음."""
    from app.domain import rules
    h = Harness().install()
    A2 = 59.97
    h.area_types["222"] = [AreaType("222", AREA, AREA, 112.4, 34, "112A"), AreaType("222", A2, A2, 80.0, 24, "80A")]
    l2 = Listing("222-9", "222", A2, "109동", "9/20", "NORMAL", "남향", 50000, None, 1, [],
                 f"222|{A2}|109동|9/20|남향", "https://example.invalid/222/9")
    h.listings["222"] = [L("222", 1, 80000), L("222", 2, 99000), l2]
    real_judge = rules.judge
    def judge(ls, ts, as_of):
        if ls and ls[0].complex_no == "222" and ls[0].area_key == A2:
            raise ValueError("area fail")
        return real_judge(ls, ts, as_of)
    rules.judge = judge
    try:
        rc = h.run()
    finally:
        rules.judge = real_judge
    a, k = h.spy["classify"][0]
    verdicts = a[1]
    failed = a[2]
    check("실패단위: 평형 하나 실패 -> 단지 222 failed_complex_nos", "222" in set(failed), str(failed))
    check("실패단위: 실패 단지 Verdict classify에 미전달", all(v.listing.complex_no != "222" for v in verdicts),
          str([v.listing.dedup_key for v in verdicts]))
    r = h.report()
    check("실패단위: 리포트에 실패 단지 매물(222-1 url) 없음", "example.invalid/222/" not in r)
    check("실패단위: 종료코드 1, 단지222 수집 실패 표시", rc == 1 and "단지222" in h.summary(), f"rc={rc}")
    vr = h.db("select count(*) from verdicts where dedup_key like '222|%'")
    check("실패단위: 실패 단지 verdict 스냅샷 없음", vr == [(0,)], str(vr))


def main():
    base_env = dict(os.environ)
    for sc in [c73_one_complex_raises, c73_all_fail, molit_failure_fails_complex, c74_tz_utc, c75_lock,
               c75_concurrent_processes, blocked_stops_naver, blocked_real_client_http_count,
               dry_run_history_untouched, no_molit_key, collector_warnings_passed, listing_drop,
               target_prior_commit_order, zero_target_areas, failure_unit_area]:
        os.environ.clear(); os.environ.update(base_env)
        sc()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

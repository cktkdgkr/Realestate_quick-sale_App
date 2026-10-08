"""검증 agent 반례 스크립트 (1단계 골격). pytest 수집 대상 아님: 직접 실행한다.

    .venv/bin/python tests/verifier/verify_stage1_skeleton.py

각 항목은 PASS/FAIL을 출력한다. FAIL이 하나라도 있으면 exit 1.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import ConfigError, load_complexes, load_settings, parse_complex_no  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


tmp = Path(tempfile.mkdtemp())

# V1: OS 환경변수 TZ=UTC 가 업무 시간대(Asia/Seoul)를 바꾸는가
s = load_settings(env_file=tmp / "none", environ={"TZ": "UTC"}, root=tmp)
check("V1 OS TZ=UTC 환경에서도 tz=Asia/Seoul", s.tz == "Asia/Seoul", f"actual tz={s.tz!r}")
# 그 결과 as_of(실행일)가 달라지는 시각 예: 2026-10-13 08:00 KST
kst_morning = datetime(2026, 10, 13, 8, 0, tzinfo=timezone(timedelta(hours=9)))
as_of = kst_morning.astimezone(s.zoneinfo).date()
check("V1b 08:00 KST 실행 시 as_of가 KST 날짜", as_of == date(2026, 10, 13), f"actual as_of={as_of}")

# V2: 비밀값이 repr/str/ConfigError 메시지에 나오지 않음
s2 = load_settings(env_file=tmp / "none", environ={"MOLIT_API_KEY": "SECRET-XYZ"}, root=tmp)
check("V2 repr/str에 비밀값 없음", "SECRET-XYZ" not in repr(s2) and "SECRET-XYZ" not in str(s2))

# V3: complexes.yaml 반례
def yaml_case(text: str):
    p = tmp / "c.yaml"
    p.write_text(text, encoding="utf-8")
    try:
        return load_complexes(p)
    except ConfigError as e:
        return e

r = yaml_case("complexes:\n  - complex_no: 3009\n")
check("V3a 따옴표 없는 정수 complex_no", not isinstance(r, Exception) and r[0].complex_no == "3009", repr(r))
r = yaml_case("complexes:\n  - 3009\n  - https://new.land.naver.com/complexes/3009?a=APT\n")
check("V3b 번호와 URL로 같은 단지 중복 -> 오류", isinstance(r, ConfigError), repr(r))
r = yaml_case("complexes:\n  - complex_no: null\n")
check("V3c complex_no: null -> 오류", isinstance(r, ConfigError), repr(r))
r = yaml_case("complexes:\n  - \n")
check("V3d 빈 항목 -> 오류", isinstance(r, ConfigError), repr(r))
r = yaml_case("")
check("V3e 빈 파일 -> 오류", isinstance(r, ConfigError), repr(r))
for bad in ["https://new.land.naver.com/complexes/3009abc", "3009 ", " 30-09"]:
    try:
        v = parse_complex_no(bad)
        ok = bad.strip() == "3009" and v == "3009"
    except ConfigError:
        ok = bad.strip() != "3009"
    check(f"V3f parse_complex_no({bad!r})", ok)

# V4: Alembic 업그레이드 -> 다운그레이드 -> 재업그레이드, ORM과 차이 없음
from alembic import command  # noqa: E402
from app.db.migrate import alembic_config, upgrade_db  # noqa: E402

db = tmp / "nested" / "dir" / "h.sqlite3"
upgrade_db(db)
command.downgrade(alembic_config(db), "base")
tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master where type='table'")}
check("V4a downgrade base 후 테이블은 alembic_version만", tables <= {"alembic_version"}, str(tables))
upgrade_db(db)
try:
    command.check(alembic_config(db))
    check("V4b 재업그레이드 후 alembic check 차이 없음", True)
except Exception as e:  # noqa: BLE001
    check("V4b 재업그레이드 후 alembic check 차이 없음", False, repr(e))

# V5: 가격 컬럼 타입이 INTEGER
con = sqlite3.connect(db)
price_cols = {
    ("listings_snapshot", "price"), ("trades_snapshot", "price"), ("verdicts", "trade_base"),
    ("verdicts", "listing_base"), ("alert_history", "last_alerted_price"),
}
for t, c in sorted(price_cols):
    typ = {r[1]: r[2] for r in con.execute(f"pragma table_info({t})")}[c]
    check(f"V5 {t}.{c} INTEGER", typ.upper() == "INTEGER", typ)

fails = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
sys.exit(1 if fails else 0)

# Handoff: 알림·리포트 agent (notifier) — 2단계

기준: CLAUDE.md 2026-10-08 개정판 (§1.1 Routine, §4.4, §9, §10). 텔레그램·SMTP 발송·재시도(notify 스킬 §1·§3·§5)는 §9에 따라 구현하지 않았다.
결과물은 HTML 리포트 파일과 `out/summary.md` 두 가지다.

## 파일

| 파일 | 내용 |
|---|---|
| `app/notify/history.py` | `classify_alerts`, `commit_history` (§10), 보조 `prior_alerts` |
| `app/notify/report.py` | `render_report` (§10), 보조 `write_outputs`, `build_view`, `build_title`, `reason_lines`, `pct_below`, `redact` |
| `app/notify/summary.py` | `render_summary` (§10) |
| `app/notify/templates/report.html.j2` | 리포트 템플릿 (인라인 스타일, table 레이아웃, 폭 600px, 외부 CSS·JS·`<style>` 없음) |
| `tests/unit/notify_sample.py` | 고정 입력 (합성 데이터). 스냅샷과 샘플이 같이 쓴다 |
| `tests/unit/test_notify_history.py` | 분류 표 모든 행, 실패 단지, dry_run, 마이그레이션된 DB |
| `tests/unit/test_notify_render.py` | 스냅샷, 섹션 순서, 표시 문구, 첫 줄, 파일 쓰기와 커밋 순서 |
| `tests/fixtures/notify/report_snapshot.html`, `summary_snapshot.md` | 스냅샷 |
| `docs/samples/report_sample.html`, `summary_sample.md` | 샘플 (스냅샷과 같은 내용이며 테스트가 일치를 확인한다) |

스냅샷 갱신: `UPDATE_NOTIFY_SNAPSHOTS=1 .venv/bin/pytest tests/unit/test_notify_render.py` 실행 후 diff를 눈으로 확인한다.

## pipeline이 지킬 호출 순서 (C6-3 조정판)

```python
from app.notify.history import classify_alerts, prior_alerts, commit_history
from app.notify.report import write_outputs, failed_complex_nos

failed = {f["complex_no"] for f in failures}               # 평형 하나라도 실패한 단지는 여기에 넣는다
targets = {e.complex_no for e in load_complexes(settings.complexes_file)}   # 이번 실행 대상 단지 전체 (실패 포함)
verdicts, gone = classify_alerts(session, verdicts, failed, run_id, dry_run, targets)   # DB 읽기만
prior = prior_alerts(session, [v.listing.dedup_key for v in verdicts if v.alert_kind == "PRICE_DROP"])
run = RunResult(..., verdicts=verdicts, ...)
context = {..., "gone": gone, "prior_alerts": prior, "failures": failures}
paths = write_outputs(run, context, report_dir=settings.report_dir, out_dir=settings.out_dir, dry_run=dry_run)
# ↑ 예외 없이 끝난 경우에만 ↓
commit_history(session, run.verdicts, failed, run_id, dry_run=dry_run, target_complex_nos=targets, now=run.started_at)
```

- `classify_alerts`는 dry_run과 관계없이 **DB를 쓰지 않는다**. 이력 갱신은 `commit_history`만 한다.
- `prior_alerts`는 반드시 `commit_history` 전에 부른다. 그 뒤에는 이전가가 현재가로 바뀌어 있다.
- `write_outputs`는 두 파일을 모두 렌더링한 뒤에 쓰기 시작한다. 각 파일은 임시 파일을 만든 뒤 `os.replace`로 바꾼다.
  - dry_run=False이면 `report_dir/YYYY-MM-DD.html`(KST 날짜), dry_run=True이면 `out_dir/YYYY-MM-DD.html`
  - 요약 파일은 항상 `out_dir/summary.md`
- `commit_history(..., dry_run=True)`는 DB를 바꾸지 않고 집계만 돌려준다. `session.commit()`도 부르지 않는다.
- `commit_history`의 `now`는 시간대가 있는 datetime이어야 한다. 없으면 ValueError가 난다.
- 시그니처는 CLAUDE.md §10을 따른다.
  - `classify_alerts(session, verdicts, failed_complex_nos, run_id, dry_run, target_complex_nos)`
  - `commit_history(session, verdicts, failed_complex_nos, run_id, *, dry_run, target_complex_nos, now=None) -> dict`
  - 반환 키: NEW, PRICE_DROP, ONGOING, DEACTIVATED, RETIRED, SKIPPED_FAILED, dry_run
- `target_complex_nos`에 없는 단지(complexes.yaml에서 뺀 단지)의 active 이력은 "내려간 매물"로 표시하지 않는다. `commit_history`가 이 이력을 `active=False`, `deactivated_run_id=run_id`로 조용히 바꾸고 `RETIRED`로 센다. 나중에 그 단지를 다시 넣고 급매가 나오면 NEW로 알린다.
- 수집 실패 단지는 `target_complex_nos`에 없더라도 보호한다. 보호가 우선이다.
- `target_complex_nos`에 없는 단지의 Verdict가 들어오면 ValueError를 낸다.

## context 계약 (render_report / render_summary / write_outputs)

| 키 | 필수 | 타입 | 내용 |
|---|---|---|---|
| `complexes` | 필수 | `list[Complex]` | 조사 대상 단지. 이 순서로 표시한다. fetch_complex가 실패해 Complex가 없는 단지는 빼도 되고, 그러면 `failures`의 name으로 표시한다 |
| `area_types` | 필수 | `dict[complex_no, list[AreaType]]` | |
| `area_summaries` | 필수 | `dict[(complex_no, area_key), dict]` | `rules.area_summary` 결과를 그대로 넣는다. 키 쌍의 area_key는 AreaType.area_key와 같은 float |
| `gone` | 필수 | `list[dict]` | `classify_alerts`의 두 번째 반환값 |
| `failures` | 필수 | `list[dict]` | `{"complex_no": str, "name": str?, "stage": str, "detail": str?, "area_key": float?}`. area_key가 있든 없든 그 단지 전체가 수집 실패로 표시된다 (area_key는 어느 평형에서 실패했는지 적는 데만 쓴다). stage는 CollectorError.stage 값 |
| `prior_alerts` | PRICE_DROP이 있으면 필수 | `dict[dedup_key, dict]` | `prior_alerts()` 결과. 없으면 KeyError |
| `molit_candidates` | 선택 | `dict[complex_no, list[dict]]` | `find_apt_seq_candidates` 결과. 키는 §10에 고정된 `apt_seq`(필수), `apt_nm`, `umd_nm`, `jibun`, `trade_count`, `last_contract`만 쓴다. `score`·`hints`는 표시하지 않는다 |
| `trades` | 선택 | `dict[(complex_no, area_key), list[Trade]]` | 저층 실거래를 참고로 표시할 때 쓴다 (§4.1). 해제 거래를 빼고 가장 최근 1건을 보여 준다 |
| `collector_warnings` | 선택 | `list[str]` | 수집기 경고 (CLAUDE.md §9). 6번 섹션에 표시된다 |
| `complexes_file` | 선택 | `str` | 안내 문구에 쓰는 경로. 기본값은 `config/complexes.yaml` |
| `dry_run` | 선택 | `bool` | `write_outputs`가 덮어쓴다 |
| `report_path` | 선택 | `str` | summary에 표시한다. `write_outputs`가 `"<폴더 이름>/<파일 이름>"`으로 채운다 |

- `RunResult.started_at`은 시간대가 있어야 한다. 리포트 날짜는 이 값의 KST 날짜다.
- `RunResult.verdicts`에는 UNKNOWN 매물을 포함한 모든 대표 매물의 Verdict를 넣는다. 층 미상 목록은 여기서 만든다.
- `alert_kind`가 None인 급매가 있으면 ValueError가 난다 (`classify_alerts`를 빼먹은 경우).

## 출력 규칙

- **summary.md 첫 줄**은 리포트 `<title>`과 같다. 예시는 다음과 같다.
  - `[수집 실패 있음] [급매 리포트] 2026-10-13 (화) — 상태 PARTIAL · 신규 2 · 인하 1 · 지속 1 · 수집 실패 1단지`
  - `[급매 리포트] 2026-10-13 (화) — 상태 OK · 신규 0 · 인하 0 · 지속 0 · 수집 실패 없음`
  - `[수집 실패] [급매 리포트] … — 상태 FAILED · 급매 판정 못 함 (신규 0 · 인하 0) · 수집 실패 2단지`
  - 드라이런이면 맨 앞에 `[드라이런]`을 붙인다.
  - status가 OK가 아닌데 failures가 비어 있으면 `[수집 실패 있음]`과 `수집 오류 있음`을 표시한다.
- 리포트 본문 순서는 notify 스킬 §4의 1~7이며 웹 링크는 뺐다. 수집 실패 배너는 1번 요약 박스의 맨 위에 둔다.
  수집 실패가 있으면 "알림 없음" 문구를 "수집에 성공한 단지에서는 … 수집 실패 단지는 확인하지 못했습니다"로 바꾼다. FAILED이면 "급매를 확인하지 못했습니다"로 쓴다.
- 대상 평형(공급 119.0㎡ 이하)이 0개이고 failures도 없는 단지는 실패가 아니라서 status를 바꾸지 않는다 (CLAUDE.md §9 2026-10-09 ①). 대신 summary의 `## 확인 필요`와 리포트 요약 박스에 `대상 평형 없음: 단지명(번호) — 공급 119.0㎡ 이하 평형이 없어 판정하지 않았습니다`를 표시하고, 현황표에는 "조사 대상 평형이 없습니다"를 표시한다. 그 단지에 failures가 있으면 수집 실패로만 표시한다.
- 판정 근거 문구는 bargain-rules §7 그대로다. 근거별 할인율은 `pct_below`로 계산하며, rules의 `discount_pct`와 같은 정수 ROUND_HALF_UP을 쓴다 (§9 rules ④, 테스트로 일치 확인).
- 가격은 `normalize.format_price`로 표시한다. §7 문구 안의 기준가만 `{base:,}만원` 형식이다.
- 오류·경고 문자열에 있는 `serviceKey`/`api_key`/`token`/`password` 파라미터 값은 `***`로 가린다.
- HTML은 Jinja2 autoescape를 쓴다.
- (2026-10-09 디자인 변경, 템플릿만) 연회색 바탕 + 흰 둥근 카드, 포인트 초록 #03C75A, 급매·하락 빨강 #F04452, 요약 숫자 타일 4개(신규·가격 인하·지속 중·수집 실패 단지), 매물 카드 칩(신규/가격 인하/지속 중/실거래 근거/매물 근거/저층, reasons 문구와 floor_label에서 템플릿이 판단), 현황표는 얇은 구분선만 사용. 모바일(360~420px)에서 가로 스크롤이 없도록 표에 width:100%·table-layout:fixed, 긴 텍스트에 overflow-wrap:anywhere·word-break:break-all, 현황표 글자 11~12px. 데이터·문구·섹션 순서·Python 로직은 그대로다.
- (2026-10-09 디자인 재변경, 템플릿만) 한옥위크 톤으로 교체: 아이보리 바탕 #F3F1EC, 글자 #161514/#5A5753, 포인트 청록 #1C9A93·보조 보라 #6A4FBF·주홍 #E8412C, 섹션마다 장식용 영문 아이브로우(THIS WEEK 등), 큰 날짜 헤더, 요약은 어두운 정보 블록(#161616, 둥근 행 + 숫자 타일 4개), 카드 #FBFAF7·테두리 #E4E0D8·모서리 14px, 수집 실패 = 주홍 배너, 확인 필요 = 황토 배너, 실거래 매칭 확인 필요 = 보라 상자. 모바일 처리(width:100%·table-layout:fixed·overflow-wrap)는 유지.

## alert_history 열 의미 확인 (web-infra 가정 3)

**동의한다. 스키마 변경 요청은 없다.**
- `active=True`이면 지금 급매로 알려진 상태다. PRICE_DROP·ONGOING 비교 대상이다.
- `active=False`이면 급매가 아니게 되었거나 매물이 사라진 상태다. 다시 급매가 되면 NEW로 알린다.
- `deactivated_run_id`는 비활성으로 바뀐 실행이다. "내려간 매물" 목록은 `classify_alerts`가 실행할 때 계산한다 (active이고, 이번 급매가 아니고, 실패 단지가 아닌 행). 따라서 표시는 비활성화되는 그 실행에서 1회만 된다. 이 열은 화면 표시 판단에는 쓰지 않고 감사 기록으로 남긴다. 리포트를 다시 만들 때 "어느 실행에서 내려갔는지"를 찾는 데 쓸 수 있다.
- NEW로 다시 활성화할 때는 `first_alerted_at`도 이번 시각으로 바꾸고 `deactivated_run_id=None`으로 둔다. 새 알림 회차로 보기 때문이다.
- ONGOING이면 `last_seen_run_id`만 바꾼다. 알림가·알림 시각은 그대로 둔다. 가격이 올랐다 내려와도 처음 알린 가격보다 낮아야 PRICE_DROP이다.

## 가정

1. (확정, CLAUDE.md §9 "실패 단위") 수집 실패의 단위는 단지다. pipeline은 평형 하나라도 실패한 단지를 `failed_complex_nos`에 넣고, 그 단지의 Verdict는 넘기지 않는다. 리포트는 방어적으로 처리한다. `failures` 항목이 하나라도 있는 단지는 다음 경우에도 단지 전체를 "수집 실패"로 표시한다: `area_key`가 AreaType과 맞지 않을 때, Complex·AreaType이 없을 때. 표시할 때는 이름(없으면 `단지 {complex_no}`)을 쓰고, 평형 정보가 있으면 `전용 n㎡`로 같이 적는다. 실패 항목이 있는 단지에는 "조사 대상 평형이 없습니다"를 쓰지 않는다. summary의 `## 수집 실패`에는 실패 항목마다 "단지 [평형]: 수집 실패 — 원인" 한 줄을 쓴다.
2. 그래도 실패 단지의 Verdict가 들어오면, 표시용 분류는 하지만 이력은 만들지도 바꾸지도 않는다 (`SKIPPED_FAILED`). 정상 흐름에서는 생기지 않는다.
3. 같은 dedup_key의 Verdict가 두 번 들어오면 ValueError를 낸다. 대표 매물만 와야 하기 때문이다.
4. "내려간 매물"의 `reason`은 두 가지다. `GONE`은 이번 수집에 매물이 없는 경우이고, `NOT_BARGAIN`은 매물은 있지만 급매 조건을 벗어난 경우다. GONE이면 동·층을 알 수 없어서 dedup_key를 작게 표시한다.
5. 신규·가격 인하 목록은 신규를 먼저 두고, 그 안에서는 discount_pct가 큰 순서로 정렬한다. judge 반환 순서에는 의존하지 않는다.
6. 저층 실거래 참고 표시는 `trades`를 줄 때만 나온다.

## 한계 / 미해결

- 미해결 1(설정에서 뺀 단지)과 2(후보 키)는 CLAUDE.md §10 변경으로 해소되어 반영했다.
- 근거별 할인율이 rules와 같은지는 `rules.judge`가 돌려준 `Verdict.discount_pct`와 비교해 확인한다 (`test_reason_pct_matches_judge_discount`). 비공개 함수는 부르지 않는다.
- 샘플·스냅샷은 실제 `normalize.format_price`로 만들었다. 모듈이 없을 때 쓰는 스텁(`notify_sample.stub_format_price`)은 지금 쓰이지 않는다.
- HTML은 브라우저·메일 앱에서 실제로 열어 보지 않았다. 구조 검사(인라인 스타일, 600px, 외부 리소스 없음)만 테스트한다.

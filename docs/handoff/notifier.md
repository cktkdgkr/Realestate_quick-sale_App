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
verdicts, gone = classify_alerts(session, verdicts, failed, run_id, dry_run)   # DB 읽기만
prior = prior_alerts(session, [v.listing.dedup_key for v in verdicts if v.alert_kind == "PRICE_DROP"])
run = RunResult(..., verdicts=verdicts, ...)
context = {..., "gone": gone, "prior_alerts": prior, "failures": failures}
paths = write_outputs(run, context, report_dir=settings.report_dir, out_dir=settings.out_dir, dry_run=dry_run)
# ↑ 예외 없이 끝난 경우에만 ↓
commit_history(session, run.verdicts, failed, run_id, dry_run=dry_run, now=run.started_at)
```

- `classify_alerts`는 dry_run과 관계없이 **DB를 쓰지 않는다**. 이력 갱신은 `commit_history`만 한다.
- `prior_alerts`는 반드시 `commit_history` 전에 부른다. 그 뒤에는 이전가가 현재가로 바뀌어 있다.
- `write_outputs`는 두 파일을 모두 렌더링한 뒤에 쓰기 시작한다. 각 파일은 임시 파일을 만든 뒤 `os.replace`로 바꾼다.
  - dry_run=False이면 `report_dir/YYYY-MM-DD.html`(KST 날짜), dry_run=True이면 `out_dir/YYYY-MM-DD.html`
  - 요약 파일은 항상 `out_dir/summary.md`
- `commit_history(..., dry_run=True)`는 DB를 바꾸지 않고 집계만 돌려준다. `session.commit()`도 부르지 않는다.
- `commit_history`의 `now`는 시간대가 있는 datetime이어야 한다. 없으면 ValueError가 난다.
- 시그니처: `commit_history(session, verdicts, failed_complex_nos, run_id, *, dry_run, now=None) -> dict`.
  §10에는 `commit_history(session, ...)`로만 적혀 있어서 나머지 인자는 이렇게 정했다.

## context 계약 (render_report / render_summary / write_outputs)

| 키 | 필수 | 타입 | 내용 |
|---|---|---|---|
| `complexes` | 필수 | `list[Complex]` | 조사 대상 단지. 이 순서로 표시한다. fetch_complex가 실패해 Complex가 없는 단지는 빼도 되고, 그러면 `failures`의 name으로 표시한다 |
| `area_types` | 필수 | `dict[complex_no, list[AreaType]]` | |
| `area_summaries` | 필수 | `dict[(complex_no, area_key), dict]` | `rules.area_summary` 결과를 그대로 넣는다. 키 쌍의 area_key는 AreaType.area_key와 같은 float |
| `gone` | 필수 | `list[dict]` | `classify_alerts`의 두 번째 반환값 |
| `failures` | 필수 | `list[dict]` | `{"complex_no": str, "name": str?, "stage": str, "detail": str?, "area_key": float?}`. area_key가 없으면 단지 전체 실패, 있으면 그 평형만 실패. stage는 CollectorError.stage 값 |
| `prior_alerts` | PRICE_DROP이 있으면 필수 | `dict[dedup_key, dict]` | `prior_alerts()` 결과. 없으면 KeyError |
| `molit_candidates` | 선택 | `dict[complex_no, list[dict]]` | `find_apt_seq_candidates` 결과. dict의 `aptSeq`(또는 `apt_seq`, `molit_apt_seq`) 값을 식별자로 보여 주고, 나머지 키는 `k=v`로 표시한다 |
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
- 판정 근거 문구는 bargain-rules §7 그대로다. 근거별 할인율은 `pct_below`로 계산하며, rules의 `discount_pct`와 같은 정수 ROUND_HALF_UP을 쓴다 (§9 rules ④, 테스트로 일치 확인).
- 가격은 `normalize.format_price`로 표시한다. §7 문구 안의 기준가만 `{base:,}만원` 형식이다.
- 오류·경고 문자열에 있는 `serviceKey`/`api_key`/`token`/`password` 파라미터 값은 `***`로 가린다.
- HTML은 Jinja2 autoescape를 쓴다.

## alert_history 열 의미 확인 (web-infra 가정 3)

**동의한다. 스키마 변경 요청은 없다.**
- `active=True`이면 지금 급매로 알려진 상태다. PRICE_DROP·ONGOING 비교 대상이다.
- `active=False`이면 급매가 아니게 되었거나 매물이 사라진 상태다. 다시 급매가 되면 NEW로 알린다.
- `deactivated_run_id`는 비활성으로 바뀐 실행이다. "내려간 매물" 목록은 `classify_alerts`가 실행할 때 계산한다 (active이고, 이번 급매가 아니고, 실패 단지가 아닌 행). 따라서 표시는 비활성화되는 그 실행에서 1회만 된다. 이 열은 화면 표시 판단에는 쓰지 않고 감사 기록으로 남긴다. 리포트를 다시 만들 때 "어느 실행에서 내려갔는지"를 찾는 데 쓸 수 있다.
- NEW로 다시 활성화할 때는 `first_alerted_at`도 이번 시각으로 바꾸고 `deactivated_run_id=None`으로 둔다. 새 알림 회차로 보기 때문이다.
- ONGOING이면 `last_seen_run_id`만 바꾼다. 알림가·알림 시각은 그대로 둔다. 가격이 올랐다 내려와도 처음 알린 가격보다 낮아야 PRICE_DROP이다.

## 가정

1. 단지 단위 실패만 이력 보호 대상이다. 평형 하나만 실패해도 pipeline이 그 단지를 `failed_complex_nos`에 넣어야 그 단지의 이력이 보호된다. 리포트에서는 `failures`의 `area_key`로 평형 단위 실패도 표시할 수 있다.
2. 실패 단지에 Verdict가 일부 들어와도 분류(표시)는 하지만 이력은 만들지도 바꾸지도 않는다 (`SKIPPED_FAILED`).
3. 같은 dedup_key의 Verdict가 두 번 들어오면 ValueError를 낸다. 대표 매물만 와야 하기 때문이다.
4. "내려간 매물"의 `reason`은 두 가지다. `GONE`은 이번 수집에 매물이 없는 경우이고, `NOT_BARGAIN`은 매물은 있지만 급매 조건을 벗어난 경우다. GONE이면 동·층을 알 수 없어서 dedup_key를 작게 표시한다.
5. 신규·가격 인하 목록은 신규를 먼저 두고, 그 안에서는 discount_pct가 큰 순서로 정렬한다. judge 반환 순서에는 의존하지 않는다.
6. 저층 실거래 참고 표시는 `trades`를 줄 때만 나온다.

## 한계 / 미해결

- **설정에서 뺀 단지의 이력**: `classify_alerts` 시그니처(§10)에는 "이번 실행 대상 단지" 정보가 없다. 그래서 complexes.yaml에서 단지를 빼면 그 단지의 active 이력이 다음 실행에서 "내려간 매물(GONE)"로 1회 표시되고 비활성화된다. 오알림은 아니지만 문구가 정확하지 않다. 해결하려면 Orchestrator가 결정해야 한다. 하나는 시그니처에 `target_complex_nos`를 추가하는 방법이고, 다른 하나는 pipeline이 대상에서 빠진 단지를 `failed_complex_nos`에 넣는 방법이다 (후자는 이력이 영구히 active로 남는다).
- `molit_candidates` dict의 키 이름은 trade-collector 구현에 맞춰 확인해야 한다. 지금은 `aptSeq`/`apt_seq`/`molit_apt_seq`를 식별자로 인식한다.
- 렌더링 테스트 `test_pct_matches_rules_discount`는 반올림이 같은지 확인하려고 `app.domain.rules._discount_pct`(비공개)를 직접 부른다. 모듈 코드는 §10 함수만 쓴다.
- 샘플·스냅샷은 실제 `normalize.format_price`로 만들었다. 모듈이 없을 때 쓰는 스텁(`notify_sample.stub_format_price`)은 지금 쓰이지 않는다.
- HTML은 브라우저·메일 앱에서 실제로 열어 보지 않았다. 구조 검사(인라인 스타일, 600px, 외부 리소스 없음)만 테스트한다.

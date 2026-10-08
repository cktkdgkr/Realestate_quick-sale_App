---
name: schedule-deploy
description: 매주 화요일 10:00 Asia/Seoul 스케줄 설정, 중복 실행 방지, Docker Compose 배포, 비밀값 관리, 헬스체크와 장애 대응 문서 작성 규칙. 스케줄러·배포·운영 문서를 작성하거나 검증할 때 읽는다.
---

# 스케줄과 배포

## 1. 스케줄
```python
CronTrigger(day_of_week="tue", hour=10, minute=0, timezone=ZoneInfo("Asia/Seoul"))
```
- 서버 OS 시간대와 무관하게 동작해야 한다. 서버가 UTC여도 KST 10:00에 돈다는 것을 테스트로 확인한다 (`trigger.get_next_fire_time`에 고정 시각 주입).
- `max_instances=1`, `coalesce=True`
- `misfire_grace_time=6*3600`: 서버가 잠시 꺼져 있다가 같은 날 16시 전에 켜지면 1회 실행. 그 이후면 건너뛰고 텔레그램으로 "이번 주 실행을 놓쳤습니다 — 웹에서 수동 실행 가능" 알림.
- 웹 화면의 수동 실행과 스케줄 실행이 겹치지 않도록 DB 기반 락(`runs` 테이블에 RUNNING 상태 확인)을 둔다. 2시간 넘은 RUNNING은 비정상 종료로 보고 해제한다.

## 2. 배포 구성
- 서버: 1 vCPU / 1GB RAM급 VM이면 충분 (예: Oracle Cloud 무료 인스턴스, AWS Lightsail, GCP e2-micro). 한국 리전 권장.
- `docker-compose.yml`: 서비스 1개(app: 웹 + 스케줄러 같은 프로세스), 볼륨 `./data:/app/data` (SQLite), `restart: unless-stopped`, `env_file: .env`, `TZ=Asia/Seoul`
- 웹 앞단은 Caddy로 HTTPS 자동 인증서 (도메인이 있을 때). 도메인이 없으면 SSH 터널이나 IP+포트로 접속하고 문서에 명시.
- 웹 인증(`WEB_USERNAME`, `WEB_PASSWORD`) 없이 기동되면 시작을 거부하도록 한다.

## 3. 비밀값
- `.env`는 `.gitignore`에 포함. 저장소에는 `.env.example`만.
- 로그 포맷터에서 토큰·키·비밀번호 패턴을 마스킹한다.
- 사용자가 대화창에 비밀값을 붙여넣지 않도록, 문서에는 "서버에서 `.env` 파일을 직접 편집"하는 방법으로 안내한다.

## 4. 헬스체크와 관측
- `GET /healthz`: DB 연결, 스케줄러 동작, 다음 실행 시각 반환
- 실행 기록: `runs` 테이블 (시작·종료 시각, 상태, 단지별 결과, 오류 메시지)
- 로그: 표준출력 JSON 한 줄 로그, Docker 로그 로테이션(`max-size: 10m`, `max-file: 3`)
- DB 백업: 매주 실행 직후 SQLite 파일을 `data/backup/`에 날짜별 복사, 8주 보관

## 5. 문서
**docs/USER_GUIDE.md** (비개발자 기준, 스크린샷 자리 표시 포함)
- 웹 접속, 단지 등록·삭제, "실거래 매칭 확인 필요" 처리, 수동 실행, 결과 보는 법, 알림 해석법

**docs/RUNBOOK.md** (증상 → 원인 → 조치)
- "수집 실패 (blocked)" 알림 → 한두 주 기다려 보기, 계속되면 수집 방식 점검
- "schema_changed" → 네이버 구조 변경. naver-land-collector §1 절차로 재확인 후 수정
- 국토부 API 인증 오류 → 키 만료·재발급
- 메일이 안 옴 → 앱 비밀번호, 스팸함 확인
- 서버 재시작 후 → `docker compose up -d`, `/healthz` 확인

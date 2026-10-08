---
name: notify-telegram-email
description: 텔레그램 봇 알림과 이메일 주간 리포트의 설정 방법, 메시지·리포트 구성 템플릿, 재알림 이력 분류(NEW/PRICE_DROP/ONGOING), 발송 실패 처리 규칙. 알림·리포트 코드를 작성하거나 결과물을 검증할 때 읽는다.
---

# 텔레그램 알림과 이메일 리포트

## 1. 설정 (사용자 안내용)
**텔레그램**
1. 텔레그램에서 `@BotFather` → `/newbot` → 봇 토큰 발급 → `TELEGRAM_BOT_TOKEN`
2. 만든 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates`에서 `chat.id` 확인 → `TELEGRAM_CHAT_ID`
3. 발송: `POST https://api.telegram.org/bot<토큰>/sendMessage` (`chat_id`, `text`, `parse_mode=HTML`, `disable_web_page_preview=true`)

**이메일 (Gmail 예시)**
1. Google 계정 2단계 인증 켜기 → 앱 비밀번호 생성
2. `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587` (STARTTLS), `SMTP_USER`, `SMTP_PASSWORD`=앱 비밀번호, `EMAIL_TO`=수신 주소

## 2. 재알림 분류 `classify_alerts`
이력 테이블 `alert_history(dedup_key PK, first_alerted_at, last_alerted_price, last_seen_run_id, active)`

| 이번 판정 | 이력 상태 | alert_kind | 텔레그램 | 이력 갱신 |
|---|---|---|---|---|
| 급매 | 없음 또는 active=False | NEW | 보냄 | 생성/active=True, last_alerted_price=현재가 |
| 급매 | active, 현재가 < last_alerted_price | PRICE_DROP | 보냄 | last_alerted_price=현재가 |
| 급매 | active, 현재가 ≥ last_alerted_price | ONGOING | 안 보냄 | last_seen만 갱신 |
| 급매 아님 또는 매물 없음 | active | — | 안 보냄 | active=False, 이메일 "내려간 급매"에 1회 표시 |

- **수집 실패한 단지의 이력은 건드리지 않는다** (실패를 "매물 사라짐"으로 오인하면 다음 주에 같은 급매가 NEW로 다시 울린다).
- 텔레그램·이메일 발송이 **성공한 뒤에만** 이력을 커밋한다. 발송 실패 시 다음 실행에서 다시 NEW로 잡히게 한다.

## 3. 텔레그램 메시지
NEW·PRICE_DROP 급매마다 한 블록. 4096자를 넘으면 블록 경계에서 나눠 여러 메시지로 보낸다.
```
🔥 [신규 급매] 래미안OO 34평(84.97㎡) 101동 저층
매매 8억 9,000 (일반층 실거래 중앙값 10억 대비 11.0%↓)
근거: 실거래·매물 | 중개사 2곳
https://new.land.naver.com/...
```
- PRICE_DROP은 `📉 [가격 인하] … 9억 2,000 → 8억 9,000`
- 수집 실패가 있으면 **별도 메시지**: `⚠️ 수집 실패: 래미안OO (네이버 접속 차단 의심). 이번 주 결과에서 이 단지는 빠졌습니다.`
- 급매 0건이고 실패도 없으면 텔레그램은 보내지 않는다 (이메일만).

## 4. 이메일 리포트
제목: `[급매 리포트] 2026-10-13 (화) — 신규 2 · 인하 1 · 지속 3` (실패 시 앞에 `[수집 실패 있음]`)

본문 순서:
1. **요약 박스**: 실행 상태, 조사 단지 수, 신규/인하/지속 건수, 실패 단지
2. **신규·가격 인하 급매 표**: 단지, 평형, 동·층, 가격, 기준가와 할인율, 근거 문구(bargain-rules §7), 링크
3. **지속 중인 급매 표**
4. **지난주 급매 중 내려간 매물** (있으면)
5. **단지·평형별 현황 표**: 평형마다 [일반층 실거래 중앙값(건수), 일반층 매물 최저가, 저층 매물 최저가, 매물 수], "실거래 부족"·"층 미상 n건" 표시
6. **교차검증 경고·수집 오류** (있으면)
7. 바닥글: 데이터 출처(국토부 실거래, 네이버 부동산), 실거래 신고 지연 안내, 웹 화면 링크

- HTML은 이메일 클라이언트 호환을 위해 인라인 스타일과 table 레이아웃만 쓴다. 모바일에서 읽히도록 폭 600px 기준.
- 같은 내용의 text/plain 버전도 함께 보낸다.
- 가격 표시는 `8억 9,000` 형식 (만원 정수 → 표시 문자열 함수 하나로 통일).

## 5. 발송 실패
- 각 채널 3회 재시도 (5s, 15s, 45s).
- 한 채널이 최종 실패하면 다른 채널로 실패 사실을 알린다 (예: 이메일 실패 → 텔레그램에 "이메일 발송 실패").
- 둘 다 실패하면 실행 상태를 FAILED로 기록하고 웹 화면에 표시한다.

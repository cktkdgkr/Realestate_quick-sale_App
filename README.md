# 네이버 부동산 급매 알림 앱 — 다중 Agent 개발 프롬프트 세트

이 폴더는 **앱을 만들어 주는 AI 개발팀**의 설정 묶음입니다. 앱 코드는 들어 있지 않고, Claude Code가 이 설정을 읽고 앱을 만듭니다.

## 사용 방법
1. 이 폴더를 원하는 위치에 풀고, 그 폴더에서 Claude Code를 실행합니다.
2. `START_PROMPT.md`의 `---` 사이 내용을 첫 메시지로 붙여넣습니다.
3. Orchestrator가 먼저 몇 가지를 물어봅니다 (테스트할 단지, API 키 준비 여부 등). 답하면 개발이 시작됩니다.
4. API 키·토큰·비밀번호는 대화창에 붙여넣지 말고, 안내에 따라 `.env` 파일에 직접 넣으세요.

## 구성
```
CLAUDE.md                 모든 agent가 따르는 프로젝트 명세 (급매 규칙, 데이터 정의, 금지 사항)
START_PROMPT.md           Orchestrator 시작 프롬프트 (진행 단계, 검증 루프 규칙)
.claude/agents/           팀원 6명
  listing-collector.md      매물 조사
  trade-collector.md        실거래 조사
  bargain-judge.md          급매 판정
  notifier.md               알림·리포트
  web-infra.md              웹·인프라
  verifier.md               검증 (독립)
.claude/skills/           작업 지침 8개
  naver-land-collector      네이버 수집 방법·요청 정책
  floor-area-normalizer     층 그룹·면적·가격 변환 규칙과 테스트표
  listing-dedup             중복 매물 묶기
  molit-trade-api           국토부 실거래 API
  bargain-rules             급매 판정 정식 명세 + 기준 테스트 21개
  notify-telegram-email     알림·리포트 구성, 재알림 규칙
  schedule-deploy           화요일 10시 KST 스케줄, 배포, 운영 문서
  verification-checklist    모듈별 합격 기준, 실데이터 대조 절차
```

## 규칙을 바꾸고 싶을 때
급매 기준(95%, 90%), 저층 정의, 실거래 기간 등은 `CLAUDE.md` §3~§4와 `bargain-rules` 스킬의 테스트표를 함께 고친 뒤 Orchestrator에게 "명세가 바뀌었다"고 알려 주면 됩니다.

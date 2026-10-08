"""운영 문서 확인 (C7-4 조정판: cron, C7-7 조정판: RUNBOOK 증상)."""

from app.config import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs"


def test_routine_cron_is_tuesday_10_kst() -> None:
    text = (DOCS / "ROUTINE.md").read_text(encoding="utf-8")
    assert "CRON_TZ=Asia/Seoul 0 10 * * 2" in text
    for needed in ("new.land.naver.com", "apis.data.go.kr", "MOLIT_API_KEY", "reports", "사용량"):
        assert needed in text, needed


def test_runbook_covers_symptoms() -> None:
    text = (DOCS / "RUNBOOK.md").read_text(encoding="utf-8")
    for symptom in ("blocked", "schema_changed", "국토부 API 인증", "Routine 실행 실패", "reports 브랜치 푸시 실패"):
        assert symptom in text, symptom


def test_user_guide_sections() -> None:
    text = (DOCS / "USER_GUIDE.md").read_text(encoding="utf-8")
    for needed in ("config/complexes.yaml", "실거래 매칭 확인 필요", "molit_apt_seq", "--dry-run", "리포트"):
        assert needed in text, needed

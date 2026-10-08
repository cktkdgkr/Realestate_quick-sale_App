"""수집기 공용 예외. 소유: listing-collector (CLAUDE.md §10).

수집 실패는 빈 결과로 숨기지 않고 이 예외로 올린다 (CLAUDE.md §7, 검증 C0-6).
"""

from __future__ import annotations

STAGES = frozenset(
    {"blocked", "schema_changed", "network", "molit_api", "molit_auth", "complex_mapping"}
)


class CollectorError(Exception):
    """수집 단계 실패.

    stage: "blocked" | "schema_changed" | "network" | "molit_api" | "molit_auth" | "complex_mapping"
    detail: 사람이 읽을 설명. 토큰·API 키 등 비밀값을 넣지 않는다.
    complex_no: 실패한 단지 번호 (단지와 무관한 실패면 None)
    """

    def __init__(self, stage: str, detail: str = "", complex_no: str | None = None) -> None:
        if stage not in STAGES:
            raise ValueError(f"알 수 없는 CollectorError stage: {stage!r}")
        self.stage = stage
        self.detail = detail
        self.complex_no = complex_no
        super().__init__(str(self))

    def __str__(self) -> str:
        where = f"[{self.complex_no}] " if self.complex_no else ""
        return f"{where}{self.stage}: {self.detail}" if self.detail else f"{where}{self.stage}"

    def __reduce__(self):  # pickle·copy 지원 (키워드 인자 생성자)
        return (self.__class__, (self.stage, self.detail, self.complex_no))

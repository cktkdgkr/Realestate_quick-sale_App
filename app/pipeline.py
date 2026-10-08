"""주간 파이프라인 진입점: `python -m app.pipeline --once`.

2단계에서 구현한다 (웹·인프라 agent + Orchestrator). 1단계에서는 실행 시 명시적으로 실패한다.
아무것도 하지 않고 성공(exit 0)으로 끝나면 "급매 없음"으로 오인될 수 있으므로 일부러 오류를 낸다 (CLAUDE.md §7).
"""

import sys


def main(argv: list[str] | None = None) -> int:
    print("app.pipeline: 아직 구현되지 않았습니다 (2단계 작업).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

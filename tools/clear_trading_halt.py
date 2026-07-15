r"""전역 거래 HALT 명시적 운영자 해제 도구.

사용:
    .venv\Scripts\python.exe tools/clear_trading_halt.py \
        --confirm --reason "증권사 장애 해소 및 잔고 대조 완료"

HALT는 긴급 청산 후 자동 해제되지 않는다. 이 도구는 --confirm과
빈 값이 아닌 --reason을 모두 요구하며, 해제 사유와 이전 HALT를
OperationEvent append-only 감사 로그에 남긴다.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="전역 거래 HALT 해제 (명시적 운영자 확인 필수)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="BUY 차단 해제의 위험을 확인한다",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="해제 근거(장애 해소, 잔고/미체결 대조 등)",
    )
    args = parser.parse_args(argv)

    reason = str(args.reason or "").strip()
    if not args.confirm:
        logger.critical("HALT 해제 거부: --confirm이 필요합니다")
        return 2
    if not reason:
        logger.critical("HALT 해제 거부: --reason은 빈 값일 수 없습니다")
        return 2

    from database.models import init_database
    from database.repositories import clear_trading_halt, get_trading_halt_state

    init_database()
    try:
        previous = get_trading_halt_state()
        cleared = clear_trading_halt(
            reason,
            source="tools.clear_trading_halt",
            mode="live",
            confirmed=True,
            expected_active_event_id=previous.get("event_id"),
            detail={
                "previous_halted": bool(previous.get("halted", False)),
                "previous_event_id": previous.get("event_id"),
                "previous_reason": previous.get("reason", ""),
            },
        )
        verified = get_trading_halt_state()
    except Exception as exc:
        logger.exception("HALT 해제 실패 — BUY 차단 상태를 유지하세요: {}", exc)
        return 1

    if verified.get("halted", True):
        logger.critical(
            "HALT 해제 검증 실패 (event_id={}) — BUY 차단 유지",
            verified.get("event_id"),
        )
        return 1

    logger.warning(
        "전역 거래 HALT 해제 완료 (event_id={}, previous_event_id={}) reason={}",
        cleared.get("event_id"),
        previous.get("event_id"),
        reason,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

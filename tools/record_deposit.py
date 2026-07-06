"""적립 입금 기록 CLI — 계정 현금에 외부 입금을 반영한다 (docs/POCKET_TRACK_PLAN.md §4).

입금은 수익이 아니다: 이 기록이 있어야 TWR 수익률이 입금 구간을 분리해 계산한다.
paper는 이 도구가 곧 입금이고, live는 증권사 실입금 후 같은 금액을 여기 기록해야
수익률이 맞는다. 다음 리밸런싱 사이클이 새 현금을 목표 비중대로 흡수한다.

사용:
    .venv\\Scripts\\python.exe tools/record_deposit.py --basket kr_pocket --amount 100000
    .venv\\Scripts\\python.exe tools/record_deposit.py --basket kr_pocket --amount 100000 --note "7월 적립"
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="적립 입금 기록 (paper: 기록=입금, live: 실입금 후 기록)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--basket", type=str, help="바스켓 이름 (계정 키 자동 해석)")
    group.add_argument("--account-key", type=str, help="계정 키 직접 지정")
    parser.add_argument("--amount", type=float, required=True, help="입금액(원, 양수)")
    parser.add_argument("--note", type=str, default="", help="메모 (예: 7월 적립)")
    args = parser.parse_args()

    if args.amount <= 0:
        logger.error("입금액은 양수여야 합니다: {}", args.amount)
        return 1

    from config.config_loader import Config
    from database.models import init_database
    from database.repositories import (
        get_cash_flow_total,
        get_latest_snapshot_summary,
        record_cash_flow,
    )

    init_database()
    config = Config.get()

    if args.basket:
        from core.basket_rebalancer import BasketRebalancer, rebalance_live_strategy_id
        baskets = BasketRebalancer._load_baskets_config()
        if args.basket not in baskets:
            logger.error("바스켓 '{}' 설정 없음 (config/baskets.yaml)", args.basket)
            return 1
        account_key = rebalance_live_strategy_id(args.basket)
        basket_capital = (baskets.get(args.basket) or {}).get("initial_capital")
    else:
        account_key = args.account_key
        basket_capital = None

    now = datetime.now()
    # 과거 소급 금지: 마지막 스냅샷 이전 시각의 입금은 TWR 체인이 중화하지 못해
    # (그 구간 수익률이 이미 확정됨) 수익률이 왜곡된다 — fail-closed.
    prev = get_latest_snapshot_summary(account_key=account_key)
    if prev is not None:
        boundary = prev.get("created_at") or prev.get("date")
        if boundary is not None and now <= boundary:
            logger.error(
                "입금 시각({})이 마지막 스냅샷({}) 이전입니다 — 과거 소급 기록은 지원하지 않습니다.",
                now, boundary,
            )
            return 1

    mode = str(config.trading.get("mode", "paper")).lower()
    flow_id = record_cash_flow(
        amount=float(args.amount),
        account_key=account_key,
        occurred_at=now,
        note=args.note,
        mode=mode,
    )

    deposits = get_cash_flow_total(account_key=account_key)
    if basket_capital is not None:
        # 바스켓 문맥이 있어야 초기자본을 안다 — 원금(초기+입금)까지 표기.
        logger.info(
            "입금 기록 완료 (id={}): {} +{:,.0f}원 — 누적 입금 {:,.0f}원, 누적 원금 {:,.0f}원",
            flow_id, account_key, args.amount, deposits, float(basket_capital) + deposits,
        )
    else:
        logger.info(
            "입금 기록 완료 (id={}): {} +{:,.0f}원 — 누적 입금 {:,.0f}원",
            flow_id, account_key, args.amount, deposits,
        )
    logger.info("다음 리밸런싱 사이클이 새 현금을 목표 비중대로 흡수합니다 (회전 상한 내).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

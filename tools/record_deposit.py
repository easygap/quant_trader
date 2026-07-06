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


def record_basket_deposit(basket_name: str, amount: float, note: str = "") -> dict:
    """바스켓 적립 입금 기록 — CLI와 웹 대시보드가 공유하는 단일 검증·기록 경로.

    검증: 금액 양수, 바스켓 존재, 과거 소급 금지(마지막 스냅샷 이후만 — TWR 체인 보호).
    occurred_at은 항상 now(서버 시각) — 웹에서 소급 조작 불가.
    반환: {ok, error?, account_key, flow_id?, deposits_total?, principal?}
    """
    from config.config_loader import Config
    from core.basket_rebalancer import BasketRebalancer, rebalance_live_strategy_id
    from database.models import init_database
    from database.repositories import (
        get_cash_flow_total,
        get_latest_snapshot_summary,
        record_cash_flow,
    )

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "금액이 숫자가 아닙니다"}
    if amount <= 0:
        return {"ok": False, "error": "입금액은 양수여야 합니다"}

    init_database()
    config = Config.get()
    baskets = BasketRebalancer._load_baskets_config()
    if basket_name not in baskets:
        return {"ok": False, "error": f"바스켓 '{basket_name}' 설정 없음"}
    account_key = rebalance_live_strategy_id(basket_name)
    basket_capital = (baskets.get(basket_name) or {}).get("initial_capital")

    now = datetime.now()
    prev = get_latest_snapshot_summary(account_key=account_key)
    if prev is not None:
        boundary = prev.get("created_at") or prev.get("date")
        if boundary is not None and now <= boundary:
            return {"ok": False, "error": "마지막 스냅샷 이전 시각 — 과거 소급 기록 불가"}

    mode = str(config.trading.get("mode", "paper")).lower()
    flow_id = record_cash_flow(
        amount=amount, account_key=account_key, occurred_at=now,
        note=note or "", mode=mode,
    )
    deposits = get_cash_flow_total(account_key=account_key)
    out = {
        "ok": True, "account_key": account_key, "flow_id": int(flow_id),
        "amount": amount, "deposits_total": float(deposits),
    }
    if basket_capital is not None:
        out["principal"] = float(basket_capital) + float(deposits)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="적립 입금 기록 (paper: 기록=입금, live: 실입금 후 기록)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--basket", type=str, help="바스켓 이름 (계정 키 자동 해석)")
    group.add_argument("--account-key", type=str, help="계정 키 직접 지정")
    parser.add_argument("--amount", type=float, required=True, help="입금액(원, 양수)")
    parser.add_argument("--note", type=str, default="", help="메모 (예: 7월 적립)")
    args = parser.parse_args()

    if args.basket:
        # 웹 대시보드와 동일한 단일 검증·기록 경로.
        result = record_basket_deposit(args.basket, args.amount, note=args.note)
        if not result.get("ok"):
            logger.error("입금 기록 실패: {}", result.get("error"))
            return 1
        if result.get("principal") is not None:
            logger.info(
                "입금 기록 완료 (id={}): {} +{:,.0f}원 — 누적 입금 {:,.0f}원, 누적 원금 {:,.0f}원",
                result["flow_id"], result["account_key"], args.amount,
                result["deposits_total"], result["principal"],
            )
        else:
            logger.info(
                "입금 기록 완료 (id={}): {} +{:,.0f}원 — 누적 입금 {:,.0f}원",
                result["flow_id"], result["account_key"], args.amount, result["deposits_total"],
            )
        logger.info("다음 리밸런싱 사이클이 새 현금을 목표 비중대로 흡수합니다 (회전 상한 내).")
        return 0

    # --account-key 직접 지정 경로 (바스켓 문맥 없음 — 원금 표기 생략)
    if args.amount <= 0:
        logger.error("입금액은 양수여야 합니다: {}", args.amount)
        return 1

    from database.models import init_database
    from database.repositories import (
        get_cash_flow_total,
        get_latest_snapshot_summary,
        record_cash_flow,
    )
    from config.config_loader import Config

    init_database()
    config = Config.get()
    account_key = args.account_key

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
        amount=float(args.amount), account_key=account_key,
        occurred_at=now, note=args.note, mode=mode,
    )
    deposits = get_cash_flow_total(account_key=account_key)
    logger.info(
        "입금 기록 완료 (id={}): {} +{:,.0f}원 — 누적 입금 {:,.0f}원",
        flow_id, account_key, args.amount, deposits,
    )
    logger.info("다음 리밸런싱 사이클이 새 현금을 목표 비중대로 흡수합니다 (회전 상한 내).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

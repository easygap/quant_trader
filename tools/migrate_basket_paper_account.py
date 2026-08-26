"""바스켓 paper 기록을 기본 계정('')에서 바스켓 전용 키로 이전하는 일회성 마이그레이션.

배경: 초기 paper 운영(2026-06-10 개시 직후)은 account_key=''(기본 계정),
strategy='basket_rebalance'(이름 없음)로 기록했다. 기본 계정은 전 계정 합산 뷰라
다른 전략 거래와 섞일 수 있고, 이름 없는 strategy는 바스켓별 귀속이 불가능하다.
이 도구는 그 기록을 basket_rebalance:<name> 키로 옮긴다.

전제: '' 계정의 basket_rebalance 기록이 전부 지정한 단일 바스켓의 것일 때만
사용한다(운영 초기 단일 바스켓 상황). 복수 바스켓이 섞인 뒤에는 귀속을 복원할
수 없으므로 사용 금지.

사용:
    .venv\\Scripts\\python.exe tools/migrate_basket_paper_account.py --basket kr_diversified_hold          # dry-run
    .venv\\Scripts\\python.exe tools/migrate_basket_paper_account.py --basket kr_diversified_hold --apply  # 실제 적용
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="바스켓 paper 기록 계정 키 마이그레이션")
    parser.add_argument("--basket", required=True, help="귀속할 바스켓 이름")
    parser.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    args = parser.parse_args()

    from core.basket_rebalancer import rebalance_live_strategy_id
    from database.models import (
        OrderRecord,
        PortfolioSnapshot,
        Position,
        TradeHistory,
        get_session,
        init_database,
    )

    init_database()
    key = rebalance_live_strategy_id(args.basket)
    session = get_session()
    try:
        trades = (
            session.query(TradeHistory)
            .filter(TradeHistory.account_key == "")
            .filter(TradeHistory.strategy == "basket_rebalance")
            .filter(TradeHistory.mode == "paper")
            .all()
        )
        positions = (
            session.query(Position)
            .filter(
                Position.mode == "paper",
                Position.account_key == "",
                Position.strategy == "basket_rebalance",
            )
            .all()
        )
        snaps = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.mode == "paper",
                PortfolioSnapshot.account_key == "",
            )
            .all()
        )
        orders = (
            session.query(OrderRecord)
            .filter(OrderRecord.account_key == "")
            .filter(OrderRecord.strategy == "basket_rebalance")
            .all()
        )

        logger.info(
            "대상: 거래 {}건, 포지션 {}건, 스냅샷 {}건, 주문레코드 {}건 → '{}'",
            len(trades), len(positions), len(snaps), len(orders), key,
        )

        if not args.apply:
            logger.info("dry-run — 변경 없음. 적용하려면 --apply")
            return 0

        for t in trades:
            t.account_key = key
            t.strategy = key
        for p in positions:
            p.account_key = key
            p.strategy = key
        for s in snaps:
            s.account_key = key
        for o in orders:
            o.account_key = key
            o.strategy = key
        session.commit()
        logger.info("마이그레이션 완료 → account_key/strategy = '{}'", key)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

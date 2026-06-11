"""바스켓 paper 트랙레코드 재시작 — 기존 기록을 아카이브 키로 이전(비파괴).

용도: 운영 중 자본·구성 변경(예: baskets.yaml initial_capital 레버) 시 NAV
시계열에 인위적 점프가 생기므로, 기존 기록을 아카이브하고 깨끗한 시계열로
재시작한다(docs/BASKET_PAPER_EVALUATION.md 운영자 레버 권장 절차). 삭제가
아니라 account_key/strategy를 `<키>@archived-<날짜>`로 바꾸는 이전이라 가역적
이고, 아카이브된 기록은 평가·게이트(정확한 키 매칭)에서 자연히 제외된다.

A안(자본 증액) 실행 절차:
  1) baskets.yaml 해당 바스켓에 initial_capital: 30000000 추가
  2) .venv\\Scripts\\python.exe tools/restart_basket_track_record.py --basket kr_diversified_hold          # dry-run
  3) 같은 명령에 --apply — 기존 기록 아카이브
  4) 다음 일일 사이클부터 새 자본으로 초기 매입·새 시계열 시작 (진행률 0/60 재시작)

되돌리기: --undo --archive-suffix <적용 시 출력된 suffix> --apply
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="바스켓 paper 트랙레코드 재시작(아카이브 이전)")
    parser.add_argument("--basket", required=True, help="대상 바스켓 이름")
    parser.add_argument("--archive-suffix", default=None,
                        help="아카이브 suffix (기본: archived-<오늘>). --undo 시 필수")
    parser.add_argument("--undo", action="store_true",
                        help="아카이브 키의 기록을 원래 키로 되돌린다")
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
    live_key = rebalance_live_strategy_id(args.basket)
    suffix = args.archive_suffix or f"archived-{datetime.now().strftime('%Y%m%d')}"
    archive_key = f"{live_key}@{suffix}"

    if args.undo:
        if not args.archive_suffix:
            logger.error("--undo에는 --archive-suffix가 필요합니다 (적용 시 출력된 값)")
            return 1
        src_key, dst_key = archive_key, live_key
    else:
        src_key, dst_key = live_key, archive_key

    session = get_session()
    try:
        # 되돌리기 대상 키에 이미 살아있는 기록이 있으면 섞임 — fail-closed
        if args.undo:
            existing = (
                session.query(TradeHistory)
                .filter(TradeHistory.account_key == dst_key)
                .count()
            )
            if existing:
                logger.error(
                    "되돌리기 중단: '{}' 키에 살아있는 거래 {}건 — 섞이면 귀속 복원 불가",
                    dst_key, existing,
                )
                return 1

        moves = []
        for model, has_strategy in [
            (TradeHistory, True), (Position, True),
            (PortfolioSnapshot, False), (OrderRecord, True),
        ]:
            rows = (
                session.query(model)
                .filter(model.account_key == src_key)
                .all()
            )
            moves.append((model.__tablename__, rows, has_strategy))

        logger.info("이전 계획: '{}' → '{}'", src_key, dst_key)
        for table, rows, _ in moves:
            logger.info("  {}: {}건", table, len(rows))

        if not args.apply:
            logger.info("dry-run — 변경 없음. 적용하려면 --apply")
            return 0

        for _, rows, has_strategy in moves:
            for r in rows:
                r.account_key = dst_key
                if has_strategy and getattr(r, "strategy", None) == src_key:
                    r.strategy = dst_key
        session.commit()
        logger.info("이전 완료 → '{}'", dst_key)
        if not args.undo:
            logger.info("되돌리기: --undo --archive-suffix {} --apply", suffix)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

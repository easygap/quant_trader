"""바스켓 paper 운영 평가 CLI — DB에서 운영 데이터를 수집해 승격 판정을 출력한다.

사용:
    .venv\\Scripts\\python.exe tools/basket_paper_evaluation.py
    .venv\\Scripts\\python.exe tools/basket_paper_evaluation.py --min-days 60 --out reports/basket_eval.md

판정 로직과 기준 근거는 core/basket_evaluation.py 및 docs/BASKET_PAPER_EVALUATION.md 참고.
데이터가 쌓이는 즉시(매일) 실행해도 안전하다 — 기간 미충족이면 WAIT + 진행률을 보여준다.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def _collect(min_days: int) -> tuple[dict, str]:
    """DB·설정에서 평가 입력을 수집해 (평가결과, 바스켓이름) 반환."""
    from config.config_loader import Config
    from core.basket_evaluation import evaluate_basket_paper_operation
    from core.basket_rebalancer import BasketRebalancer
    from core.trading_hours import TradingHours
    from database.models import get_session, PortfolioSnapshot, TradeHistory, init_database
    from database.repositories import get_pending_failed_orders

    init_database()
    config = Config.get()
    enabled = BasketRebalancer.get_enabled_baskets()
    basket_label = ",".join(enabled) if enabled else "(enabled 바스켓 없음)"

    session = get_session()
    try:
        trades = (
            session.query(TradeHistory)
            .filter(TradeHistory.strategy.like("basket_rebalance%"))
            .filter(TradeHistory.mode == "paper")
            .all()
        )
        snaps = (
            session.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.account_key == "")
            .order_by(PortfolioSnapshot.date.asc())
            .all()
        )
    finally:
        session.close()

    def _d(v):
        return v.date() if hasattr(v, "date") and callable(getattr(v, "date")) else v

    today = date.today()
    candidates = [_d(t.executed_at) for t in trades if getattr(t, "executed_at", None)]
    candidates += [_d(s.date) for s in snaps]
    operation_start = min(candidates) if candidates else today

    # 운영 기간 내 KRX 영업일 수 / 스냅샷이 찍힌 영업일 수
    th = TradingHours(config)
    snapshot_dates = {_d(s.date) for s in snaps}
    trading_days_total = 0
    snapshot_days = 0
    d = operation_start
    from datetime import datetime as _dt
    while d <= today:
        if th.is_trading_day(_dt(d.year, d.month, d.day)):
            trading_days_total += 1
            if d in snapshot_dates:
                snapshot_days += 1
        d += timedelta(days=1)

    total_costs = sum(
        float(t.commission or 0) + float(t.tax or 0) + float(t.slippage or 0) for t in trades
    )
    initial_capital = float(
        (config.risk_params.get("position_sizing") or {}).get("initial_capital")
        or config.trading.get("initial_capital")
        or 10_000_000
    )

    nav_return_pct = None
    if snaps:
        nav_return_pct = (float(snaps[-1].total_value) / initial_capital - 1.0) * 100

    benchmark_return_pct = None
    try:
        from core.data_collector import DataCollector
        benchmark_return_pct = DataCollector.fetch_benchmark_return(
            str(operation_start), str(today), symbol="KS11",
        )
    except Exception as exc:  # 벤치마크는 참고 지표 — 실패해도 평가는 진행
        logger.warning("KS11 벤치마크 조회 실패(참고 지표 생략): {}", exc)

    result = evaluate_basket_paper_operation(
        operation_start=operation_start,
        today=today,
        trading_days_total=trading_days_total,
        snapshot_days=snapshot_days,
        pending_failed_orders=len(get_pending_failed_orders() or []),
        total_costs=total_costs,
        initial_capital=initial_capital,
        nav_return_pct=nav_return_pct,
        benchmark_return_pct=benchmark_return_pct,
        min_trading_days=min_days,
    )
    return result, basket_label


def main() -> int:
    parser = argparse.ArgumentParser(description="바스켓 paper 운영 평가 (승격 판정)")
    parser.add_argument("--min-days", type=int, default=60, help="필요 운영 영업일 수 (기본 60)")
    parser.add_argument("--out", type=str, default=None, help="평가 리포트 저장 경로 (Markdown)")
    args = parser.parse_args()

    from core.basket_evaluation import format_evaluation_report

    result, basket_label = _collect(args.min_days)
    report = format_evaluation_report(result, basket_name=basket_label)
    print(report)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        logger.info("평가 리포트 저장: {}", out)

    # exit code: WAIT/PASS=0(정상 흐름), FAIL_REVIEW=1(운영자 확인 필요)
    return 1 if result["verdict"] == "FAIL_REVIEW" else 0


if __name__ == "__main__":
    raise SystemExit(main())

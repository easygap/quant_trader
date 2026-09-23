"""평가 창 회귀 테스트 (2026-09-23 점검).

- 규칙을 바꾼 트랙은 새 규칙 적용일 이후를 따로 센다(rules_window). 예전 규칙의
  운영 일수로 진행률이 100%가 되면 새 규칙을 검토할 근거가 없다.
- 설계를 바꾼 트랙의 성과 귀속은 설계 적용일부터 잰다(design_effective_from).
"""

from datetime import date, datetime
from unittest.mock import patch

import pytest

from database.models import init_database


def _seed(key, days, *, cum=None, trade_day=None):
    from database.models import TradeHistory, get_session
    from database.repositories import save_portfolio_snapshot

    init_database()
    for i, d in enumerate(days):
        save_portfolio_snapshot(
            total_value=1_000_000, cash=400_000, invested=600_000,
            cumulative_return=(cum[i] if cum else 0.0),
            account_key=key, snapshot_date=datetime.combine(d, datetime.min.time()),
            mode="paper",
        )
    if trade_day:
        s = get_session()
        try:
            s.add(TradeHistory(
                account_key=key, strategy=key, symbol="069500", action="BUY",
                price=100_000, quantity=1, total_amount=100_000, commission=15,
                mode="paper", executed_at=datetime.combine(trade_day, datetime.min.time()),
            ))
            s.commit()
        finally:
            s.close()


def _collect(name, cfg, **kw):
    from core.basket_evaluation import collect_basket_paper_evaluation

    with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config",
               return_value={name: cfg}):
        result, _ = collect_basket_paper_evaluation(
            basket_name=name, include_benchmark=False, **kw,
        )
    return result


def test_rules_window_counts_only_days_under_new_rules():
    name = "t_rules_window"
    days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16),
            date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21)]
    _seed(f"basket_rebalance:{name}", days, trade_day=date(2026, 9, 14))
    cfg = {"enabled": True, "initial_capital": 1_000_000, "holdings": {"069500": 1.0},
           "promotion": {"paper_only": True, "rules_effective_from": "2026-09-17"}}

    r = _collect(name, cfg)

    rw = r["rules_window"]
    assert rw["since"] == "2026-09-17"
    # 9/17, 9/18, 9/21 + (오늘까지 스냅샷 없는 거래일은 제외되지 않고 결측으로 센다)
    assert rw["trading_days"] >= 3
    assert r["progress_days"] >= rw["trading_days"] + 3   # 전체 운영 일수는 그대로
    assert "rules_window" not in _collect(name, {**cfg, "promotion": {"paper_only": True}})


def test_daily_card_shows_new_rules_progress():
    from core.basket_evaluation import build_daily_report_extras

    extras = build_daily_report_extras(eval_result={
        "progress_days": 52, "min_trading_days": 60, "snapshot_days": 52,
        "snapshot_coverage": 1.0, "progress_pct": 0.87, "paper_only": True,
        "rules_window": {"trading_days": 5, "min_trading_days": 60},
        "metrics": {},
    })
    assert "새 규칙 5/60일" in extras["progress"]


def test_attribution_window_starts_at_design_date():
    name = "t_design_window"
    days = [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)]
    # 8/06까지 누적 +10%, 8/10 누적 +12.2% → 설계 적용 이후 구간은 +2%
    _seed(f"basket_rebalance:{name}", days, cum=[5.0, 10.0, 11.0, 12.2],
          trade_day=date(2026, 8, 5))
    cfg = {"enabled": True, "initial_capital": 1_000_000,
           "holdings": {"005930": 1.0}, "target_stock_weight": 0.6,
           "promotion": {"design_effective_from": "2026-08-07"}}
    seen = {}

    def _design(holdings, fraction, start, end):
        seen["start"] = start
        return 1.0

    with patch("core.basket_evaluation.compute_design_portfolio_return", _design):
        r = _collect(name, cfg, include_attribution=True)

    assert seen["start"] == date(2026, 8, 7)
    m = r["metrics"]
    assert m["attribution_window"][0] == "2026-08-07"
    assert m["attribution_nav_pct"] == pytest.approx(2.0, abs=1e-6)
    assert m["execution_gap_pct"] == pytest.approx(1.0, abs=1e-6)


# ------------------------------------------------------------ 승격 판정 NaN

def _metrics(**over):
    from core.promotion_engine import StrategyMetrics

    base = dict(name="t", total_return=10.0, profit_factor=1.5, mdd=-10.0,
                wf_positive_rate=0.8, wf_sharpe_positive_rate=0.8, wf_windows=5,
                wf_total_trades=50, sharpe=1.2)
    base.update(over)
    return StrategyMetrics(**base)


@pytest.mark.parametrize("field", ["total_return", "profit_factor", "wf_positive_rate"])
def test_nan_core_metric_fails_paper_only(field):
    from core.promotion_engine import _check_paper_only

    ok, reason = _check_paper_only(_metrics(**{field: float("nan")}))
    assert ok is False and field in reason


@pytest.mark.parametrize("field", ["sharpe", "mdd"])
def test_nan_risk_metric_fails_provisional_without_crash(field):
    from core.promotion_engine import _check_provisional_candidate

    ok, reason = _check_provisional_candidate(_metrics(**{field: float("nan")}))
    assert ok is False and field in reason


def test_missing_metric_fails_closed_instead_of_type_error():
    from core.promotion_engine import _check_paper_only

    ok, _ = _check_paper_only(_metrics(total_return=None))
    assert ok is False


def test_paper_order_errors_are_counted_but_not_gating():
    from core.cycle_observability import record_cycle_event
    from core.basket_evaluation import format_evaluation_report

    name = "t_paper_errors"
    key = f"basket_rebalance:{name}"
    _seed(key, [date(2026, 9, 21)], trade_day=date(2026, 9, 21))
    record_cycle_event("ORDER_ERROR", "예외", severity="critical", strategy=key, mode="paper")
    cfg = {"enabled": True, "initial_capital": 1_000_000, "holdings": {"069500": 1.0}}

    r = _collect(name, cfg)

    assert r["metrics"]["paper_order_errors"] == 1
    assert not any("오류" in i for i in r["issues"])          # 판정에는 넣지 않는다
    assert "주문·사이클 오류 이벤트 1건" in format_evaluation_report(r, name)

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd


class FakeCollector:
    def __init__(self, frames):
        self.frames = frames

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["close"])
        return df.copy()


def _ohlcv(dates, close):
    close = np.array(close, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [1_000_000] * len(close),
        }
    )


def _frames_for_rotation():
    dates = pd.bdate_range("2025-01-01", "2025-03-10")
    steps = np.arange(len(dates), dtype=float)
    a = 100 + steps * 0.5
    b = 100 + np.minimum(steps, 24) * 0.9 - np.maximum(steps - 24, 0) * 0.8
    c = 100 + np.maximum(steps - 24, 0) * 1.4
    benchmark = np.full(len(dates), 100.0)
    return {
        "AAA": _ohlcv(dates, a),
        "BBB": _ohlcv(dates, b),
        "CCC": _ohlcv(dates, c),
        "KS11": _ohlcv(dates, benchmark),
    }


def _params():
    return {
        "target_top_n": 2,
        "target_exposure": 0.80,
        "target_tolerance_pct": 0.0,
        "short_lookback": 2,
        "long_lookback": 3,
        "short_weight": 0.5,
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
    }


def test_target_weight_plan_uses_prior_day_scores_for_targets():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    c = frames["CCC"].copy()
    c.loc[c["date"] == pd.Timestamp("2025-02-03"), "close"] = 500.0
    frames["CCC"] = c

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.score_day == "2025-01-31"
    assert set(plan.targets) == {"AAA", "BBB"}
    assert "CCC" not in plan.targets
    assert {order.symbol for order in plan.orders if order.action == "BUY"} == {"AAA", "BBB"}


def test_target_weight_plan_risk_overlay_uses_prior_day_benchmark():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    benchmark = frames["KS11"].copy()
    same_day_mask = benchmark["date"] == pd.Timestamp("2025-02-03")
    benchmark.loc[same_day_mask, "close"] = 50.0
    benchmark.loc[same_day_mask, "open"] = 50.0
    benchmark.loc[same_day_mask, "high"] = 50.0
    benchmark.loc[same_day_mask, "low"] = 50.0
    frames["KS11"] = benchmark

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={
            **_params(),
            "market_exposure_mode": "benchmark_risk",
            "market_ma_period": 5,
            "bear_target_exposure": 0.35,
            "benchmark_drawdown_lookback": 5,
            "benchmark_drawdown_trigger_pct": 4.0,
        },
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.target_exposure == 0.8
    assert plan.risk_off is False


def test_pilot_plan_validation_blocks_order_count_and_notional_caps():
    from core.target_weight_rotation import build_target_weight_plan, validate_plan_against_pilot

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(_frames_for_rotation()),
    )
    pilot_check = SimpleNamespace(
        allowed=True,
        reason="pilot authorized",
        remaining_orders=1,
        caps_snapshot={
            "max_orders_per_day": 1,
            "max_concurrent_positions": 1,
            "max_notional_per_trade": 10_000,
            "max_gross_exposure": 30_000,
        },
    )

    validation = validate_plan_against_pilot(plan, pilot_check)

    assert validation.allowed is False
    assert "remaining_orders" in validation.reason
    assert "max order notional" in validation.reason
    assert "target positions" in validation.reason


def test_execute_plan_dry_run_preserves_sell_before_buy_ordering():
    from core.target_weight_rotation import build_target_weight_plan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={**_params(), "target_top_n": 1},
        cash=50_000.0,
        positions={"BBB": {"quantity": 20, "avg_price": 105.0}},
        as_of_date="2025-03-03",
        collector=FakeCollector(_frames_for_rotation()),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=True,
    )

    actions = [detail["order"]["action"] for detail in execution["details"]]
    assert actions == sorted(actions, reverse=True)
    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)


def test_execute_plan_stops_after_failed_sell_before_buy():
    from core.target_weight_rotation import TargetWeightOrder, TargetWeightPlan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = TargetWeightPlan(
        candidate_id="candidate",
        as_of_date="2025-03-03",
        trade_day="2025-03-03",
        score_day="2025-02-28",
        params_hash="hash",
        symbols=["AAA", "BBB"],
        targets=["BBB"],
        prices={"AAA": 100.0, "BBB": 200.0},
        target_exposure=0.8,
        base_target_exposure=0.8,
        risk_off=False,
        nav=10_000.0,
        cash_before=1_000.0,
        market_value_before=9_000.0,
        cash_after_estimate=1_000.0,
        gross_exposure_after=8_000.0,
        target_position_count=1,
        orders=[
            TargetWeightOrder(
                symbol="AAA",
                action="SELL",
                price=100.0,
                quantity=10,
                notional=1_000.0,
                current_quantity=10,
                target_quantity=0,
                current_weight_pct=10.0,
                target_weight_pct=0.0,
                reason="sell first",
            ),
            TargetWeightOrder(
                symbol="BBB",
                action="BUY",
                price=200.0,
                quantity=5,
                notional=1_000.0,
                current_quantity=0,
                target_quantity=5,
                current_weight_pct=0.0,
                target_weight_pct=10.0,
                reason="buy second",
            ),
        ],
        diagnostics={},
    )

    class FakeExecutor:
        buy_calls = 0

        def __init__(self, config, account_key=""):
            pass

        def execute_sell(self, **kwargs):
            return {"success": False, "reason": "no position"}

        def execute_buy_quantity(self, **kwargs):
            FakeExecutor.buy_calls += 1
            return {"success": True}

    class FakePortfolio:
        def __init__(self, config, account_key=""):
            pass

        def get_available_cash(self):
            return 10_000.0

        def get_total_value(self):
            return 10_000.0

    with patch("core.order_executor.OrderExecutor", FakeExecutor), \
         patch("core.portfolio_manager.PortfolioManager", FakePortfolio):
        execution = execute_plan(
            plan,
            config=SimpleNamespace(trading={"mode": "paper"}),
            dry_run=False,
        )

    assert execution["failed"] == 1
    assert execution["halted"] is True
    assert execution["details"][1]["status"] == "skipped_after_failure"
    assert FakeExecutor.buy_calls == 0

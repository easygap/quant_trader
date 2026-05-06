import numpy as np
import pandas as pd


class FakeCollector:
    quiet_ohlcv_log = False

    def __init__(self, frames):
        self.frames = frames

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["close"])
        return df.copy()


class NoCostRiskManager:
    def calculate_transaction_costs(self, price, quantity, action="BUY", avg_daily_volume=None, avg_price=None):
        return {
            "commission": 0.0,
            "tax": 0.0,
            "capital_gains_tax": 0.0,
            "slippage": 0.0,
            "execution_price": float(price),
        }


class CostRiskManager:
    def calculate_transaction_costs(self, price, quantity, action="BUY", avg_daily_volume=None, avg_price=None):
        if action == "BUY":
            return {
                "commission": 1.0,
                "tax": 0.0,
                "capital_gains_tax": 0.0,
                "slippage": 0.0,
                "execution_price": float(price) + 0.1,
            }
        return {
            "commission": 1.0,
            "tax": 1.0,
            "capital_gains_tax": 0.0,
            "slippage": 0.0,
            "execution_price": max(0.0, float(price) - 0.1),
        }


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


def _frames_for_score_floor():
    dates = pd.bdate_range("2025-01-27", "2025-02-05")
    return {
        "AAA": _ohlcv(dates, [100, 101, 102, 103, 104, 105, 106, 107]),
        "BBB": _ohlcv(dates, [100, 99, 98, 97, 96, 95, 94, 93]),
        "CCC": _ohlcv(dates, [100, 98, 96, 94, 92, 90, 88, 86]),
        "KS11": _ohlcv(dates, [100] * len(dates)),
    }


def test_target_weight_rotation_holds_top_n_with_cash_buffer():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    result = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-03-10",
        capital=100_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
        },
        collector=FakeCollector(_frames_for_rotation()),
        risk_manager=NoCostRiskManager(),
    )

    eq = result["equity_curve"]
    metrics = result["target_weight_metrics"]

    assert not eq.empty
    assert int(eq.loc[eq["date"] == pd.Timestamp("2025-02-03"), "n_positions"].iloc[0]) == 2
    assert metrics["target_top_n"] == 2
    assert metrics["avg_slots_filled"] == 2.0
    assert metrics["slot_fill_rate_pct"] == 100.0
    first_exposure = 1 - (
        eq.loc[eq["date"] == pd.Timestamp("2025-02-03"), "cash"].iloc[0]
        / eq.loc[eq["date"] == pd.Timestamp("2025-02-03"), "value"].iloc[0]
    )
    assert 0.79 <= first_exposure <= 0.81


def test_target_weight_plan_records_liquidity_diagnostics():
    from core.target_weight_rotation import build_target_weight_plan

    plan = build_target_weight_plan(
        symbols=["AAA", "BBB", "CCC"],
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
        },
        cash=100_000.0,
        positions={},
        as_of_date="2025-03-10",
        collector=FakeCollector(_frames_for_rotation()),
    )

    liquidity = plan.diagnostics["liquidity"]

    assert liquidity["lookback_days"] == 20
    assert liquidity["symbols"]["AAA"]["complete"] is True
    assert liquidity["symbols"]["AAA"]["observations"] == 20
    assert liquidity["symbols"]["AAA"]["avg_daily_value"] > 0


def test_target_weight_rotation_uses_prior_day_scores_for_rebalance():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    frames = _frames_for_rotation()
    c = frames["CCC"].copy()
    c.loc[c["date"] == pd.Timestamp("2025-02-03"), "close"] = 500.0
    frames["CCC"] = c

    result = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-02-05",
        capital=100_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
        },
        collector=FakeCollector(frames),
        risk_manager=NoCostRiskManager(),
    )

    first_day_buys = [
        t["symbol"]
        for t in result["trades"]
        if t["action"] == "BUY" and t["date"] == pd.Timestamp("2025-02-03")
    ]

    assert "CCC" not in first_day_buys
    assert set(first_day_buys) == {"AAA", "BBB"}


def test_target_weight_rotation_delta_rebalances_and_charges_costs():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    params = {
        "target_top_n": 2,
        "target_exposure": 0.80,
        "target_tolerance_pct": 0.0,
        "short_lookback": 2,
        "long_lookback": 3,
        "short_weight": 0.5,
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
    }
    no_cost = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-03-10",
        capital=100_000.0,
        params=params,
        collector=FakeCollector(_frames_for_rotation()),
        risk_manager=NoCostRiskManager(),
    )
    with_cost = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-03-10",
        capital=100_000.0,
        params=params,
        collector=FakeCollector(_frames_for_rotation()),
        risk_manager=CostRiskManager(),
    )

    sell_symbols = {
        t["symbol"]
        for t in no_cost["trades"]
        if t["action"] == "REBALANCE_SELL" and t["date"] >= pd.Timestamp("2025-03-03")
    }
    buy_symbols = {
        t["symbol"]
        for t in no_cost["trades"]
        if t["action"] == "BUY" and t["date"] >= pd.Timestamp("2025-03-03")
    }

    assert "BBB" in sell_symbols
    assert "CCC" in buy_symbols
    assert with_cost["equity_curve"]["value"].iloc[-1] < no_cost["equity_curve"]["value"].iloc[-1]


def test_target_weight_rotation_hold_rank_buffer_reduces_symbol_churn():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    base_params = {
        "target_top_n": 2,
        "target_exposure": 0.80,
        "target_tolerance_pct": 0.0,
        "short_lookback": 2,
        "long_lookback": 3,
        "short_weight": 0.5,
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
    }
    base = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-03-10",
        capital=100_000.0,
        params=base_params,
        collector=FakeCollector(_frames_for_rotation()),
        risk_manager=NoCostRiskManager(),
    )
    buffered = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-03-10",
        capital=100_000.0,
        params={**base_params, "hold_rank_buffer": 1},
        collector=FakeCollector(_frames_for_rotation()),
        risk_manager=NoCostRiskManager(),
    )

    base_march_symbols = {
        t["symbol"]
        for t in base["trades"]
        if t["date"] >= pd.Timestamp("2025-03-03")
    }
    buffered_march_symbols = {
        t["symbol"]
        for t in buffered["trades"]
        if t["date"] >= pd.Timestamp("2025-03-03")
    }

    assert "CCC" in base_march_symbols
    assert "CCC" not in buffered_march_symbols
    assert buffered["target_weight_metrics"]["hold_rank_buffer"] == 1
    assert (
        buffered["target_weight_metrics"]["target_weight_turnover_per_year"]
        < base["target_weight_metrics"]["target_weight_turnover_per_year"]
    )


def test_target_weight_rotation_benchmark_risk_overlay_reduces_exposure():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    frames = _frames_for_rotation()
    benchmark = frames["KS11"].copy()
    decline_mask = benchmark["date"] >= pd.Timestamp("2025-01-27")
    benchmark.loc[decline_mask, "close"] = np.linspace(98.0, 88.0, int(decline_mask.sum()))
    benchmark.loc[decline_mask, "open"] = benchmark.loc[decline_mask, "close"]
    benchmark.loc[decline_mask, "high"] = benchmark.loc[decline_mask, "close"]
    benchmark.loc[decline_mask, "low"] = benchmark.loc[decline_mask, "close"]
    frames["KS11"] = benchmark

    result = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-02-05",
        capital=100_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
            "market_exposure_mode": "benchmark_risk",
            "market_ma_period": 5,
            "bear_target_exposure": 0.35,
            "benchmark_drawdown_lookback": 5,
            "benchmark_drawdown_trigger_pct": 4.0,
        },
        collector=FakeCollector(frames),
        risk_manager=NoCostRiskManager(),
    )

    first_day = result["equity_curve"].iloc[0]
    first_exposure = 1 - first_day["cash"] / first_day["value"]

    assert 0.34 <= first_exposure <= 0.36
    assert result["target_weight_metrics"]["avg_target_exposure_pct"] == 35.0
    assert result["target_weight_metrics"]["min_target_exposure_pct"] == 35.0
    assert result["target_weight_metrics"]["risk_off_rebalance_count"] == 1
    assert result["target_weight_metrics"]["risk_off_rebalance_pct"] == 100.0


def test_target_weight_rotation_benchmark_risk_uses_prior_day_benchmark():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    frames = _frames_for_rotation()
    benchmark = frames["KS11"].copy()
    same_day_mask = benchmark["date"] == pd.Timestamp("2025-02-03")
    benchmark.loc[same_day_mask, "close"] = 50.0
    benchmark.loc[same_day_mask, "open"] = 50.0
    benchmark.loc[same_day_mask, "high"] = 50.0
    benchmark.loc[same_day_mask, "low"] = 50.0
    frames["KS11"] = benchmark

    result = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-02-03",
        capital=100_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
            "market_exposure_mode": "benchmark_risk",
            "market_ma_period": 5,
            "bear_target_exposure": 0.35,
            "benchmark_drawdown_lookback": 5,
            "benchmark_drawdown_trigger_pct": 4.0,
        },
        collector=FakeCollector(frames),
        risk_manager=NoCostRiskManager(),
    )

    first_day = result["equity_curve"].iloc[0]
    first_exposure = 1 - first_day["cash"] / first_day["value"]

    assert 0.79 <= first_exposure <= 0.81
    assert result["target_weight_metrics"]["risk_off_rebalance_count"] == 0


def test_target_weight_rotation_score_floor_leaves_weak_slots_in_cash():
    from tools.research_candidate_sweep import run_target_weight_rotation_backtest

    result = run_target_weight_rotation_backtest(
        symbols=["AAA", "BBB", "CCC"],
        start="2025-02-03",
        end="2025-02-05",
        capital=100_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 0.80,
            "short_lookback": 2,
            "long_lookback": 3,
            "short_weight": 0.5,
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
            "min_score_floor_pct": 0.0,
        },
        collector=FakeCollector(_frames_for_score_floor()),
        risk_manager=NoCostRiskManager(),
    )

    buys = [t["symbol"] for t in result["trades"] if t["action"] == "BUY"]
    metrics = result["target_weight_metrics"]
    first_day = result["equity_curve"].iloc[0]
    first_exposure = 1 - first_day["cash"] / first_day["value"]

    assert buys == ["AAA"]
    assert metrics["avg_slots_filled"] == 1.0
    assert metrics["slot_fill_rate_pct"] == 50.0
    assert 0.79 <= first_exposure <= 0.81

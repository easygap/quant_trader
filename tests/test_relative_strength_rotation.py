import numpy as np
import pandas as pd

from config.config_loader import Config


def _rotation_df(n=8):
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = np.linspace(100, 107, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": [1_000_000] * n,
        },
        index=dates,
    )


def _monthly_rotation_df(close):
    dates = pd.bdate_range("2025-01-27", periods=len(close))
    close = np.array(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": [1_000_000] * len(close),
        },
        index=dates,
    )


def _rotation_strategy(params):
    from strategies.relative_strength_rotation import RelativeStrengthRotationStrategy

    strategy = RelativeStrengthRotationStrategy(Config.get())
    strategy.params = {
        "short_lookback": 2,
        "long_lookback": 3,
        "sma_period": 2,
        "short_weight": 0.6,
        **params,
    }
    strategy.indicator_engine.calculate_all = lambda df: df
    return strategy


def _strategy_with_market_filter(pass_values, *, market_filter_exit):
    from strategies.relative_strength_rotation import RelativeStrengthRotationStrategy

    strategy = RelativeStrengthRotationStrategy(Config.get())
    strategy.params = {
        "short_lookback": 2,
        "long_lookback": 3,
        "sma_period": 2,
        "short_weight": 0.6,
        "market_filter_sma200": True,
        "market_filter_exit": market_filter_exit,
    }
    strategy.indicator_engine.calculate_all = lambda df: df

    def fake_market_filter(index):
        strategy._mf_series = pd.Series(pass_values, index=index)

    strategy._ensure_market_filter = fake_market_filter
    return strategy


def test_rotation_market_filter_exit_switches_to_cash():
    df = _rotation_df()
    pass_values = [True, True, True, True, True, False, False, True]
    strategy = _strategy_with_market_filter(pass_values, market_filter_exit=True)

    result = strategy.analyze(df)

    assert bool(result["market_filter_exit"].iloc[5]) is True
    assert result["signal"].iloc[5] == "SELL"


def test_rotation_market_filter_without_exit_only_blocks_entries():
    df = _rotation_df()
    pass_values = [True, True, True, True, True, False, False, True]
    strategy = _strategy_with_market_filter(pass_values, market_filter_exit=False)

    result = strategy.analyze(df)

    assert bool(result["market_filter_pass"].iloc[5]) is False
    assert bool(result["market_filter_exit"].iloc[5]) is False
    assert result["signal"].iloc[5] == "HOLD"


def test_rotation_dense_ranked_can_buy_without_absolute_trend_filters():
    df = _monthly_rotation_df([100, 99, 98, 97, 96, 95, 94, 93])
    strategy = _rotation_strategy(
        {
            "rank_entry_mode": "dense_ranked",
            "use_positive_momentum_filter": False,
            "use_trend_filter": False,
            "exit_trend_edge": False,
            "exit_rebalance_mode": "none",
        }
    )

    result = strategy.analyze(df)
    rebalance = result[result["rebalance_day"]].iloc[0]

    assert rebalance["composite_score"] < 0
    assert bool(rebalance["above_trend"]) is False
    assert rebalance["signal"] == "BUY"


def test_rotation_benchmark_excess_adds_ranking_diagnostics():
    df = _monthly_rotation_df([100, 101, 102, 103, 104, 105, 106, 107])
    strategy = _rotation_strategy(
        {
            "score_mode": "benchmark_excess",
            "benchmark_symbol": "KS11",
            "rank_entry_mode": "dense_ranked",
            "use_positive_momentum_filter": False,
            "use_trend_filter": False,
            "exit_trend_edge": False,
            "exit_rebalance_mode": "none",
        }
    )
    strategy._benchmark_composite = lambda index, *_args: pd.Series(0.01, index=index)

    result = strategy.analyze(df)
    rebalance = result[result["rebalance_day"]].iloc[0]
    expected_excess = rebalance["composite_score"] - 0.01

    assert np.isclose(rebalance["benchmark_composite_score"], 0.01)
    assert np.isclose(rebalance["benchmark_excess_score"], expected_excess)
    assert np.isclose(rebalance["ranking_score"], expected_excess)
    assert np.isclose(rebalance["strategy_score"], expected_excess * 100)


def test_rotation_benchmark_excess_blocks_buy_when_benchmark_missing():
    df = _monthly_rotation_df([100, 101, 102, 103, 104, 105, 106, 107])
    strategy = _rotation_strategy(
        {
            "score_mode": "benchmark_excess",
            "rank_entry_mode": "dense_ranked",
            "use_positive_momentum_filter": False,
            "use_trend_filter": False,
            "exit_trend_edge": False,
            "exit_rebalance_mode": "none",
        }
    )
    strategy._benchmark_composite = lambda index, *_args: pd.Series(np.nan, index=index)

    result = strategy.analyze(df)

    assert "BUY" not in set(result["signal"])
    assert result["benchmark_excess_score"].isna().all()


def test_rotation_score_floor_exit_overrides_dense_ranked_buy():
    df = _monthly_rotation_df([100, 99, 98, 97, 96, 95, 94, 93])
    strategy = _rotation_strategy(
        {
            "rank_entry_mode": "dense_ranked",
            "use_positive_momentum_filter": False,
            "use_trend_filter": False,
            "exit_trend_edge": False,
            "exit_rebalance_mode": "score_floor",
            "sell_score_floor_pct": -1.0,
        }
    )

    result = strategy.analyze(df)
    rebalance = result[result["rebalance_day"]].iloc[0]

    assert rebalance["ranking_score"] * 100 <= -1.0
    assert rebalance["signal"] == "SELL"

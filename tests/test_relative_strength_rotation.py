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

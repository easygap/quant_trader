import numpy as np
import pandas as pd

from config.config_loader import Config


def _price_df(closes):
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    close = np.array(closes, dtype=float)
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


def test_benchmark_relative_momentum_uses_excess_return_for_signal():
    from strategies.momentum_factor import MomentumFactorStrategy

    strategy = MomentumFactorStrategy(Config.get())
    strategy.params = {
        "benchmark_relative": True,
        "lookback_days": 2,
        "buy_threshold_pct": 2.0,
        "sell_threshold_pct": -2.0,
    }

    df = _price_df([100, 100, 110, 115, 118])
    benchmark_return = pd.Series(
        [np.nan, np.nan, 12.0, 20.0, 5.0],
        index=df.index,
    )
    strategy._benchmark_return = lambda index, lookback, symbol: benchmark_return

    result = strategy.analyze(df)

    assert result["signal"].iloc[3] == "SELL"
    assert result["signal"].iloc[4] == "BUY"
    assert round(result["benchmark_excess_return"].iloc[4], 2) == 2.27
    assert result["strategy_score"].iloc[4] > 0


def test_benchmark_relative_momentum_fails_closed_without_benchmark_data():
    from strategies.momentum_factor import MomentumFactorStrategy

    strategy = MomentumFactorStrategy(Config.get())
    strategy.params = {
        "benchmark_relative": True,
        "lookback_days": 2,
        "buy_threshold_pct": 1.0,
        "sell_threshold_pct": -1.0,
    }

    df = _price_df([100, 100, 110, 120])
    strategy._benchmark_return = lambda index, lookback, symbol: pd.Series(np.nan, index=index)

    result = strategy.analyze(df)

    assert result["signal"].tolist() == ["HOLD", "HOLD", "HOLD", "HOLD"]
    assert result["benchmark_excess_return"].isna().all()

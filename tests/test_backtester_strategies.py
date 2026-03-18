"""전략 analyze()/백테스터 계약 테스트."""

from datetime import datetime

import numpy as np
import pandas as pd

from backtest.backtester import Backtester
from strategies.mean_reversion import MeanReversionStrategy
from strategies.scoring_strategy import ScoringStrategy
from strategies.trend_following import TrendFollowingStrategy
from core.strategy_ensemble import StrategyEnsemble


def _sample_ohlcv(days: int = 320) -> pd.DataFrame:
    np.random.seed(7)
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
    returns = np.random.normal(0.0002, 0.018, days)
    prices = 50000 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": prices * (1 + np.random.uniform(-0.01, 0.01, days)),
        "high": prices * (1 + np.random.uniform(0.0, 0.03, days)),
        "low": prices * (1 - np.random.uniform(0.0, 0.03, days)),
        "close": prices,
        "volume": np.random.randint(100000, 5000000, days),
    }, index=dates)
    df.index.name = "date"
    return df


def test_strategy_analyze_contract_produces_signal_column():
    df = _sample_ohlcv()

    for strategy_cls in (
        ScoringStrategy,
        MeanReversionStrategy,
        TrendFollowingStrategy,
        StrategyEnsemble,
    ):
        analyzed = strategy_cls().analyze(df.copy())
        assert "signal" in analyzed.columns
        assert len(analyzed) == len(df)


def test_backtester_runs_for_all_strategies():
    df = _sample_ohlcv()
    bt = Backtester()

    for strategy_name in ("scoring", "mean_reversion", "trend_following", "ensemble"):
        result = bt.run(df.copy(), strategy_name=strategy_name)
        assert result.get("metrics")
        assert "total_return" in result["metrics"]

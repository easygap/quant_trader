from datetime import datetime

import numpy as np
import pandas as pd


def _sample_ohlcv(days: int = 320) -> pd.DataFrame:
    np.random.seed(12)
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
    returns = np.random.normal(0.0003, 0.015, days)
    prices = 50000 * np.cumprod(1 + returns)
    df = pd.DataFrame(
        {
            "open": prices * 0.99,
            "high": prices * 1.01,
            "low": prices * 0.98,
            "close": prices,
            "volume": np.random.randint(500000, 2000000, days),
        },
        index=dates,
    )
    df.index.name = "date"
    return df


def test_strategy_validator_runs_with_mocked_data(monkeypatch, tmp_path):
    from backtest.strategy_validator import StrategyValidator

    sample = _sample_ohlcv()

    class FakeCollector:
        def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
            return sample.copy()

    monkeypatch.setattr("backtest.strategy_validator.DataCollector", FakeCollector)

    validator = StrategyValidator(output_dir=str(tmp_path))
    result = validator.run(symbol="005930", strategy_name="scoring", benchmark_symbol="KS11")

    assert "validation" in result
    assert "out_sample" in result
    assert result["report_path"]

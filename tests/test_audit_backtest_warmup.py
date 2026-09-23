"""워크포워드·OOS 지표 워밍업 감사 회귀 테스트.

Backtester.run(trade_start_date=...)는 PortfolioBacktester와 같은 규약으로 앞 구간을 지표
워밍업으로만 쓰고, 거래·자본 곡선·지표는 trade_start_date부터 잰다. 검증기 OOS·워크포워드와
최적화기 OOS가 이 경로를 쓴다.
"""

import numpy as np
import pandas as pd
import pytest


class _WarmupConfig:
    settings = {}
    strategies = {}

    def __init__(self, *, dynamic_slippage=False):
        self._trading = {"skip_earnings_days": 0}
        self._risk_params = {
            "transaction_costs": {
                "commission_rate": 0.00015,
                "tax_rate": 0.002,
                "slippage": 0.001,
                "slippage_ticks": 0,
                "dynamic_slippage": {
                    "enabled": dynamic_slippage,
                    "warn_at_volume_ratio": 0.01,
                    "warn_slippage_multiplier": 2.0,
                    "critical_at_volume_ratio": 0.03,
                    "critical_slippage_multiplier": 4.0,
                },
            },
            "stop_loss": {"type": "fixed", "fixed_rate": 0.50},
            "take_profit": {"fixed_rate": 0.50, "partial_exit": False},
            "trailing_stop": {"enabled": False},
            "position_sizing": {"max_risk_per_trade": 0.01, "initial_capital": 100_000},
            "diversification": {"max_position_ratio": 0.20, "max_investment_ratio": 0.70},
            "position_limits": {"min_holding_days": 0, "max_holding_days": 0, "max_monthly_roundtrips": 0},
            "liquidity_filter": {"backtest_max_participation_rate": 1.0},
            "gap_risk": {"enabled": False},
            "blackswan": {"enabled": False},
            "backtest_regime_filter": {"enabled": False},
        }

    @property
    def risk_params(self):
        return self._risk_params

    @property
    def trading(self):
        return self._trading


class _SmaCrossStrategy:
    """종가가 N일 단순이동평균 위면 BUY, 아래면 SELL (과거 데이터만 쓰는 인과적 신호)."""

    def __init__(self, window: int, sell_below: bool = False):
        self.window = window
        self.sell_below = sell_below

    def analyze(self, df):
        out = df.copy()
        sma = out["close"].rolling(self.window, min_periods=self.window).mean()
        out["signal"] = "HOLD"
        out.loc[out["close"] > sma, "signal"] = "BUY"
        if self.sell_below:
            out.loc[out["close"] < sma, "signal"] = "SELL"
        return out


def _ohlcv(close, volume=None, start="2022-01-03"):
    close = np.asarray(close, dtype=float)
    dates = pd.bdate_range(start, periods=len(close))
    volume = np.full(len(close), 1_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
        },
        index=dates,
    )


def _backtester(strategy, **config_kwargs):
    from backtest.backtester import Backtester

    bt = Backtester(_WarmupConfig(**config_kwargs))
    bt._get_strategy = lambda _name: strategy
    return bt


def test_trade_start_date_warms_indicators_before_the_test_window():
    """60일 이동평균 전략: 테스트 구간만 넣으면 60행 뒤에야 첫 매수, 워밍업을 넣으면 첫날 매수."""
    df = _ohlcv(100.0 + np.arange(200, dtype=float))  # 꾸준한 상승 → 워밍업만 되면 항상 BUY
    test_start = df.index[120]
    strategy = _SmaCrossStrategy(window=60)

    cold = _backtester(strategy).run(df.iloc[120:].copy(), strategy_name="sma", strict_lookahead=True)
    warm = _backtester(strategy).run(
        df.copy(), strategy_name="sma", strict_lookahead=True, trade_start_date=test_start
    )

    cold_first_buy = next(t for t in cold["trades"] if t["action"] == "BUY")
    warm_first_buy = next(t for t in warm["trades"] if t["action"] == "BUY")
    assert cold_first_buy["date"] == df.index[180]  # 창 60번째 행 신호 → 다음 날 시가
    assert warm_first_buy["date"] == test_start  # 워밍업 마지막 날 신호 → 첫날 시가
    assert warm_first_buy["signal_date"] == df.index[119]

    # 자본 곡선·기간·지표는 평가 구간만
    equity = warm["equity_curve"]
    assert equity["date"].iloc[0] == test_start
    assert len(equity) == 80
    assert warm["warmup_rows"] == 120
    assert warm["period"].startswith(str(test_start))
    assert all(t["date"] >= test_start for t in warm["trades"])


def test_skipped_warmup_rows_do_not_change_strict_results():
    """strict 모드에서 분석을 건너뛴 워밍업 행도 원본 값(거래량 등)을 유지해 relaxed 실행과 같다."""
    # 130행까지 상승 후 하락 → 첫 거래일 매수, 하락 구간에서 매도
    close = np.concatenate((100.0 + np.arange(130) * 0.5, 164.5 - np.arange(1, 31) * 2.0))
    # 워밍업 거래량은 크고 평가 구간은 작다. 워밍업 행의 거래량이 비면 20일 평균 거래량이
    # 급감해 동적 슬리피지 배수가 달라지고 체결가가 relaxed 실행과 어긋난다.
    # (1% 손실 규칙으로 주문은 13주 안팎 → 거래량 400주면 참여율 3%를 넘는다)
    volume = np.where(np.arange(160) < 99, 1_000_000.0, 400.0)
    df = _ohlcv(close, volume)
    strategy = _SmaCrossStrategy(window=20, sell_below=True)
    kwargs = dict(strategy_name="sma", trade_start_date=df.index[100])

    strict = _backtester(strategy, dynamic_slippage=True).run(df.copy(), strict_lookahead=True, **kwargs)
    relaxed = _backtester(strategy, dynamic_slippage=True).run(df.copy(), strict_lookahead=False, **kwargs)

    assert [t["action"] for t in strict["trades"]][:2] == ["BUY", "SELL"]
    assert strict["trades"][0]["date"] == df.index[100]
    assert strict["trades"] == relaxed["trades"]
    pd.testing.assert_frame_equal(strict["equity_curve"], relaxed["equity_curve"])


def test_trade_start_date_after_last_row_is_an_error():
    df = _ohlcv(np.full(30, 100.0))
    bt = _backtester(_SmaCrossStrategy(window=5))

    with pytest.raises(ValueError):
        bt.run(df, strategy_name="sma", trade_start_date=df.index[-1] + pd.Timedelta(days=30))


# ─── 검증기 OOS ────────────────────────────────────────────────


def test_validator_out_of_sample_run_uses_in_sample_as_warmup(monkeypatch, tmp_path):
    import backtest.strategy_validator as sv

    rng = np.random.default_rng(5)
    df = _ohlcv(50_000 * np.cumprod(1 + rng.normal(0.0003, 0.015, 160)), rng.integers(500_000, 2_000_000, 160))

    class FakeCollector:
        def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
            return df.copy()

    monkeypatch.setattr(sv, "DataCollector", FakeCollector)
    validator = sv.StrategyValidator(output_dir=str(tmp_path))
    calls = []
    original_run = validator.backtester.run

    def spy_run(frame, **kwargs):
        calls.append((len(frame), kwargs.get("trade_start_date")))
        return original_run(frame, **kwargs)

    monkeypatch.setattr(validator.backtester, "run", spy_run)

    result = validator.run(symbol="005930", strategy_name="scoring", use_benchmark_top50=False)

    split_idx = 112  # max(60, int(160 × 0.7))
    assert (160, df.index[split_idx]) in calls  # OOS: 전체를 넣고 split부터 거래
    assert (split_idx, None) in calls  # 인샘플은 그대로
    oos_equity = result["out_sample"]["equity_curve"]
    assert oos_equity["date"].iloc[0] == df.index[split_idx]
    assert len(oos_equity) == 160 - split_idx


# ─── 최적화기 OOS ──────────────────────────────────────────────


def test_optimizer_out_of_sample_runs_use_train_span_as_warmup(monkeypatch):
    import backtest.param_optimizer as po

    df = _ohlcv(100.0 + np.arange(200, dtype=float))
    calls = []

    def fake_run_single(frame, strategy_name, params, config, strict, capital, trade_start_date=None):
        calls.append((len(frame), trade_start_date))
        return {"sharpe_ratio": 2.0, "total_trades": 5, "total_return": 1.0, "max_drawdown": -1.0}

    monkeypatch.setattr(po, "_run_single", fake_run_single)
    config = _WarmupConfig()
    train_end = 140  # int(200 × 0.7)

    po.grid_search(df, "scoring", search_space={"buy_threshold": [3]}, config=config, initial_capital=100_000)
    assert calls == [(train_end, None), (200, df.index[train_end])]

    calls.clear()
    result = po.grid_search_scoring_weights(
        df,
        weight_search_space={"w_macd": [1], "w_bollinger": [1], "w_volume": [1]},
        threshold_pairs=[(3, -3)],
        config=config,
        initial_capital=100_000,
    )
    assert result is not None
    assert calls == [(train_end, None), (200, df.index[train_end])]

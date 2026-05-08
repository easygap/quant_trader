"""
TICKET-05 회귀 테스트: 포트폴리오 백테스터 시장국면 필터

1. bearish 구간 — 신규 매수 전량 차단
2. caution 구간 — 포지션 수량 50% 축소 (caution_scale=0.5)

regime_series를 직접 주입하여 외부 데이터 없이 결정론적으로 검증.
"""

import numpy as np
import pandas as pd
import pytest


def _make_signal_df(n: int = 60, close: float = 10000.0, volume: int = 500_000) -> pd.DataFrame:
    """단순 OHLCV + signal DataFrame 생성."""
    dates = pd.bdate_range("2024-01-01", periods=n)
    closes = np.full(n, close)
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": [volume] * n,
            "signal": ["HOLD"] * n,
            "strategy_score": [3.0] * n,
            "atr": [close * 0.02] * n,
        },
        index=dates,
    )
    # 30일부터 BUY 신호 지속
    df.loc[df.index[30:], "signal"] = "BUY"
    return df


class TestBearishRegimeBlocksBuys:
    """bearish 구간에서 신규 매수가 전혀 발생하지 않아야 한다."""

    def test_no_buys_in_bearish_regime(self):
        from config.config_loader import Config
        from backtest.portfolio_backtester import PortfolioBacktester

        config = Config.get()
        # regime 필터 활성화 보장
        config.risk_params.setdefault("backtest_regime_filter", {})["enabled"] = True
        config.risk_params["backtest_regime_filter"]["caution_scale"] = 0.5

        pbt = PortfolioBacktester(config)

        sym = "TEST01"
        df = _make_signal_df(n=60)
        signals = {sym: df}
        all_dates = list(df.index)

        # 모든 날짜를 bearish로 지정
        bearish_series = pd.Series("bearish", index=all_dates)

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals=signals,
            data={},
            all_dates=all_dates,
            initial_capital=100_000_000,
            regime_series=bearish_series,
        )

        buy_trades = [t for t in result["trades"] if t["action"] == "BUY"]
        assert len(buy_trades) == 0, (
            f"bearish 구간에서 매수 발생: {len(buy_trades)}건 "
            f"(날짜: {[str(t['date'].date()) for t in buy_trades[:3]]})"
        )
        assert result["regime_buy_blocks"] > 0, "regime_buy_blocks 카운터가 0 (차단 미집계)"


class TestCautionRegimeScalesPosition:
    """caution 구간에서 매수 수량이 bullish 대비 50% 이하여야 한다."""

    def _run_with_regime(self, regime_label: str, capital: int = 100_000_000):
        from config.config_loader import Config
        from backtest.portfolio_backtester import PortfolioBacktester

        config = Config.get()
        config.risk_params.setdefault("backtest_regime_filter", {})["enabled"] = True
        config.risk_params["backtest_regime_filter"]["caution_scale"] = 0.5

        pbt = PortfolioBacktester(config)

        sym = "TEST02"
        df = _make_signal_df(n=60)
        signals = {sym: df}
        all_dates = list(df.index)

        regime_series = pd.Series(regime_label, index=all_dates)

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals=signals,
            data={},
            all_dates=all_dates,
            initial_capital=capital,
            regime_series=regime_series,
        )
        buy_trades = [t for t in result["trades"] if t["action"] == "BUY"]
        return buy_trades, result

    def test_caution_quantity_less_than_bullish(self):
        bullish_buys, _ = self._run_with_regime("bullish")
        caution_buys, result = self._run_with_regime("caution")

        # caution에서도 매수가 발생해야 함 (차단이 아니라 축소)
        assert len(caution_buys) > 0, "caution 구간에서 매수가 전혀 없음 (차단이 아닌 축소여야 함)"

        # caution_buys 카운터 집계 확인
        assert result["regime_caution_buys"] > 0, "regime_caution_buys 카운터가 0 (축소 미집계)"

        # 수량 비교: caution이 bullish보다 작아야 함
        if bullish_buys and caution_buys:
            bull_qty = bullish_buys[0]["quantity"]
            caut_qty = caution_buys[0]["quantity"]
            # caution_scale=0.5 → caution 수량 ≤ bullish 수량 × 0.5 (반올림 허용 +1)
            assert caut_qty <= max(1, bull_qty * 0.5 + 1), (
                f"caution 수량({caut_qty})이 bullish({bull_qty}) × 0.5 + 1 초과. "
                "caution_scale 축소 미적용 의심"
            )


class _PortfolioGuardConfig:
    settings = {}
    strategies = {}

    def __init__(
        self,
        *,
        gap_enabled=True,
        skip_earnings_days=0,
        blackswan_enabled=False,
        recovery_scale=0.5,
    ):
        self._trading = {"skip_earnings_days": skip_earnings_days}
        self._risk_params = {
            "transaction_costs": {
                "commission_rate": 0.0,
                "tax_rate": 0.0,
                "slippage": 0.0,
                "slippage_ticks": 0,
                "dynamic_slippage": {"enabled": False},
            },
            "stop_loss": {"type": "fixed", "fixed_rate": 0.03},
            "take_profit": {"fixed_rate": 0.50},
            "trailing_stop": {"enabled": False},
            "position_sizing": {
                "max_risk_per_trade": 0.01,
                "initial_capital": 100_000,
            },
            "diversification": {
                "max_positions": 10,
                "max_position_ratio": 0.20,
                "max_investment_ratio": 0.95,
                "min_cash_ratio": 0.0,
            },
            "position_limits": {"max_holding_days": 0},
            "backtest_regime_filter": {"enabled": False},
            "gap_risk": {
                "enabled": gap_enabled,
                "gap_down_threshold": -0.03,
                "gap_up_entry_block": 0.05,
            },
            "blackswan": {
                "enabled": blackswan_enabled,
                "single_stock_threshold": -0.05,
                "portfolio_threshold": -0.03,
                "consecutive_days": 3,
                "consecutive_threshold": -0.02,
                "cooldown_minutes": 60,
                "recovery_minutes": 60,
                "recovery_scale": recovery_scale,
            },
        }

    @property
    def risk_params(self):
        return self._risk_params

    @property
    def trading(self):
        return self._trading


def _make_portfolio_guard_df(close, *, open_=None, signals=None, volume=1_000_000):
    dates = pd.bdate_range("2024-01-01", periods=len(close))
    close = np.array(close, dtype=float)
    open_ = close if open_ is None else np.array(open_, dtype=float)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": [volume] * len(close),
            "signal": signals or ["HOLD"] * len(close),
            "strategy_score": [3.0] * len(close),
            "atr": close * 0.02,
        },
        index=dates,
    )
    return df


class TestPortfolioRiskEventGuards:
    """paper/live 리스크 이벤트를 포트폴리오 백테스트에도 반영한다."""

    def test_gap_up_blocks_new_buy(self):
        from backtest.portfolio_backtester import PortfolioBacktester

        pbt = PortfolioBacktester(_PortfolioGuardConfig(gap_enabled=True))
        sym = "GUARD01"
        df = _make_portfolio_guard_df(
            [100.0, 100.0, 100.0],
            open_=[100.0, 106.0, 100.0],
            signals=["HOLD", "BUY", "HOLD"],
        )

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals={sym: df},
            data={},
            all_dates=list(df.index),
            initial_capital=100_000.0,
        )

        assert [t["action"] for t in result["trades"]] == []
        assert result["gap_up_buy_blocks"] == 1
        assert result["skipped_reasons"]["gap_up_entry_block"] == 1

    def test_earnings_window_blocks_new_buy(self):
        from backtest.portfolio_backtester import PortfolioBacktester

        pbt = PortfolioBacktester(_PortfolioGuardConfig(skip_earnings_days=3))
        sym = "GUARD02"
        df = _make_portfolio_guard_df(
            [100.0, 100.0, 100.0],
            signals=["HOLD", "BUY", "HOLD"],
        )
        df["earnings_date"] = pd.NaT
        df.loc[df.index[1], "earnings_date"] = df.index[1]

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals={sym: df},
            data={},
            all_dates=list(df.index),
            initial_capital=100_000.0,
        )

        assert [t["action"] for t in result["trades"]] == []
        assert result["earnings_buy_blocks"] == 1
        assert result["skipped_reasons"]["earnings_window"] == 1

    def test_gap_down_exit_preempts_close_stop_loss(self):
        from backtest.portfolio_backtester import PortfolioBacktester

        pbt = PortfolioBacktester(_PortfolioGuardConfig(gap_enabled=True))
        sym = "GUARD03"
        df = _make_portfolio_guard_df(
            [100.0, 96.0, 96.0],
            open_=[100.0, 95.0, 96.0],
            signals=["BUY", "HOLD", "HOLD"],
        )

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals={sym: df},
            data={},
            all_dates=list(df.index),
            initial_capital=100_000.0,
        )

        assert [t["action"] for t in result["trades"]] == ["BUY", "GAP_DOWN"]
        assert result["gap_down_exits"] == 1
        assert result["exit_reason_counts"]["GAP_DOWN"] == 1

    def test_blackswan_exit_blocks_cooldown_and_scales_recovery_buy(self):
        from backtest.portfolio_backtester import PortfolioBacktester

        pbt = PortfolioBacktester(
            _PortfolioGuardConfig(
                gap_enabled=False,
                blackswan_enabled=True,
                recovery_scale=0.5,
            )
        )
        sym = "GUARD04"
        df = _make_portfolio_guard_df(
            [100.0, 94.0, 94.0, 100.0],
            open_=[100.0, 100.0, 94.0, 100.0],
            signals=["BUY", "HOLD", "BUY", "BUY"],
        )

        result = pbt._simulate_portfolio(
            symbols=[sym],
            signals={sym: df},
            data={},
            all_dates=list(df.index),
            initial_capital=100_000.0,
        )

        assert [t["action"] for t in result["trades"]] == ["BUY", "BLACKSWAN", "BUY"]
        assert result["blackswan_triggers"] == 1
        assert result["blackswan_buy_blocks"] == 1
        assert result["blackswan_recovery_buys"] == 1
        assert result["skipped_reasons"]["blackswan_cooldown"] == 1

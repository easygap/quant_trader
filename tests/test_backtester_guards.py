"""
백테스터 안전장치 테스트
- 유동성 필터 (주문량 제한)
- 월간 거래 횟수 제한
- Z-Score 0 표준편차 방어
"""

import numpy as np
import pandas as pd
import pytest


# ─── 유동성 필터 테스트 ───────────────────────────────

def _make_df(n=60, volume=1000):
    """OHLCV DataFrame 생성 (signal 컬럼 포함)."""
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = 10000 + np.cumsum(np.random.randn(n) * 100)
    close = np.maximum(close, 1000)
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": [volume] * n,
        },
        index=dates,
    )


class _BacktestGuardConfig:
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
            "take_profit": {"fixed_rate": 0.50, "partial_exit": False},
            "trailing_stop": {"enabled": False},
            "position_sizing": {"max_risk_per_trade": 0.01, "initial_capital": 100_000_000},
            "diversification": {"max_position_ratio": 0.20, "max_investment_ratio": 0.70},
            "position_limits": {"min_holding_days": 0, "max_holding_days": 0},
            "liquidity_filter": {"backtest_max_participation_rate": 1.0},
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
            "backtest_regime_filter": {"enabled": False},
        }

    @property
    def risk_params(self):
        return self._risk_params

    @property
    def trading(self):
        return self._trading


def _make_guard_df(close, *, open_=None, signals=None, volume=1_000_000):
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
        },
        index=dates,
    )
    df["_avg_daily_volume"] = df["volume"]
    return df


class TestLiquidityFilter:
    """백테스터 유동성 필터: 주문량이 일평균 거래량의 N%를 초과하면 축소."""

    def test_low_volume_limits_quantity(self):
        """거래량이 매우 낮으면 매수 수량이 제한되어야 함."""
        from config.config_loader import Config

        config = Config.get()
        # 유동성 필터 강제 설정
        liq = config.risk_params.setdefault("liquidity_filter", {})
        liq["backtest_max_participation_rate"] = 0.01  # 1%

        from backtest.backtester import Backtester

        bt = Backtester(config)

        # 거래량 100주인 종목 → 최대 매수 1주
        df = _make_df(n=60, volume=100)
        df["signal"] = "HOLD"
        df.iloc[30, df.columns.get_loc("signal")] = "BUY"
        df.iloc[55, df.columns.get_loc("signal")] = "SELL"

        result = bt._simulate(df, initial_capital=100_000_000)
        buy_trades = [t for t in result["trades"] if t["action"] == "BUY"]

        if buy_trades:
            # 거래량 100주 × 1% = 1주가 최대
            assert buy_trades[0]["quantity"] <= 1, (
                f"유동성 필터 미작동: {buy_trades[0]['quantity']}주 매수 (최대 1주)"
            )

    def test_high_volume_no_limit(self):
        """거래량이 충분하면 유동성 필터가 수량을 제한하지 않아야 함."""
        from config.config_loader import Config

        config = Config.get()
        liq = config.risk_params.setdefault("liquidity_filter", {})
        liq["backtest_max_participation_rate"] = 0.01

        from backtest.backtester import Backtester

        bt = Backtester(config)

        df = _make_df(n=60, volume=1_000_000)
        df["signal"] = "HOLD"
        df.iloc[30, df.columns.get_loc("signal")] = "BUY"

        result = bt._simulate(df, initial_capital=100_000_000)
        buy_trades = [t for t in result["trades"] if t["action"] == "BUY"]

        if buy_trades:
            # 거래량 100만주 × 1% = 1만주 — 포지션 사이징 상한이 더 작을 수 있음
            assert buy_trades[0]["quantity"] > 1, "고유동성 종목인데 1주만 매수됨"


# ─── 월간 거래 횟수 제한 테스트 ───────────────────────

class TestMonthlyTradeLimit:
    """월간 왕복 거래 횟수 상한 초과 시 신규 매수 차단."""

    def test_monthly_cap_blocks_excess_buys(self):
        """max_monthly_roundtrips 초과 시 같은 달 내 매수가 차단되어야 함."""
        from config.config_loader import Config

        config = Config.get()
        pos_limits = config.risk_params.setdefault("position_limits", {})
        pos_limits["max_monthly_roundtrips"] = 2  # 월 2회 제한
        # min_holding_days를 0으로 → 짧은 사이클 허용
        pos_limits["min_holding_days"] = 0

        from backtest.backtester import Backtester

        bt = Backtester(config)

        # 같은 달(1월) 안에 BUY/SELL 3회 반복 — 간격 3일씩
        # bdate_range("2024-01-01", periods=22) → 22영업일 = 1월 내
        dates = pd.bdate_range("2024-01-01", periods=22)
        n = len(dates)
        close = np.full(n, 10000.0)
        df = pd.DataFrame(
            {"open": close, "high": close, "low": close, "close": close, "volume": [500_000] * n},
            index=dates,
        )
        df["signal"] = "HOLD"
        # 3번의 매수/매도 사이클: day 1/4, 7/10, 13/16
        for buy_day, sell_day in [(1, 4), (7, 10), (13, 16)]:
            df.iloc[buy_day, df.columns.get_loc("signal")] = "BUY"
            df.iloc[sell_day, df.columns.get_loc("signal")] = "SELL"

        result = bt._simulate(df, initial_capital=100_000_000)
        buy_trades = [t for t in result["trades"] if t["action"] == "BUY"]

        # 월 2회 제한이므로 3번째 매수는 차단되어야 함
        assert len(buy_trades) <= 2, (
            f"월간 거래 제한 미작동: {len(buy_trades)}회 매수 (최대 2회). "
            f"매수 날짜: {[str(t['date'].date()) for t in buy_trades]}"
        )


# ─── Z-Score 0 표준편차 방어 테스트 ──────────────────

class TestZScoreSafety:
    """z_std == 0 구간에서 Inf/NaN이 발생하지 않아야 함."""

    def test_flat_price_no_inf(self):
        """가격이 완전히 동일한 구간에서 z_score가 Inf가 아닌 0이어야 함."""
        from strategies.mean_reversion import MeanReversionStrategy

        strategy = MeanReversionStrategy()

        # 가격이 완전히 동일 (std=0)
        n = 40
        dates = pd.bdate_range("2024-01-01", periods=n)
        flat_price = 10000.0
        df = pd.DataFrame(
            {
                "open": [flat_price] * n,
                "high": [flat_price] * n,
                "low": [flat_price] * n,
                "close": [flat_price] * n,
                "volume": [100000] * n,
            },
            index=dates,
        )

        result = strategy.analyze(df)

        assert "z_score" in result.columns
        z_scores = result["z_score"]
        assert not z_scores.isin([np.inf, -np.inf]).any(), "Inf 발생"
        assert not z_scores.isna().all(), "전부 NaN"
        # 표준편차 0이면 z_score는 0이어야 함
        assert (z_scores.dropna() == 0).all(), f"z_score가 0이 아님: {z_scores.dropna().unique()}"


class TestBacktestRiskEventGuards:
    """paper/live 리스크 이벤트를 단일종목 백테스트에도 반영."""

    def test_gap_up_blocks_new_buy(self):
        from backtest.backtester import Backtester

        bt = Backtester(_BacktestGuardConfig(gap_enabled=True))
        df = _make_guard_df(
            [100.0, 100.0, 100.0],
            open_=[100.0, 106.0, 100.0],
            signals=["HOLD", "BUY", "HOLD"],
        )

        result = bt._simulate(df, initial_capital=100_000.0)

        assert [t["action"] for t in result["trades"]] == []
        assert result["gap_up_buy_blocks"] == 1

    def test_earnings_window_blocks_new_buy(self):
        from backtest.backtester import Backtester

        bt = Backtester(_BacktestGuardConfig(skip_earnings_days=3))
        df = _make_guard_df(
            [100.0, 100.0, 100.0],
            signals=["HOLD", "BUY", "HOLD"],
        )
        df["earnings_date"] = pd.NaT
        df.loc[df.index[1], "earnings_date"] = df.index[1]

        result = bt._simulate(df, initial_capital=100_000.0)

        assert [t["action"] for t in result["trades"]] == []
        assert result["earnings_buy_blocks"] == 1

    def test_gap_down_exit_preempts_close_stop_loss(self):
        from backtest.backtester import Backtester

        bt = Backtester(_BacktestGuardConfig(gap_enabled=True))
        df = _make_guard_df(
            [100.0, 96.0, 96.0],
            open_=[100.0, 95.0, 96.0],
            signals=["BUY", "HOLD", "HOLD"],
        )

        result = bt._simulate(df, initial_capital=100_000.0)
        actions = [t["action"] for t in result["trades"]]

        assert actions == ["BUY", "GAP_DOWN"]
        assert result["gap_down_exits"] == 1

    def test_blackswan_exit_blocks_cooldown_and_scales_recovery_buy(self):
        from backtest.backtester import Backtester

        bt = Backtester(
            _BacktestGuardConfig(
                gap_enabled=False,
                blackswan_enabled=True,
                recovery_scale=0.5,
            )
        )
        df = _make_guard_df(
            [100.0, 94.0, 94.0, 100.0],
            open_=[100.0, 100.0, 94.0, 100.0],
            signals=["BUY", "HOLD", "BUY", "BUY"],
        )

        result = bt._simulate(df, initial_capital=100_000.0)
        actions = [t["action"] for t in result["trades"]]

        assert actions == ["BUY", "BLACKSWAN", "BUY"]
        assert result["blackswan_triggers"] == 1
        assert result["blackswan_buy_blocks"] == 1
        assert result["blackswan_recovery_buys"] == 1

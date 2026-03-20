"""
통합 스모크 테스트 — pytest 수집 가능 버전
- 설정, DB, 지표, 신호, 리스크, 백테스트, 리포트, 디스코드, 포트폴리오 등 모듈 로드 및 기본 동작 검증
"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest


def _sample_ohlcv(days: int = 500):
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
    returns = np.random.normal(0.0003, 0.02, days)
    prices = 50000 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": prices * (1 + np.random.uniform(-0.01, 0.01, days)),
        "high": prices * (1 + np.random.uniform(0, 0.03, days)),
        "low": prices * (1 - np.random.uniform(0, 0.03, days)),
        "close": prices,
        "volume": np.random.randint(100000, 5000000, days),
    }, index=dates)
    df.index.name = "date"
    return df


class TestConfigAndDb:
    def test_config_load(self):
        from config.config_loader import Config
        config = Config.get()
        assert config.active_strategy
        assert isinstance(config.discord, dict)
        assert config.risk_params

    def test_database_init(self):
        from database.models import init_database
        engine = init_database()
        assert engine is not None


class TestIndicatorsAndSignal:
    def test_indicator_engine(self):
        from core.indicator_engine import IndicatorEngine
        df = _sample_ohlcv(100)
        out = IndicatorEngine().calculate_all(df)
        assert "rsi" in out.columns
        assert "macd" in out.columns
        assert "adx" in out.columns
        assert "atr" in out.columns
        assert "obv" in out.columns
        assert "volume_ratio" in out.columns

    def test_signal_generator(self):
        from core.indicator_engine import IndicatorEngine
        from core.signal_generator import SignalGenerator
        df = _sample_ohlcv(100)
        df = IndicatorEngine().calculate_all(df)
        out = SignalGenerator().generate(df)
        assert "signal" in out.columns
        latest = SignalGenerator().get_latest_signal(out)
        assert latest["signal"] in ("BUY", "SELL", "HOLD")


class TestRiskManager:
    def test_risk_manager_calculations(self):
        from core.risk_manager import RiskManager
        rm = RiskManager()
        assert rm.calculate_stop_loss(50000) > 0
        assert rm.calculate_take_profit(50000)["target_final"] > 0
        assert rm.calculate_position_size(10_000_000, 50000, 48000) > 0
        assert rm.check_mdd(10_000_000)["mdd"] >= 0
        costs = rm.calculate_transaction_costs(50000, 10, "BUY")
        assert costs["total_cost"] > 0


class TestStrategies:
    def test_strategies_generate_signal(self):
        from strategies.scoring_strategy import ScoringStrategy
        from strategies.mean_reversion import MeanReversionStrategy
        from strategies.trend_following import TrendFollowingStrategy
        df = _sample_ohlcv(320)
        assert ScoringStrategy().generate_signal(df)["signal"] in ("BUY", "SELL", "HOLD")
        assert MeanReversionStrategy().generate_signal(df)["signal"] in ("BUY", "SELL", "HOLD")
        assert TrendFollowingStrategy().generate_signal(df)["signal"] in ("BUY", "SELL", "HOLD")


class TestBacktestAndReport:
    def test_backtester_all_strategies(self):
        from backtest.backtester import Backtester
        df = _sample_ohlcv(320)
        bt = Backtester()
        for name in ("scoring", "mean_reversion", "trend_following", "ensemble"):
            result = bt.run(df.copy(), strategy_name=name)
            assert result.get("metrics")
            assert "total_return" in result["metrics"]
            assert "sharpe_ratio" in result["metrics"]
            m = result["metrics"]
            for key in (
                "avg_holding_days",
                "total_commission",
                "commission_to_profit_ratio",
                "monthly_roundtrips_per_symbol",
                "annual_roundtrips_total",
            ):
                assert key in m
            assert "overtrading_warnings" in result

    def test_report_generator(self):
        from backtest.backtester import Backtester
        from backtest.report_generator import ReportGenerator
        df = _sample_ohlcv(320)
        result = Backtester().run(df, strategy_name="scoring")
        rg = ReportGenerator()
        assert rg.generate_text_report(result) is not None
        assert rg.generate_html_report(result) is not None


class TestMonitoringAndApi:
    def test_discord_bot_console_fallback(self):
        from monitoring.discord_bot import DiscordBot
        bot = DiscordBot()
        assert bot.send_message("smoke") is True
        assert bot.send_embed("title", "desc") is True

    def test_kis_api_init(self):
        from api.kis_api import KISApi
        api = KISApi()
        assert api is not None

    def test_websocket_handler_init(self):
        pytest.importorskip("websockets")
        from api.websocket_handler import WebSocketHandler
        ws = WebSocketHandler()
        assert ws is not None
        ws.on_price_update(lambda x: None)

    def test_portfolio_summary(self):
        from core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
        summary = pm.get_portfolio_summary()
        assert "total_value" in summary
        assert summary["total_value"] >= 0
        assert "realized_pnl" in summary
        assert "unrealized_pnl" in summary

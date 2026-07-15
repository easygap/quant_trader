import pandas as pd
import pytest


def test_cost_impact_marks_cost_flipped_result_as_fail():
    from backtest.cost_impact import summarize_cost_impact

    summary = summarize_cost_impact({
        "initial_capital": 1_000.0,
        "final_value": 990.0,
        "total_return": -1.0,
        "total_commission": 8.0,
        "total_tax": 2.0,
        "total_slippage_cost": 10.0,
    })

    assert summary["gross_return_estimate_pct"] == 1.0
    assert summary["net_return_pct"] == -1.0
    assert summary["cost_drag_pct"] == 2.0
    assert summary["cost_drag_bps"] == 200.0
    assert summary["status"] == "fail"
    assert "costs_flip_profitable_gross_to_net_loss" in summary["issues"]


def test_portfolio_backtester_adds_cost_impact_metrics():
    from backtest.portfolio_backtester import PortfolioBacktester

    class _Config:
        risk_params = {}

    equity = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "value": [1_000.0, 990.0],
        "n_positions": [1, 0],
    })
    trades = [
        {
            "date": pd.Timestamp("2026-01-02"),
            "symbol": "AAA",
            "action": "BUY",
            "price": 100.0,
            "quantity": 5,
            "pnl": 0.0,
            "commission": 3.0,
            "tax": 0.0,
            "slippage_cost": 4.0,
        },
        {
            "date": pd.Timestamp("2026-01-05"),
            "symbol": "AAA",
            "action": "SELL",
            "price": 98.0,
            "quantity": 5,
            "pnl": -10.0,
            "commission": 3.0,
            "tax": 2.0,
            "slippage_cost": 4.0,
        },
    ]

    metrics = PortfolioBacktester(_Config())._calculate_portfolio_metrics(
        {"equity_curve": equity, "trades": trades},
        initial_capital=1_000.0,
    )

    assert metrics["total_transaction_cost"] == 16.0
    assert metrics["gross_return"] == 0.6
    assert metrics["cost_drag_pct"] == 1.6
    assert metrics["cost_impact_status"] == "fail"


def test_single_backtester_metrics_anchor_first_day_to_initial_capital():
    """첫날 즉시 발생한 손실/비용도 MDD와 일수익률에 포함한다."""
    from backtest.backtester import Backtester

    class _Config:
        risk_params = {}

    equity = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "value": [900.0, 900.0],
        "cash": [900.0, 900.0],
        "position_value": [0.0, 0.0],
    })
    metrics = Backtester(_Config())._calculate_metrics(
        {"equity_curve": equity, "trades": []},
        initial_capital=1_000.0,
    )

    assert metrics["total_return"] == -10.0
    assert metrics["max_drawdown"] == -10.0
    assert equity["daily_return"].iloc[0] == pytest.approx(-0.1)
    assert metrics["sharpe_ratio"] < 0


def test_portfolio_metrics_anchor_first_day_to_initial_capital():
    """포트폴리오 백테스터도 첫 관측일 이전 초기자본 기준점을 보존한다."""
    from backtest.portfolio_backtester import PortfolioBacktester

    class _Config:
        risk_params = {}

    equity = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "value": [900.0, 900.0],
        "n_positions": [1, 1],
    })
    metrics = PortfolioBacktester(_Config())._calculate_portfolio_metrics(
        {"equity_curve": equity, "trades": []},
        initial_capital=1_000.0,
    )

    assert metrics["total_return"] == -10.0
    assert metrics["max_drawdown"] == -10.0
    assert equity["daily_return"].iloc[0] == pytest.approx(-0.1)
    assert metrics["sharpe_ratio"] < 0


def test_text_report_contains_cost_before_after_section(tmp_path):
    from backtest.cost_impact import summarize_cost_impact
    from backtest.report_generator import ReportGenerator

    metrics = {
        "initial_capital": 1_000.0,
        "final_value": 990.0,
        "total_return": -1.0,
        "annual_return": -1.0,
        "sharpe_ratio": -0.1,
        "max_drawdown": -1.0,
        "calmar_ratio": 0.0,
        "total_trades": 1,
        "win_rate": 0.0,
        "winning_trades": 0,
        "losing_trades": 1,
        "profit_factor": 0.0,
        "avg_win": 0.0,
        "avg_loss": 10.0,
        "total_commission": 8.0,
        "total_tax": 2.0,
        "total_slippage_cost": 10.0,
        "commission_to_profit_ratio": None,
        "monthly_roundtrips_per_symbol": 1.0,
        "annual_roundtrips_total": 12.0,
    }
    metrics["cost_impact"] = summarize_cost_impact(metrics)
    result = {
        "strategy": "cost_report_s",
        "period": "2026-01-02 ~ 2026-01-05",
        "metrics": metrics,
        "trades": [],
        "equity_curve": pd.DataFrame(),
    }

    text = ReportGenerator(output_dir=str(tmp_path)).generate_text_report(result)

    assert "[ 비용 전/후 성과 비교 ]" in text
    assert "비용 차감 전 추정 수익률" in text
    assert "비용 드래그" in text


class _SymbolTaxBacktestConfig:
    trading = {"market_regime_filter": False, "skip_earnings_days": 0}
    risk_params = {
        "position_sizing": {"max_risk_per_trade": 0.01},
        "stop_loss": {"type": "fixed", "fixed_rate": 0.03},
        "take_profit": {"fixed_rate": 0.08, "partial_exit": False},
        "trailing_stop": {"enabled": False},
        "diversification": {
            "max_position_ratio": 0.20,
            "max_investment_ratio": 0.70,
        },
        "position_limits": {"min_holding_days": 0, "max_holding_days": 0},
        "backtest_regime_filter": {"enabled": False},
        "transaction_costs": {
            "commission_rate": 0.0,
            "tax_rate": 0.002,
            "tax_exempt_symbols": ["069500", "357870"],
            "holding_period_income_tax": {
                "enabled": True,
                "rate": 0.154,
                "symbols": ["357870"],
            },
            "slippage": 0.0,
            "slippage_ticks": 0,
            "dynamic_slippage": {"enabled": False},
        },
    }


class _PassthroughSignalStrategy:
    @staticmethod
    def analyze(df):
        return df.copy()


def _run_symbol_tax_backtest(symbol):
    from backtest.backtester import Backtester

    df = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [100.0, 101.0],
            "low": [100.0, 101.0],
            "close": [100.0, 101.0],
            "volume": [1_000_000.0, 1_000_000.0],
            "signal": ["BUY", "SELL"],
        },
        index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
    )
    backtester = Backtester(_SymbolTaxBacktestConfig())
    backtester._get_strategy = lambda _name: _PassthroughSignalStrategy()
    return backtester.run(
        df,
        strategy_name="passthrough",
        initial_capital=1_000_000.0,
        strict_lookahead=False,
        symbol=symbol,
        execution_model="legacy_same_close",
    )


def test_backtester_kr_equity_etf_sell_is_transaction_tax_exempt():
    result = _run_symbol_tax_backtest("069500")

    sell = next(t for t in result["trades"] if t["action"] == "SELL")
    assert sell["tax"] == 0.0
    assert result["metrics"]["total_tax"] == 0.0


def test_backtester_other_etf_sell_applies_holding_period_income_tax():
    result = _run_symbol_tax_backtest("357870")

    buy = next(t for t in result["trades"] if t["action"] == "BUY")
    sell = next(t for t in result["trades"] if t["action"] == "SELL")
    expected = round(
        (sell["price"] - buy["price"]) * sell["quantity"] * 0.154,
        0,
    )
    assert expected > 0
    assert sell["tax"] == pytest.approx(expected)
    assert result["metrics"]["total_tax"] == pytest.approx(expected)

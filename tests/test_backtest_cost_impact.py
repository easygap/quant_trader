import pandas as pd


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

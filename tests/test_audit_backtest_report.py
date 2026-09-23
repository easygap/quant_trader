"""백테스트 리포트 감사 회귀 테스트.

- 리포트 거래표가 엔진 지표와 같은 청산 목록(PNL_EXIT_ACTIONS)을 쓴다.
"""

import re
from pathlib import Path

import pandas as pd


def _report_metrics():
    from backtest.cost_impact import summarize_cost_impact

    metrics = {
        "initial_capital": 1_000_000.0,
        "final_value": 900_000.0,
        "total_return": -10.0,
        "annual_return": -10.0,
        "cagr": -10.0,
        "sharpe_ratio": -0.5,
        "max_drawdown": -12.0,
        "calmar_ratio": -0.83,
        "total_trades": 3,
        "win_rate": 0.0,
        "winning_trades": 0,
        "losing_trades": 3,
        "profit_factor": 0.0,
        "avg_win": 0.0,
        "avg_loss": 30_000.0,
        "total_commission": 0.0,
        "total_tax": 0.0,
        "total_slippage_cost": 0.0,
        "commission_to_profit_ratio": None,
        "monthly_roundtrips_per_symbol": 1.0,
        "annual_roundtrips_total": 12.0,
    }
    metrics["cost_impact"] = summarize_cost_impact(metrics)
    return metrics


def _risk_exit_trades():
    trades = []
    for i, action in enumerate(("GAP_DOWN", "BLACKSWAN", "MAX_HOLD")):
        day = pd.Timestamp("2024-01-02") + pd.Timedelta(days=7 * i)
        trades.append(
            {"date": day, "action": "BUY", "price": 100.0, "quantity": 100, "pnl": 0, "pnl_rate": 0}
        )
        trades.append(
            {
                "date": day + pd.Timedelta(days=3),
                "action": action,
                "price": 97.0,
                "quantity": 100,
                "pnl": -30_000.0,
                "pnl_rate": -3.0,
            }
        )
    return trades


def test_report_uses_engine_exit_action_set():
    import backtest.backtester as backtester_mod
    import backtest.report_generator as report_mod

    assert report_mod.PNL_EXIT_ACTIONS is backtester_mod.PNL_EXIT_ACTIONS
    assert {"GAP_DOWN", "BLACKSWAN", "MAX_HOLD", "TAKE_PROFIT_PARTIAL"} <= report_mod.PNL_EXIT_ACTIONS


def test_text_report_lists_gap_down_and_blackswan_sells(tmp_path):
    from backtest.report_generator import ReportGenerator

    result = {
        "strategy": "audit_exit_actions",
        "period": "2024-01-02 ~ 2024-01-31",
        "metrics": _report_metrics(),
        "trades": _risk_exit_trades(),
        "equity_curve": pd.DataFrame(),
    }

    text = ReportGenerator(output_dir=str(tmp_path)).generate_text_report(result)

    recent = text.split("[ 최근 매도 거래 (최대 10건) ]", 1)[1]
    for action in ("GAP_DOWN", "BLACKSWAN", "MAX_HOLD"):
        assert action in recent


def test_html_trades_table_lists_gap_down_and_blackswan_sells():
    from backtest.report_generator import ReportGenerator

    html = ReportGenerator._generate_trades_table(_risk_exit_trades())

    for action in ("GAP_DOWN", "BLACKSWAN", "MAX_HOLD"):
        assert f"<td>{action}</td>" in html


def _emitted_exit_actions(path: str) -> set:
    src = Path(path).read_text(encoding="utf-8")
    patterns = (
        r'"action":\s*"([A-Z_]+)"',
        r'_execute_full_exit\(\s*"([A-Z_]+)"',
        r'\bsell_reason\s*=\s*"([A-Z_]+)"',
    )
    found = set()
    for pattern in patterns:
        found |= set(re.findall(pattern, src))
    found.discard("BUY")
    return found


def test_every_exit_action_emitted_by_engines_is_in_pnl_exit_actions():
    """엔진이 새 청산 사유를 기록하면 지표·리포트 공용 목록에도 들어 있어야 한다."""
    import backtest.backtester as backtester_mod
    import backtest.portfolio_backtester as portfolio_mod

    single = _emitted_exit_actions(backtester_mod.__file__)
    portfolio = _emitted_exit_actions(portfolio_mod.__file__)

    # 패턴이 코드 형태 변화로 아무것도 못 찾는 경우를 막기 위한 하한 확인
    assert {
        "SELL", "STOP_LOSS", "TAKE_PROFIT", "TAKE_PROFIT_PARTIAL",
        "TRAILING_STOP", "MAX_HOLD", "GAP_DOWN", "BLACKSWAN",
    } <= single
    assert {
        "SELL", "STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP",
        "MAX_HOLD", "GAP_DOWN", "BLACKSWAN",
    } <= portfolio
    assert (single | portfolio) <= backtester_mod.PNL_EXIT_ACTIONS

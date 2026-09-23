"""백테스트 성과 지표 감사 회귀 테스트.

- 보유기간 만료(MAX_HOLD) 청산이 승률·손익비·거래 수·보유 기간에서 빠지지 않는다.
- 칼마 비율은 부호 있는 CAGR / |MDD|.
- 샤프·소르티노의 무위험수익률은 이름 있는 상수 하나(3%)이고 리포트에 표기된다.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest


class _SingleConfig:
    """단일 종목 Backtester용 최소 설정 (외부 데이터·비용 없음)."""

    settings = {}
    strategies = {}

    def __init__(self, *, max_holding_days=0, min_holding_days=0):
        self._trading = {"skip_earnings_days": 0}
        self._risk_params = {
            "transaction_costs": {
                "commission_rate": 0.0,
                "tax_rate": 0.0,
                "slippage": 0.0,
                "slippage_ticks": 0,
                "dynamic_slippage": {"enabled": False},
            },
            "stop_loss": {"type": "fixed", "fixed_rate": 0.50},
            "take_profit": {"fixed_rate": 0.50, "partial_exit": False},
            "trailing_stop": {"enabled": False},
            "position_sizing": {"max_risk_per_trade": 0.01, "initial_capital": 1_000_000},
            "diversification": {"max_position_ratio": 0.20, "max_investment_ratio": 0.70},
            "position_limits": {
                "min_holding_days": min_holding_days,
                "max_holding_days": max_holding_days,
                "max_monthly_roundtrips": 0,
            },
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


def _frame(close, *, open_=None, signals=None, start="2024-01-01"):
    dates = pd.bdate_range(start, periods=len(close))
    close = np.asarray(close, dtype=float)
    open_ = close if open_ is None else np.asarray(open_, dtype=float)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": [1_000_000] * len(close),
            "signal": signals or ["HOLD"] * len(close),
        },
        index=dates,
    )
    df["_avg_daily_volume"] = df["volume"]
    return df


def test_max_hold_exit_is_counted_in_trade_metrics():
    """30일 만료로 닫힌 거래가 거래 수·승률·보유 기간에 그대로 잡혀야 한다."""
    from backtest.backtester import Backtester

    bt = Backtester(_SingleConfig(max_holding_days=30))
    n = 30
    close = 100.0 - np.arange(n) * 0.1  # 손절·익절에 걸리지 않는 완만한 하락
    signals = ["BUY"] + ["HOLD"] * (n - 1)
    df = _frame(close, signals=signals)

    result = bt._simulate(df, initial_capital=1_000_000.0)
    actions = [t["action"] for t in result["trades"]]
    assert actions == ["BUY", "MAX_HOLD"]
    buy, max_hold = result["trades"]
    assert (max_hold["date"] - buy["date"]).days == 30
    assert max_hold["pnl"] < 0

    metrics = bt._calculate_metrics(result, initial_capital=1_000_000.0)
    assert metrics["total_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["winning_trades"] == 0
    assert metrics["win_rate"] == 0
    assert metrics["avg_holding_days"] == pytest.approx(30.0)
    assert metrics["ev_per_trade"] < 0
    assert metrics["max_consecutive_losses"] == 1


def test_partial_exit_keeps_holding_clock_until_full_exit():
    """부분 익절 뒤 MAX_HOLD로 잔량이 닫히면 두 매도 모두 같은 매수일 기준으로 보유 기간을 잰다."""
    from backtest.backtester import Backtester, PNL_EXIT_ACTIONS

    buy_date = pd.Timestamp("2024-01-02")
    trades = [
        {"date": buy_date, "action": "BUY", "price": 100.0, "quantity": 10, "pnl": 0},
        {
            "date": buy_date + pd.Timedelta(days=5),
            "action": "TAKE_PROFIT_PARTIAL",
            "price": 105.0,
            "quantity": 5,
            "pnl": 25.0,
        },
        {
            "date": buy_date + pd.Timedelta(days=30),
            "action": "MAX_HOLD",
            "price": 99.0,
            "quantity": 5,
            "pnl": -5.0,
        },
    ]
    assert {"MAX_HOLD", "TAKE_PROFIT_PARTIAL"} <= PNL_EXIT_ACTIONS
    equity = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-02", periods=22),
            "value": [1_000.0] * 22,
        }
    )
    metrics = Backtester(_SingleConfig())._calculate_metrics(
        {"equity_curve": equity, "trades": trades}, initial_capital=1_000.0
    )

    assert metrics["total_trades"] == 2
    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["avg_holding_days"] == pytest.approx((5 + 30) / 2)


# ─── 칼마 비율: 부호 있는 CAGR / |MDD| ────────────────────────────


def _equity(values, start="2020-01-01"):
    values = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range(start, periods=len(values)),
            "value": values,
            "n_positions": [1] * len(values),
        }
    )


def _single_metrics(values, initial_capital=1_000.0):
    from backtest.backtester import Backtester

    return Backtester(_SingleConfig())._calculate_metrics(
        {"equity_curve": _equity(values), "trades": []}, initial_capital=initial_capital
    )


def _portfolio_metrics(values, initial_capital=1_000.0):
    from backtest.portfolio_backtester import PortfolioBacktester

    return PortfolioBacktester(_SingleConfig())._calculate_portfolio_metrics(
        {"equity_curve": _equity(values), "trades": []}, initial_capital=initial_capital
    )


@pytest.mark.parametrize("metrics_fn", [_single_metrics, _portfolio_metrics])
def test_calmar_is_negative_for_a_losing_strategy(metrics_fn):
    """꾸준히 잃는 전략의 칼마는 음수여야 한다 (예전엔 abs()로 양수)."""
    metrics = metrics_fn(np.linspace(990.0, 900.0, 252))

    assert metrics["total_return"] < 0
    assert metrics["max_drawdown"] < 0
    assert metrics["calmar_ratio"] < 0
    # 산술 연간 수익률 필드는 그대로 보고한다.
    assert metrics["annual_return"] == pytest.approx(-10.0, abs=0.01)


@pytest.mark.parametrize("metrics_fn", [_single_metrics, _portfolio_metrics])
def test_calmar_uses_cagr_not_arithmetic_annual_return(metrics_fn):
    """2년 +21%(CAGR 10%)에 MDD -10%면 칼마는 1.0 (산술 연수익 10.5% 기준이면 1.05)."""
    values = np.concatenate(([900.0], np.linspace(900.0, 1_210.0, 503)))
    assert len(values) == 504  # 504 / 252 = 2년

    metrics = metrics_fn(values)

    assert metrics["max_drawdown"] == pytest.approx(-10.0)
    assert metrics["cagr"] == pytest.approx(10.0, abs=0.01)
    assert metrics["annual_return"] == pytest.approx(10.5, abs=0.01)
    assert metrics["calmar_ratio"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("metrics_fn", [_single_metrics, _portfolio_metrics])
def test_calmar_is_zero_without_drawdown(metrics_fn):
    metrics = metrics_fn(np.linspace(1_000.0, 1_100.0, 252))

    assert metrics["max_drawdown"] == 0
    assert metrics["calmar_ratio"] == 0.0


# ─── 샤프 무위험수익률: 이름 있는 상수 하나 + 리포트 표기 ───────────────


def test_backtest_risk_free_constant_keeps_three_percent():
    from backtest.backtester import BACKTEST_RISK_FREE_ANNUAL, BACKTEST_RISK_FREE_LABEL

    # 값을 바꾸면 min_sharpe·OOS 게이트 등 이 기준에 맞춘 문턱을 재기준화해야 한다.
    assert BACKTEST_RISK_FREE_ANNUAL == 0.03
    assert BACKTEST_RISK_FREE_LABEL == "rf 3%"


def test_both_engines_compute_sharpe_with_the_shared_risk_free_rate():
    from backtest.backtester import BACKTEST_RISK_FREE_ANNUAL

    rng = np.random.default_rng(7)
    values = 1_000.0 * np.cumprod(1 + rng.normal(0.0008, 0.01, 300))
    single = _single_metrics(values)
    portfolio = _portfolio_metrics(values)

    daily = pd.Series(values).pct_change()
    daily.iloc[0] = values[0] / 1_000.0 - 1.0  # 두 엔진 모두 첫날을 초기자본 대비로 포함
    expected = (daily.mean() * 252 - BACKTEST_RISK_FREE_ANNUAL) / (daily.std() * np.sqrt(252))

    assert single["sharpe_ratio"] == pytest.approx(round(expected, 2))
    assert portfolio["sharpe_ratio"] == single["sharpe_ratio"]
    assert portfolio["sortino_ratio"] == single["sortino_ratio"]


def test_report_labels_show_backtest_risk_free_rate(tmp_path):
    from backtest.cost_impact import summarize_cost_impact
    from backtest.report_generator import ReportGenerator
    from backtest.strategy_validator import StrategyValidator

    metrics = {
        "initial_capital": 1_000.0, "final_value": 1_010.0, "total_return": 1.0,
        "annual_return": 1.0, "cagr": 1.0, "sharpe_ratio": 0.4, "max_drawdown": -2.0,
        "calmar_ratio": 0.5, "total_trades": 0, "win_rate": 0.0, "winning_trades": 0,
        "losing_trades": 0, "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "total_commission": 0.0, "total_tax": 0.0, "total_slippage_cost": 0.0,
        "commission_to_profit_ratio": None, "monthly_roundtrips_per_symbol": 0.0,
        "annual_roundtrips_total": 0.0,
    }
    metrics["cost_impact"] = summarize_cost_impact(metrics)
    result = {
        "strategy": "rf_label",
        "period": "2024-01-02 ~ 2024-12-30",
        "metrics": metrics,
        "trades": [],
        "equity_curve": pd.DataFrame(),
    }
    rg = ReportGenerator(output_dir=str(tmp_path))

    text = rg.generate_text_report(result)
    sharpe_line = next(line for line in text.splitlines() if "샤프 지수" in line)
    assert "rf 3%" in sharpe_line

    html = Path(rg.generate_html_report(result, filename="rf_label.html")).read_text(encoding="utf-8")
    assert "샤프 지수 (rf 3%)" in html

    section = StrategyValidator._format_section("OUT_OF_SAMPLE", metrics, {"sharpe_ratio": 0.1})
    assert section.count("샤프(rf 3%)") == 2

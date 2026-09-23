"""백테스트 리포트 감사 회귀 테스트.

- 리포트 거래표가 엔진 지표와 같은 청산 목록(PNL_EXIT_ACTIONS)을 쓴다.
- 실전 vs 백테스트 슬리피지 카드는 transaction_costs.slippage를 읽고 '하한'으로 표기한다.
"""

import re
from pathlib import Path

import pandas as pd
import pytest


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


# ─── 실전 vs 백테스트 슬리피지 카드 ─────────────────────────────


def test_backtest_slippage_reads_transaction_costs_key(monkeypatch):
    import backtest.report_generator as report_mod
    from config.config_loader import Config

    class _Cfg:
        # 최상위 slippage는 RiskManager가 읽지 않는 키 — 리포트도 무시해야 한다.
        risk_params = {"slippage": 0.009, "transaction_costs": {"slippage": 0.001}}

    monkeypatch.setattr(Config, "get", classmethod(lambda cls: _Cfg()))

    assert report_mod._default_backtest_slippage_pct() == pytest.approx(0.1)


def test_live_slippage_card_labels_backtest_value_as_fixed_rate_floor():
    import backtest.report_generator as report_mod

    summary = {
        "n": 3,
        "mean_pct": 0.12,
        "median_pct": 0.10,
        "max_abs_pct": 0.30,
        "backtest_assumed_pct": 0.05,
    }

    text = "\n".join(report_mod._format_live_slippage_text_table(summary))
    assert "transaction_costs.slippage" in text
    assert "하한" in text
    assert "risk_params.slippage" not in text

    html = report_mod._format_live_slippage_html_card(summary)
    assert "고정 비율 하한" in html


# ─── 시장 국면별 성과: 월수익률·국면 MDD 공식 ──────────────────────


def _q1_2024_dates():
    return pd.bdate_range("2024-01-01", "2024-03-29")


def _step_series(dates, jan, feb_mar):
    """1월은 jan, 2월 첫 거래일부터는 feb_mar (2월 첫날 갭 하락 후 보합)."""
    return [jan if d.month == 1 else feb_mar for d in dates]


def test_strategy_monthly_return_keeps_month_first_day_move():
    from backtest.report_generator import _strategy_monthly_returns

    dates = _q1_2024_dates()
    equity = pd.DataFrame({"date": dates, "value": _step_series(dates, 100.0, 90.0)})

    rets = _strategy_monthly_returns(equity, initial_capital=100.0)

    assert rets[pd.Period("2024-01", "M")] == pytest.approx(0.0)
    assert rets[pd.Period("2024-02", "M")] == pytest.approx(-0.10)
    assert rets[pd.Period("2024-03", "M")] == pytest.approx(0.0)


def test_strategy_first_month_is_measured_from_initial_capital():
    from backtest.report_generator import _strategy_monthly_returns

    dates = pd.bdate_range("2024-01-01", "2024-01-31")
    equity = pd.DataFrame({"date": dates, "value": [95.0] * len(dates)})

    assert _strategy_monthly_returns(equity, initial_capital=100.0).iloc[0] == pytest.approx(-0.05)
    # 초기자본을 모르면 첫 관측값 기준 (기존 동작과 같음)
    assert _strategy_monthly_returns(equity).iloc[0] == pytest.approx(0.0)


def test_kospi_first_month_uses_close_before_start():
    from backtest.report_generator import _kospi_monthly_returns_from_ohlc

    dates = pd.bdate_range("2023-12-20", "2024-01-31")
    close = [100.0 if d.year == 2023 else 105.0 for d in dates]  # 1월 첫 거래일 +5% 갭
    ks11 = pd.DataFrame({"close": close}, index=dates)

    rets = _kospi_monthly_returns_from_ohlc(ks11, start=pd.Timestamp("2024-01-01"))

    assert list(rets.index) == [pd.Period("2024-01", "M")]
    assert rets.iloc[0] == pytest.approx(0.05)


def test_regime_mdd_only_compounds_that_regimes_months():
    from backtest.report_generator import _mdd_from_monthly_returns

    # {1월, 3월}이 보합이면 사이의 2월 손실과 무관하게 MDD 0
    assert _mdd_from_monthly_returns([0.0, 0.0]) == pytest.approx(0.0)
    # 한 달만 있어도 그 달의 손실은 낙폭이다.
    assert _mdd_from_monthly_returns([-0.10]) == pytest.approx(-10.0)
    assert _mdd_from_monthly_returns([0.10, -0.20, 0.05]) == pytest.approx(-20.0)
    assert _mdd_from_monthly_returns([]) == 0.0


def test_market_regime_breakdown_classifies_month_opening_gap(monkeypatch):
    """2월 첫 거래일 -10% 갭은 2월을 하락장으로 분류하고, 보합인 1·3월 MDD에 섞이지 않는다."""
    from backtest.report_generator import (
        REGIME_BEAR,
        REGIME_SIDEWAYS,
        compute_market_regime_breakdown,
    )

    ks_dates = pd.bdate_range("2023-12-15", "2024-03-29")
    ks11 = pd.DataFrame(
        {"close": [2_500.0 if d < pd.Timestamp("2024-02-01") else 2_250.0 for d in ks_dates]},
        index=ks_dates,
    )

    class FakeCollector:
        def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
            assert symbol == "KS11"
            return ks11.loc[pd.Timestamp(start_date): pd.Timestamp(end_date)].copy()

    monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)

    dates = _q1_2024_dates()
    equity = pd.DataFrame({"date": dates, "value": _step_series(dates, 100.0, 90.0)})
    breakdown = compute_market_regime_breakdown(
        {"equity_curve": equity, "initial_capital": 100.0},
        warn_bear_underperformance=False,
    )

    bear = breakdown[REGIME_BEAR]
    assert bear["n_months"] == 1
    assert bear["avg_strat_pct"] == pytest.approx(-10.0)
    assert bear["avg_kospi_pct"] == pytest.approx(-10.0)
    assert bear["excess_pct"] == pytest.approx(0.0)
    assert bear["mdd_pct"] == pytest.approx(-10.0)

    sideways = breakdown[REGIME_SIDEWAYS]
    assert sideways["n_months"] == 2
    assert sideways["mdd_pct"] == pytest.approx(0.0)

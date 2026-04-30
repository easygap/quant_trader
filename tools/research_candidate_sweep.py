#!/usr/bin/env python3
"""
Research candidate sweep for portfolio-level strategy variants.

This is intentionally a research artifact, not a live/paper promotion path.
It ranks candidate variants, records benchmark excess metrics, and lets the
existing promotion engine label whether a candidate is even worth capped paper
study.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DEFAULT_START = "2023-01-01"
DEFAULT_END = "2025-12-31"
DEFAULT_INITIAL_CAPITAL = 10_000_000
DEFAULT_TOP_N = 20
DEFAULT_CANDIDATE_FAMILY = "rotation"
DEFAULT_OUTPUT_DIR = Path("reports/research_sweeps")
DEFAULT_RESEARCH_DIVERSIFICATION = {
    "max_positions": 2,
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}


def normalize_symbol(value: Any) -> str:
    """Normalize KR numeric codes that may lose leading zeroes in shells."""
    symbol = str(value).strip()
    if symbol.upper() == "KS11":
        return "KS11"
    if symbol.upper().endswith(".KS") and symbol[:-3].isdigit():
        return f"{symbol[:-3].zfill(6)}.KS"
    if symbol.isdigit() and len(symbol) <= 6:
        return symbol.zfill(6)
    return symbol


def normalize_symbols(symbols: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen = set()
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if code and code not in seen:
            normalized.append(code)
            seen.add(code)
    return normalized


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    strategy: str
    params: dict[str, Any]
    description: str
    diversification: dict[str, Any] | None = None


def get_git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def build_rotation_candidate_specs() -> list[CandidateSpec]:
    """Small, interpretable rotation variants for the first candidate factory."""
    return [
        CandidateSpec(
            candidate_id="rotation_base",
            strategy="relative_strength_rotation",
            params={},
            description="current config baseline",
        ),
        CandidateSpec(
            candidate_id="rotation_fast_momentum",
            strategy="relative_strength_rotation",
            params={
                "short_lookback": 40,
                "long_lookback": 100,
                "sma_period": 50,
                "short_weight": 0.7,
            },
            description="faster momentum and trend response",
        ),
        CandidateSpec(
            candidate_id="rotation_slow_momentum",
            strategy="relative_strength_rotation",
            params={
                "short_lookback": 80,
                "long_lookback": 160,
                "sma_period": 80,
                "short_weight": 0.5,
            },
            description="slower, lower-turnover momentum response",
        ),
        CandidateSpec(
            candidate_id="rotation_abs_momentum_a",
            strategy="relative_strength_rotation",
            params={"abs_momentum_filter": "A"},
            description="requires previous 120d absolute momentum > 0",
        ),
        CandidateSpec(
            candidate_id="rotation_abs_momentum_b",
            strategy="relative_strength_rotation",
            params={"abs_momentum_filter": "B"},
            description="requires previous 60d and 120d absolute momentum > 0",
        ),
        CandidateSpec(
            candidate_id="rotation_market_filter",
            strategy="relative_strength_rotation",
            params={"market_filter_sma200": True},
            description="allows new entries only when KS11 is above SMA200",
        ),
    ]


def build_momentum_candidate_specs() -> list[CandidateSpec]:
    """Simple momentum-factor variants for research-only discovery."""
    return [
        CandidateSpec(
            candidate_id="momentum_factor_base",
            strategy="momentum_factor",
            params={},
            description="current config baseline",
        ),
        CandidateSpec(
            candidate_id="momentum_factor_40d",
            strategy="momentum_factor",
            params={
                "lookback_days": 40,
                "buy_threshold_pct": 4.0,
                "sell_threshold_pct": -3.0,
            },
            description="medium-term price momentum",
        ),
        CandidateSpec(
            candidate_id="momentum_factor_60d",
            strategy="momentum_factor",
            params={
                "lookback_days": 60,
                "buy_threshold_pct": 6.0,
                "sell_threshold_pct": -4.0,
            },
            description="slower, stronger price momentum",
        ),
        CandidateSpec(
            candidate_id="momentum_factor_120d",
            strategy="momentum_factor",
            params={
                "lookback_days": 120,
                "buy_threshold_pct": 10.0,
                "sell_threshold_pct": -6.0,
            },
            description="longer horizon trend persistence",
        ),
    ]


def build_breakout_candidate_specs() -> list[CandidateSpec]:
    """Breakout-volume variants kept separate from promotion artifacts."""
    return [
        CandidateSpec(
            candidate_id="breakout_volume_base",
            strategy="breakout_volume",
            params={},
            description="current config baseline",
        ),
        CandidateSpec(
            candidate_id="breakout_volume_fast",
            strategy="breakout_volume",
            params={
                "breakout_period": 10,
                "surge_ratio": 1.3,
                "adx_min": 18,
            },
            description="faster breakout with looser volume/trend filters",
        ),
        CandidateSpec(
            candidate_id="breakout_volume_balanced",
            strategy="breakout_volume",
            params={
                "breakout_period": 20,
                "surge_ratio": 1.6,
                "adx_min": 20,
            },
            description="balanced breakout and volume confirmation",
        ),
        CandidateSpec(
            candidate_id="breakout_volume_strict",
            strategy="breakout_volume",
            params={
                "breakout_period": 40,
                "surge_ratio": 2.0,
                "adx_min": 22,
            },
            description="slower breakout with stricter confirmation",
        ),
    ]


def build_pullback_candidate_specs() -> list[CandidateSpec]:
    """Trend-pullback variants for testing dip entries inside an existing trend."""
    return [
        CandidateSpec(
            candidate_id="trend_pullback_base",
            strategy="trend_pullback",
            params={},
            description="current config baseline",
        ),
        CandidateSpec(
            candidate_id="trend_pullback_aggressive",
            strategy="trend_pullback",
            params={
                "sma_period": 50,
                "rsi_entry": 48,
                "adx_min": 18,
                "rsi_exit": 72,
            },
            description="more frequent pullback entries in medium trends",
        ),
        CandidateSpec(
            candidate_id="trend_pullback_balanced",
            strategy="trend_pullback",
            params={
                "sma_period": 60,
                "rsi_entry": 42,
                "adx_min": 20,
                "rsi_exit": 68,
            },
            description="balanced pullback entries with standard trend confirmation",
        ),
        CandidateSpec(
            candidate_id="trend_pullback_conservative",
            strategy="trend_pullback",
            params={
                "sma_period": 80,
                "rsi_entry": 38,
                "adx_min": 22,
                "rsi_exit": 65,
            },
            description="stricter pullback entries with slower trend confirmation",
        ),
    ]


def build_benchmark_relative_candidate_specs() -> list[CandidateSpec]:
    """Benchmark-relative momentum variants targeting same-universe excess returns."""
    return [
        CandidateSpec(
            candidate_id="benchmark_relative_momentum_60d",
            strategy="momentum_factor",
            params={
                "benchmark_relative": True,
                "benchmark_symbol": "KS11",
                "lookback_days": 60,
                "buy_threshold_pct": 3.0,
                "sell_threshold_pct": -2.0,
                "max_realized_vol_pct": 35.0,
            },
            description="60d stock momentum must exceed KS11 by at least 3%",
        ),
        CandidateSpec(
            candidate_id="benchmark_relative_momentum_120d",
            strategy="momentum_factor",
            params={
                "benchmark_relative": True,
                "benchmark_symbol": "KS11",
                "lookback_days": 120,
                "buy_threshold_pct": 6.0,
                "sell_threshold_pct": -3.0,
                "max_realized_vol_pct": 35.0,
            },
            description="120d stock momentum must exceed KS11 by at least 6%",
        ),
        CandidateSpec(
            candidate_id="benchmark_relative_momentum_lowvol",
            strategy="momentum_factor",
            params={
                "benchmark_relative": True,
                "benchmark_symbol": "KS11",
                "lookback_days": 120,
                "buy_threshold_pct": 4.0,
                "sell_threshold_pct": -2.0,
                "max_realized_vol_pct": 28.0,
                "sell_on_high_vol": True,
            },
            description="benchmark-relative momentum with a stricter volatility gate",
        ),
    ]


def build_risk_budget_candidate_specs() -> list[CandidateSpec]:
    """Exposure-structure variants for testing whether risk budget is the bottleneck."""
    balanced_budget = {
        "max_positions": 4,
        "max_position_ratio": 0.25,
        "max_investment_ratio": 0.80,
        "min_cash_ratio": 0.15,
    }
    defensive_budget = {
        "max_positions": 3,
        "max_position_ratio": 0.20,
        "max_investment_ratio": 0.60,
        "min_cash_ratio": 0.30,
    }
    return [
        CandidateSpec(
            candidate_id="risk_budget_momentum_120d_concentrated",
            strategy="momentum_factor",
            params={
                "lookback_days": 120,
                "buy_threshold_pct": 10.0,
                "sell_threshold_pct": -6.0,
            },
            description="120d momentum under the current concentrated research budget",
            diversification=DEFAULT_RESEARCH_DIVERSIFICATION,
        ),
        CandidateSpec(
            candidate_id="risk_budget_momentum_120d_balanced",
            strategy="momentum_factor",
            params={
                "lookback_days": 120,
                "buy_threshold_pct": 10.0,
                "sell_threshold_pct": -6.0,
            },
            description="120d momentum with more positions and lower single-name weight",
            diversification=balanced_budget,
        ),
        CandidateSpec(
            candidate_id="risk_budget_momentum_120d_defensive",
            strategy="momentum_factor",
            params={
                "lookback_days": 120,
                "buy_threshold_pct": 10.0,
                "sell_threshold_pct": -6.0,
            },
            description="120d momentum with lower gross exposure and higher cash reserve",
            diversification=defensive_budget,
        ),
        CandidateSpec(
            candidate_id="risk_budget_rotation_slow_balanced",
            strategy="relative_strength_rotation",
            params={
                "short_lookback": 80,
                "long_lookback": 160,
                "sma_period": 80,
                "short_weight": 0.5,
            },
            description="slow rotation with balanced exposure instead of concentration",
            diversification=balanced_budget,
        ),
        CandidateSpec(
            candidate_id="risk_budget_rotation_slow_defensive",
            strategy="relative_strength_rotation",
            params={
                "short_lookback": 80,
                "long_lookback": 160,
                "sma_period": 80,
                "short_weight": 0.5,
            },
            description="slow rotation with lower gross exposure and higher cash reserve",
            diversification=defensive_budget,
        ),
    ]


def build_cash_switch_candidate_specs() -> list[CandidateSpec]:
    """Market-filter exit variants that switch to cash when broad market trend breaks."""
    defensive_budget = {
        "max_positions": 3,
        "max_position_ratio": 0.20,
        "max_investment_ratio": 0.60,
        "min_cash_ratio": 0.30,
    }
    return [
        CandidateSpec(
            candidate_id="cash_switch_rotation_sma200",
            strategy="relative_strength_rotation",
            params={
                "market_filter_sma200": True,
                "market_filter_exit": True,
                "market_filter_ma_period": 200,
            },
            description="rotation blocks entries and exits to cash when KS11 is below SMA200",
        ),
        CandidateSpec(
            candidate_id="cash_switch_rotation_sma120",
            strategy="relative_strength_rotation",
            params={
                "market_filter_sma200": True,
                "market_filter_exit": True,
                "market_filter_ma_period": 120,
            },
            description="faster cash switch when KS11 is below SMA120",
        ),
        CandidateSpec(
            candidate_id="cash_switch_rotation_slow_defensive",
            strategy="relative_strength_rotation",
            params={
                "short_lookback": 80,
                "long_lookback": 160,
                "sma_period": 80,
                "short_weight": 0.5,
                "market_filter_sma200": True,
                "market_filter_exit": True,
                "market_filter_ma_period": 200,
            },
            description="slow rotation with defensive exposure and KS11 cash switch",
            diversification=defensive_budget,
        ),
    ]


def build_candidate_specs(candidate_family: str = DEFAULT_CANDIDATE_FAMILY) -> list[CandidateSpec]:
    family = candidate_family.lower().strip()
    if family in ("rotation", "relative_strength_rotation"):
        return build_rotation_candidate_specs()
    if family in ("momentum", "momentum_factor"):
        return build_momentum_candidate_specs()
    if family in ("breakout", "breakout_volume"):
        return build_breakout_candidate_specs()
    if family in ("pullback", "trend_pullback"):
        return build_pullback_candidate_specs()
    if family in ("benchmark_relative", "relative_momentum", "bench_rel_momentum"):
        return build_benchmark_relative_candidate_specs()
    if family in ("risk_budget", "exposure", "diversification"):
        return build_risk_budget_candidate_specs()
    if family in ("cash_switch", "market_exit", "market_filter_exit"):
        return build_cash_switch_candidate_specs()
    if family == "all":
        return [
            *build_rotation_candidate_specs(),
            *build_momentum_candidate_specs(),
            *build_breakout_candidate_specs(),
            *build_pullback_candidate_specs(),
            *build_benchmark_relative_candidate_specs(),
            *build_risk_budget_candidate_specs(),
            *build_cash_switch_candidate_specs(),
        ]
    raise ValueError(
        "candidate_family must be one of: rotation, momentum, breakout, pullback, "
        "benchmark_relative, risk_budget, cash_switch, all"
    )


def make_windows(start: str, end: str, window_months: int = 12, step_months: int = 6) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    cursor = pd.Timestamp(start)
    max_end = pd.Timestamp(end)
    while True:
        window_end = cursor + pd.DateOffset(months=window_months) - pd.Timedelta(days=1)
        if window_end > max_end:
            window_end = max_end
        if cursor >= max_end or (window_end - cursor).days < 60:
            break
        windows.append((cursor.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
        cursor += pd.DateOffset(months=step_months)
    return windows


@contextmanager
def temporary_diversification(config, overrides: dict[str, Any] | None):
    if not overrides:
        yield
        return

    div = config.risk_params.setdefault("diversification", {})
    saved = {key: div.get(key) for key in overrides}
    div.update(overrides)
    try:
        yield
    finally:
        for key in overrides:
            if saved[key] is None:
                div.pop(key, None)
            else:
                div[key] = saved[key]


def select_canonical_universe(top_n: int = DEFAULT_TOP_N) -> list[str]:
    """Use the same liquidity proxy as canonical promotion evaluation."""
    from core.data_collector import DataCollector
    import FinanceDataReader as fdr

    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    stocks = fdr.StockListing("KOSPI")
    common = stocks[~stocks["Code"].str.match(r"^\d{5}[5-9KL]$")]
    if "Marcap" in common.columns:
        common = common[common["Marcap"] > 1e11]

    amounts: dict[str, float] = {}
    for sym in common["Code"].tolist()[:100]:
        try:
            df = dc.fetch_korean_stock(sym, "2022-10-01", "2022-12-31")
            if df is not None and not df.empty:
                if "date" in df.columns:
                    df = df.set_index("date")
                amounts[sym] = float((df["close"].astype(float) * df["volume"].astype(float)).mean())
        except Exception:
            continue

    return normalize_symbols(sorted(amounts, key=amounts.get, reverse=True)[:top_n])


def buy_and_hold_benchmark_with_returns(
    symbols: list[str],
    start: str,
    end: str,
    capital: float,
) -> tuple[dict[str, Any], pd.Series]:
    from core.data_collector import DataCollector

    if not symbols:
        return (
            {"ew_bh_return": 0, "ew_bh_sharpe": 0, "universe_size": 0, "benchmark_symbols": []},
            pd.Series(dtype=float),
        )

    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    per_symbol_capital = capital / len(symbols)
    parts = []
    benchmark_symbols = []
    for sym in symbols:
        try:
            df = dc.fetch_korean_stock(normalize_symbol(sym), start, end)
        except Exception as e:
            logger.warning("benchmark fetch failed for {}: {}", sym, e)
            continue
        if df is None or df.empty:
            continue
        if "date" in df.columns:
            df = df.set_index("date")
        df = df[df.index >= pd.Timestamp(start)]
        if len(df) < 2:
            continue
        parts.append(per_symbol_capital / float(df["close"].iloc[0]) * df["close"].astype(float))
        benchmark_symbols.append(normalize_symbol(sym))

    combined = pd.concat(parts, axis=1).sum(axis=1).dropna() if parts else pd.Series(dtype=float)
    if len(combined) <= 1:
        return (
            {"ew_bh_return": 0, "ew_bh_sharpe": 0, "universe_size": 0, "benchmark_symbols": []},
            pd.Series(dtype=float),
        )

    total_return = (float(combined.iloc[-1]) / capital - 1) * 100
    daily_returns = combined.pct_change().dropna()
    std = float(daily_returns.std()) if len(daily_returns) > 1 else 0
    sharpe = (float(daily_returns.mean()) * 252 - 0.03) / (std * np.sqrt(252)) if std > 0 else 0
    return (
        {
            "ew_bh_return": round(total_return, 2),
            "ew_bh_sharpe": round(sharpe, 2),
            "universe_size": len(benchmark_symbols),
            "benchmark_symbols": benchmark_symbols,
        },
        daily_returns,
    )


def buy_and_hold_benchmark(symbols: list[str], start: str, end: str, capital: float) -> dict[str, Any]:
    benchmark, _daily_returns = buy_and_hold_benchmark_with_returns(symbols, start, end, capital)
    return benchmark


def candidate_exposure_series(equity_curve: pd.DataFrame | None) -> tuple[pd.Series, str]:
    if equity_curve is None or equity_curve.empty:
        return pd.Series(dtype=float), "none"

    eq = equity_curve.copy()
    if "date" in eq.columns:
        eq = eq.set_index("date")
    eq.index = pd.to_datetime(eq.index)

    if {"cash", "value"}.issubset(eq.columns):
        value = eq["value"].astype(float).replace(0, np.nan)
        cash = eq["cash"].astype(float)
        exposure = ((value - cash) / value).replace([np.inf, -np.inf], np.nan)
        return exposure.clip(lower=0, upper=1).fillna(0), "cash_value"

    if "n_positions" in eq.columns:
        return (eq["n_positions"].astype(float) > 0).astype(float), "position_presence"

    return pd.Series(0.0, index=eq.index), "none"


def exposure_summary(equity_curve: pd.DataFrame | None) -> dict[str, Any]:
    exposure, source = candidate_exposure_series(equity_curve)
    if exposure.empty:
        return {
            "avg_exposure_pct": 0,
            "median_exposure_pct": 0,
            "avg_cash_pct": 100,
            "invested_days_pct": 0,
            "exposure_observation_days": 0,
            "exposure_source": source,
        }

    return {
        "avg_exposure_pct": round(float(exposure.mean()) * 100, 1),
        "median_exposure_pct": round(float(exposure.median()) * 100, 1),
        "avg_cash_pct": round((1 - float(exposure.mean())) * 100, 1),
        "invested_days_pct": round(float((exposure > 0.01).sum()) / max(len(exposure), 1) * 100, 1),
        "exposure_observation_days": int(len(exposure)),
        "exposure_source": source,
    }


def exposure_matched_benchmark_metrics(
    equity_curve: pd.DataFrame | None,
    benchmark_daily_returns: pd.Series | None,
    capital: float,
) -> dict[str, Any]:
    if benchmark_daily_returns is None or benchmark_daily_returns.empty:
        return {
            "exposure_matched_bh_return": 0,
            "exposure_matched_bh_sharpe": 0,
            "exposure_matched_bh_mdd": 0,
        }

    exposure, _source = candidate_exposure_series(equity_curve)
    if exposure.empty:
        return {
            "exposure_matched_bh_return": 0,
            "exposure_matched_bh_sharpe": 0,
            "exposure_matched_bh_mdd": 0,
        }

    benchmark_returns = benchmark_daily_returns.copy().astype(float)
    benchmark_returns.index = pd.to_datetime(benchmark_returns.index)
    exposure = exposure.sort_index().reindex(benchmark_returns.index, method="ffill")
    exposure = exposure.shift(1).fillna(0).clip(lower=0, upper=1)
    matched_returns = (benchmark_returns * exposure).dropna()
    if matched_returns.empty:
        return {
            "exposure_matched_bh_return": 0,
            "exposure_matched_bh_sharpe": 0,
            "exposure_matched_bh_mdd": 0,
        }

    curve = capital * (1 + matched_returns).cumprod()
    total_return = (float(curve.iloc[-1]) / capital - 1) * 100
    std = float(matched_returns.std()) if len(matched_returns) > 1 else 0
    sharpe = (float(matched_returns.mean()) * 252 - 0.03) / (std * np.sqrt(252)) if std > 0 else 0
    peak = curve.cummax()
    mdd = float(((curve - peak) / peak).min() * 100)
    return {
        "exposure_matched_bh_return": round(total_return, 2),
        "exposure_matched_bh_sharpe": round(sharpe, 2),
        "exposure_matched_bh_mdd": round(mdd, 2),
    }


def calculate_research_metrics(
    result: dict,
    capital: float,
    benchmark_daily_returns: pd.Series | None = None,
) -> dict[str, Any]:
    eq = result.get("equity_curve")
    trades = result.get("trades", [])
    if eq is None or eq.empty:
        return {
            "total_return": 0,
            "sharpe": 0,
            "profit_factor": 0,
            "mdd": 0,
            "win_rate": 0,
            "total_trades": 0,
            "signal_density": 0,
            "ev_per_trade": 0,
            "cost_adjusted_cagr": -100,
            "turnover_per_year": 0,
            **exposure_summary(eq),
            **exposure_matched_benchmark_metrics(eq, benchmark_daily_returns, capital),
        }

    eq = eq.copy()
    if "date" in eq.columns:
        eq = eq.set_index("date")

    final = float(eq["value"].iloc[-1])
    total_return = (final / capital - 1) * 100
    years = max(len(eq) / 252, 1 / 252)
    daily_returns = eq["value"].pct_change().dropna()
    std = float(daily_returns.std()) if len(daily_returns) > 1 else 0
    sharpe = (float(daily_returns.mean()) * 252 - 0.03) / (std * np.sqrt(252)) if std > 0 else 0
    peak = eq["value"].cummax()
    mdd = float(((eq["value"] - peak) / peak).min() * 100)

    sells = [t for t in trades if t.get("action") != "BUY"]
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
    gross_profit = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99 if gross_profit > 0 else 0)
    n_positions = eq.get("n_positions", pd.Series(0, index=eq.index))
    realized_pnl = sum(t.get("pnl", 0) for t in sells)
    notional = sum(
        abs(float(t.get("price", 0) or 0) * float(t.get("quantity", 0) or 0))
        for t in trades
    )

    return {
        "total_return": round(total_return, 2),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(profit_factor, 2),
        "mdd": round(mdd, 2),
        "win_rate": round(wins / len(sells) * 100, 1) if sells else 0,
        "total_trades": len(sells),
        "signal_density": round(float((n_positions > 0).sum()) / max(len(eq), 1) * 100, 1),
        "ev_per_trade": round(realized_pnl / len(sells), 0) if sells else 0,
        "cost_adjusted_cagr": round(((final / capital) ** (1 / years) - 1) * 100, 2)
        if final > 0 and capital > 0
        else -100,
        "turnover_per_year": round(notional / capital / years * 100, 1) if capital > 0 else 0,
        **exposure_summary(eq),
        **exposure_matched_benchmark_metrics(eq, benchmark_daily_returns, capital),
    }


def rank_score(metrics: dict[str, Any]) -> float:
    """Research ranking score. Promotion status remains the hard gate."""
    excess_return = float(metrics.get("benchmark_excess_return", 0) or 0)
    excess_sharpe = float(metrics.get("benchmark_excess_sharpe", 0) or 0)
    sharpe = float(metrics.get("sharpe", 0) or 0)
    profit_factor = min(float(metrics.get("profit_factor", 0) or 0), 5.0)
    mdd = float(metrics.get("mdd", 0) or 0)
    trades = int(metrics.get("total_trades", 0) or 0)
    turnover = float(metrics.get("turnover_per_year", 0) or 0)

    score = excess_return + excess_sharpe * 10 + sharpe * 5 + profit_factor * 2 + mdd * 0.2
    if trades < 30:
        score -= 25
    if turnover >= 1000:
        score -= 20
    return round(score, 2)


def promotion_status(candidate_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
    from core.promotion_engine import promote

    result = promote(candidate_to_strategy_metrics(candidate_id, metrics))
    return {
        "status": result.status,
        "allowed_modes": result.allowed_modes,
        "reason": result.reason,
    }


def candidate_to_strategy_metrics(candidate_id: str, metrics: dict[str, Any]):
    """Map a research candidate metrics dict to the promotion engine input type."""
    from core.promotion_engine import StrategyMetrics

    return StrategyMetrics(
        name=candidate_id,
        total_return=metrics.get("total_return", 0),
        profit_factor=metrics.get("profit_factor", 0),
        mdd=metrics.get("mdd", 0),
        wf_positive_rate=metrics.get("wf_positive_rate", 0),
        wf_sharpe_positive_rate=metrics.get("wf_sharpe_positive_rate", 0),
        wf_windows=metrics.get("wf_windows", 0),
        wf_total_trades=metrics.get("wf_total_trades", 0),
        sharpe=metrics.get("sharpe", 0),
        benchmark_excess_return=metrics.get("benchmark_excess_return"),
        benchmark_excess_sharpe=metrics.get("benchmark_excess_sharpe"),
        ev_per_trade=metrics.get("ev_per_trade"),
        cost_adjusted_cagr=metrics.get("cost_adjusted_cagr"),
        turnover_per_year=metrics.get("turnover_per_year"),
    )


def diversification_for_spec(spec: CandidateSpec) -> dict[str, Any]:
    return dict(spec.diversification or DEFAULT_RESEARCH_DIVERSIFICATION)


def build_candidate_record(
    spec: CandidateSpec,
    metrics: dict[str, Any],
    benchmark: dict[str, Any],
) -> dict[str, Any]:
    metrics = dict(metrics)
    metrics["benchmark_excess_return"] = round(
        float(metrics.get("total_return", 0)) - float(benchmark.get("ew_bh_return", 0)),
        2,
    )
    metrics["benchmark_excess_sharpe"] = round(
        float(metrics.get("sharpe", 0)) - float(benchmark.get("ew_bh_sharpe", 0)),
        2,
    )
    metrics["exposure_matched_excess_return"] = round(
        float(metrics.get("total_return", 0)) - float(metrics.get("exposure_matched_bh_return", 0)),
        2,
    )
    metrics["exposure_matched_excess_sharpe"] = round(
        float(metrics.get("sharpe", 0)) - float(metrics.get("exposure_matched_bh_sharpe", 0)),
        2,
    )
    promotion = promotion_status(spec.candidate_id, metrics)
    return {
        "candidate_id": spec.candidate_id,
        "strategy": spec.strategy,
        "params": spec.params,
        "description": spec.description,
        "diversification": diversification_for_spec(spec),
        "alpha_pass": (
            metrics.get("benchmark_excess_return", 0) > 0
            and metrics.get("benchmark_excess_sharpe", 0) > 0
        ),
        "rank_score": rank_score(metrics),
        "promotion": promotion,
        "rejection_reasons": candidate_rejection_reasons(metrics, promotion),
        "metrics": metrics,
    }


def candidate_rejection_reasons(metrics: dict[str, Any], promotion: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if metrics.get("benchmark_excess_return", 0) <= 0:
        reasons.append("benchmark_excess_return <= 0")
    if metrics.get("benchmark_excess_sharpe", 0) <= 0:
        reasons.append("benchmark_excess_sharpe <= 0")
    if promotion.get("status") != "provisional_paper_candidate":
        reasons.append(f"promotion_status={promotion.get('status')}")
    if metrics.get("total_trades", 0) < 30:
        reasons.append("total_trades < 30")
    if metrics.get("ev_per_trade") is not None and metrics.get("ev_per_trade", 0) <= 0:
        reasons.append("ev_per_trade <= 0")
    if metrics.get("turnover_per_year") is not None and metrics.get("turnover_per_year", 0) >= 1000:
        reasons.append("turnover_per_year >= 1000")
    return reasons


def sort_candidate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda r: (
            bool(r.get("alpha_pass", False)),
            r.get("promotion", {}).get("status") == "live_candidate",
            r.get("promotion", {}).get("status") == "provisional_paper_candidate",
            r.get("rank_score", float("-inf")),
            r.get("metrics", {}).get("benchmark_excess_return", float("-inf")),
        ),
        reverse=True,
    )


def build_decision_summary(
    candidates: list[dict[str, Any]],
    *,
    walk_forward_enabled: bool,
    benchmark: dict[str, Any],
) -> dict[str, Any]:
    eligible = [
        r for r in candidates
        if r.get("alpha_pass") and r.get("promotion", {}).get("status") == "provisional_paper_candidate"
    ]
    alpha_candidates = [r for r in candidates if r.get("alpha_pass")]
    best_id = candidates[0]["candidate_id"] if candidates else None

    if int(benchmark.get("universe_size", 0) or 0) <= 0:
        return {
            "action": "INSUFFICIENT_BENCHMARK_DATA",
            "reason": "Benchmark data was unavailable, so excess-return gates cannot be trusted.",
            "best_candidate_id": best_id,
            "eligible_candidate_ids": [],
            "alpha_candidate_ids": [],
            "next_actions": [
                "Fix benchmark/universe data coverage before interpreting this sweep.",
                "Do not run canonical promotion from this artifact.",
            ],
        }

    if eligible:
        ids = [r["candidate_id"] for r in eligible]
        return {
            "action": "RUN_CANONICAL_EVALUATION",
            "reason": "At least one candidate has positive benchmark excess and provisional paper status.",
            "best_candidate_id": best_id,
            "eligible_candidate_ids": ids,
            "alpha_candidate_ids": [r["candidate_id"] for r in alpha_candidates],
            "next_actions": [
                f"Run canonical promotion evaluation for: {', '.join(ids)}.",
                "Keep paper/live gates unchanged; this artifact is research-only.",
            ],
        }

    if alpha_candidates and not walk_forward_enabled:
        ids = [r["candidate_id"] for r in alpha_candidates]
        return {
            "action": "RUN_FULL_WALK_FORWARD",
            "reason": "Quick sweep found benchmark-positive candidates, but walk-forward was skipped.",
            "best_candidate_id": best_id,
            "eligible_candidate_ids": [],
            "alpha_candidate_ids": ids,
            "next_actions": [
                f"Re-run without --quick for: {', '.join(ids)}.",
                "Promote nothing until walk-forward stability and canonical gates pass.",
            ],
        }

    if alpha_candidates:
        ids = [r["candidate_id"] for r in alpha_candidates]
        return {
            "action": "KEEP_RESEARCH_ONLY",
            "reason": "Benchmark-positive candidates failed promotion quality gates.",
            "best_candidate_id": best_id,
            "eligible_candidate_ids": [],
            "alpha_candidate_ids": ids,
            "next_actions": [
                "Keep these candidates research-only.",
                "Inspect rejection_reasons before expanding the search space.",
            ],
        }

    return {
        "action": "NO_ALPHA_CANDIDATE",
        "reason": "No candidate produced both positive benchmark excess return and positive excess Sharpe.",
        "best_candidate_id": best_id,
        "eligible_candidate_ids": [],
        "alpha_candidate_ids": [],
        "next_actions": [
            "Do not run canonical promotion from this sweep.",
            "Design a new candidate family or expand the universe before another promotion attempt.",
        ],
    }


def evaluate_candidate(
    spec: CandidateSpec,
    symbols: list[str],
    start: str,
    end: str,
    capital: float,
    benchmark_daily_returns: pd.Series | None = None,
) -> dict[str, Any]:
    from backtest.portfolio_backtester import PortfolioBacktester
    from config.config_loader import Config

    config = Config.get()
    fetch_start = (pd.Timestamp(start) - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
    with temporary_diversification(config, diversification_for_spec(spec)):
        pbt = PortfolioBacktester(config)
        result = pbt.run(
            symbols=symbols,
            strategy_name=spec.strategy,
            initial_capital=capital,
            start_date=fetch_start,
            end_date=end,
            trade_start_date=start,
            param_overrides={spec.strategy: spec.params},
        )

    if result.get("equity_curve") is not None and not result["equity_curve"].empty:
        eq = result["equity_curve"]
        result["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(start)].copy()
    result["trades"] = [
        t
        for t in result.get("trades", [])
        if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(start)
    ]
    return calculate_research_metrics(result, capital, benchmark_daily_returns)


def attach_walk_forward_metrics(
    spec: CandidateSpec,
    metrics: dict[str, Any],
    symbols: list[str],
    windows: list[tuple[str, str]],
    capital: float,
    benchmark_daily_returns: pd.Series | None = None,
) -> dict[str, Any]:
    wf_metrics = []
    for start, end in windows:
        try:
            wf_benchmark_returns = benchmark_daily_returns
            if wf_benchmark_returns is not None and not wf_benchmark_returns.empty:
                idx = pd.to_datetime(wf_benchmark_returns.index)
                wf_benchmark_returns = wf_benchmark_returns[
                    (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))
                ]
            wf_metrics.append(evaluate_candidate(spec, symbols, start, end, capital, wf_benchmark_returns))
        except Exception as e:
            logger.warning("{} WF {}~{} failed: {}", spec.candidate_id, start, end, e)
            wf_metrics.append({"total_return": 0, "sharpe": 0, "total_trades": 0})

    n_windows = len(wf_metrics)
    metrics = dict(metrics)
    metrics["wf_windows"] = n_windows
    metrics["wf_positive_rate"] = round(
        sum(1 for m in wf_metrics if m.get("total_return", 0) > 0) / max(n_windows, 1),
        3,
    )
    metrics["wf_sharpe_positive_rate"] = round(
        sum(1 for m in wf_metrics if m.get("sharpe", 0) > 0) / max(n_windows, 1),
        3,
    )
    metrics["wf_total_trades"] = sum(int(m.get("total_trades", 0) or 0) for m in wf_metrics)
    metrics["wf_details"] = [
        {"return": m.get("total_return", 0), "sharpe": m.get("sharpe", 0)}
        for m in wf_metrics
    ]
    return metrics


def run_candidate_sweep(
    *,
    symbols: list[str] | None = None,
    top_n: int = DEFAULT_TOP_N,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    capital: float = DEFAULT_INITIAL_CAPITAL,
    include_walk_forward: bool = True,
    candidate_family: str = DEFAULT_CANDIDATE_FAMILY,
) -> dict[str, Any]:
    from config.config_loader import Config

    config = Config.get()
    symbols = normalize_symbols(symbols or select_canonical_universe(top_n))
    benchmark, benchmark_daily_returns = buy_and_hold_benchmark_with_returns(symbols, start, end, capital)
    windows = make_windows(start, end) if include_walk_forward else []
    records = []

    specs = build_candidate_specs(candidate_family)
    for spec in specs:
        logger.info("Evaluating {}", spec.candidate_id)
        metrics = evaluate_candidate(spec, symbols, start, end, capital, benchmark_daily_returns)
        if include_walk_forward:
            metrics = attach_walk_forward_metrics(
                spec,
                metrics,
                symbols,
                windows,
                capital,
                benchmark_daily_returns,
            )
        else:
            metrics.update(
                {
                    "wf_windows": 0,
                    "wf_positive_rate": 0,
                    "wf_sharpe_positive_rate": 0,
                    "wf_total_trades": 0,
                    "wf_details": [],
                }
            )
        records.append(build_candidate_record(spec, metrics, benchmark))

    ranked = sort_candidate_records(records)
    family_slug = candidate_family.lower().strip()
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{family_slug}"
    eligible = [
        r for r in ranked
        if r.get("alpha_pass") and r.get("promotion", {}).get("status") == "provisional_paper_candidate"
    ]
    decision = build_decision_summary(
        ranked,
        walk_forward_enabled=include_walk_forward,
        benchmark=benchmark,
    )
    return {
        "schema_version": 1,
        "artifact_type": "research_candidate_sweep_bundle",
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(),
        "commit_hash": get_git_hash(),
        "config_yaml_hash": config.yaml_hash,
        "config_resolved_hash": config.resolved_hash,
        "eval_start": start,
        "eval_end": end,
        "initial_capital": capital,
        "candidate_family": candidate_family,
        "universe": symbols,
        "benchmark": benchmark,
        "walk_forward": {
            "enabled": include_walk_forward,
            "windows": [{"start": s, "end": e} for s, e in windows],
        },
        "ranking_rule": (
            "rank_score + promotion status; live/paper promotion remains controlled "
            "by canonical promotion and evidence gates"
        ),
        "decision": decision,
        "candidates": ranked,
        "summary": {
            "evaluated": len(ranked),
            "eligible_for_canonical_eval": len(eligible),
            "best_candidate_id": ranked[0]["candidate_id"] if ranked else None,
            "decision_action": decision["action"],
        },
    }


def validate_sweep_artifact(payload: dict[str, Any]) -> tuple[bool, str]:
    """Lightweight schema validation for research candidate artifacts."""
    required = {
        "schema_version",
        "artifact_type",
        "run_id",
        "generated_at",
        "commit_hash",
        "universe",
        "benchmark",
        "candidates",
        "summary",
    }
    missing = sorted(required - set(payload))
    if missing:
        return False, f"missing fields: {', '.join(missing)}"
    if payload.get("schema_version") != 1:
        return False, "schema_version must be 1"
    if payload.get("artifact_type") != "research_candidate_sweep_bundle":
        return False, "artifact_type must be research_candidate_sweep_bundle"
    if not isinstance(payload.get("candidates"), list):
        return False, "candidates must be a list"
    return True, "ok"


def write_candidate_artifacts(bundle: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    ok, reason = validate_sweep_artifact(bundle)
    if not ok:
        raise ValueError(f"invalid research sweep artifact: {reason}")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(bundle.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
    json_path = output_dir / f"candidate_sweep_{stamp}.json"
    md_path = output_dir / f"candidate_sweep_{stamp}.md"

    json_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    lines = [
        "# Research Candidate Sweep",
        f"Generated: {bundle.get('generated_at', '')[:19]}",
        f"Period: {bundle.get('eval_start')} ~ {bundle.get('eval_end')}",
        f"Candidate family: {bundle.get('candidate_family', 'rotation')}",
        f"Universe size: {len(bundle.get('universe', []))}",
        "",
        "## Benchmark",
        f"- EW B&H return: {bundle.get('benchmark', {}).get('ew_bh_return', 0):.2f}%",
        f"- EW B&H Sharpe: {bundle.get('benchmark', {}).get('ew_bh_sharpe', 0):.2f}",
        "",
        "## Decision",
        f"- Action: {bundle.get('decision', {}).get('action', 'UNKNOWN')}",
        f"- Reason: {bundle.get('decision', {}).get('reason', '')}",
        f"- Best candidate: {bundle.get('decision', {}).get('best_candidate_id')}",
        "",
        "## Next Actions",
    ]
    for action in bundle.get("decision", {}).get("next_actions", []):
        lines.append(f"- {action}")
    lines.extend([
        "",
        "## Ranking",
        "| Rank | Candidate | Status | Score | Return | Excess | EM Excess | Avg Exp | Sharpe | PF | MDD | Trades |",
        "|------|-----------|--------|-------|--------|--------|-----------|---------|--------|----|-----|--------|",
    ])
    for i, rec in enumerate(bundle.get("candidates", []), start=1):
        m = rec.get("metrics", {})
        lines.append(
            f"| {i} | {rec.get('candidate_id')} | {rec.get('promotion', {}).get('status')} | "
            f"{rec.get('rank_score', 0):.2f} | {m.get('total_return', 0):.2f}% | "
            f"{m.get('benchmark_excess_return', 0):.2f}%p | "
            f"{m.get('exposure_matched_excess_return', 0):.2f}%p | "
            f"{m.get('avg_exposure_pct', 0):.1f}% | {m.get('sharpe', 0):.2f} | "
            f"{m.get('profit_factor', 0):.2f} | {m.get('mdd', 0):.2f}% | {m.get('total_trades', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Guardrail",
            "This report is research-only. Live promotion still requires the canonical promotion bundle, eligible paper evidence, positive benchmark excess, and the live hard gate.",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def parse_symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return normalize_symbols([s.strip() for s in value.split(",") if s.strip()])


def main() -> None:
    parser = argparse.ArgumentParser(description="Research candidate sweep")
    parser.add_argument("--symbols", help="Comma-separated symbols. Omit to use canonical liquidity universe.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument(
        "--candidate-family",
        default=DEFAULT_CANDIDATE_FAMILY,
        choices=[
            "rotation",
            "momentum",
            "breakout",
            "pullback",
            "benchmark_relative",
            "risk_budget",
            "cash_switch",
            "all",
        ],
        help="Research candidate family to evaluate.",
    )
    parser.add_argument("--quick", action="store_true", help="Skip walk-forward windows.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    bundle = run_candidate_sweep(
        symbols=parse_symbols(args.symbols),
        top_n=args.top_n,
        start=args.start,
        end=args.end,
        capital=args.capital,
        include_walk_forward=not args.quick,
        candidate_family=args.candidate_family,
    )
    json_path, md_path = write_candidate_artifacts(bundle, Path(args.output_dir))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    decision = bundle.get("decision", {})
    print(f"Decision: {decision.get('action', 'UNKNOWN')} - {decision.get('reason', '')}")
    for i, rec in enumerate(bundle.get("candidates", [])[:5], start=1):
        m = rec.get("metrics", {})
        print(
            f"{i}. {rec['candidate_id']}: {rec['promotion']['status']} "
            f"ret={m.get('total_return', 0):.2f}% "
            f"excess={m.get('benchmark_excess_return', 0):.2f}%p "
            f"em_excess={m.get('exposure_matched_excess_return', 0):.2f}%p "
            f"avg_exp={m.get('avg_exposure_pct', 0):.1f}% "
            f"sharpe={m.get('sharpe', 0):.2f}"
        )


if __name__ == "__main__":
    main()

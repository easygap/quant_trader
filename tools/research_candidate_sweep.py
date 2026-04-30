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


def build_benchmark_aware_rotation_candidate_specs() -> list[CandidateSpec]:
    """Exposure-retaining rotation variants ranked by stock momentum above KS11."""
    balanced_budget = {
        "max_positions": 4,
        "max_position_ratio": 0.25,
        "max_investment_ratio": 0.85,
        "min_cash_ratio": 0.10,
    }
    common = {
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
        "rank_entry_mode": "dense_ranked",
        "use_positive_momentum_filter": False,
        "use_trend_filter": False,
        "exit_trend_edge": False,
        "exit_rebalance_mode": "score_floor",
        "disable_trailing_stop": True,
        "take_profit_rate": 0.10,
    }
    return [
        CandidateSpec(
            candidate_id="benchmark_aware_rotation_60_120_dense",
            strategy="relative_strength_rotation",
            params={
                **common,
                "short_lookback": 60,
                "long_lookback": 120,
                "sma_period": 60,
                "short_weight": 0.6,
                "sell_score_floor_pct": -8.0,
            },
            description="dense monthly rotation ranked by 60/120d momentum excess over KS11",
        ),
        CandidateSpec(
            candidate_id="benchmark_aware_rotation_80_160_dense",
            strategy="relative_strength_rotation",
            params={
                **common,
                "short_lookback": 80,
                "long_lookback": 160,
                "sma_period": 80,
                "short_weight": 0.5,
                "sell_score_floor_pct": -10.0,
            },
            description="slower benchmark-aware rotation with a wider excess score floor",
        ),
        CandidateSpec(
            candidate_id="benchmark_aware_rotation_40_100_dense",
            strategy="relative_strength_rotation",
            params={
                **common,
                "short_lookback": 40,
                "long_lookback": 100,
                "sma_period": 50,
                "short_weight": 0.7,
                "sell_score_floor_pct": -6.0,
            },
            description="faster benchmark-aware rotation with a tighter excess score floor",
        ),
        CandidateSpec(
            candidate_id="benchmark_aware_rotation_60_120_balanced",
            strategy="relative_strength_rotation",
            params={
                **common,
                "short_lookback": 60,
                "long_lookback": 120,
                "sma_period": 60,
                "short_weight": 0.6,
                "sell_score_floor_pct": -8.0,
            },
            description="benchmark-aware rotation with broader, lower single-name exposure",
            diversification=balanced_budget,
        ),
    ]


def build_target_weight_rotation_candidate_specs() -> list[CandidateSpec]:
    """Research-only monthly top-N target-weight rotation variants."""
    common = {
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
        "rebalance_frequency": "monthly",
        "target_exposure": 0.85,
        "target_tolerance_pct": 1.0,
    }
    return [
        CandidateSpec(
            candidate_id="target_weight_rotation_top2_60_120_excess",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 2,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
            },
            description="monthly top-2 target-weight rotation ranked by 60/120d KS11 excess momentum",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_60_120_excess",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
            },
            description="monthly top-3 target-weight rotation ranked by 60/120d KS11 excess momentum",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_40_100_excess",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 40,
                "long_lookback": 100,
                "short_weight": 0.7,
            },
            description="faster monthly top-3 target-weight rotation ranked by KS11 excess momentum",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_40_100_floor0",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 40,
                "long_lookback": 100,
                "short_weight": 0.7,
                "min_score_floor_pct": 0.0,
            },
            description="faster top-3 target rotation that leaves slots in cash below zero excess momentum",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_40_100_floor3",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 40,
                "long_lookback": 100,
                "short_weight": 0.7,
                "min_score_floor_pct": 3.0,
            },
            description="faster top-3 target rotation with a stricter 3pct excess score floor",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "min_score_floor_pct": 0.0,
            },
            description="broader top-5 target rotation that leaves weak excess slots in cash",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0_hold3",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "min_score_floor_pct": 0.0,
                "hold_rank_buffer": 3,
            },
            description="top-5 score-floor rotation that retains holdings still ranked within the top 8",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0_exp80",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "target_exposure": 0.80,
                "min_score_floor_pct": 0.0,
            },
            description="top-5 score-floor rotation with 80pct target exposure to reduce turnover",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0_exp80_tol3",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "target_exposure": 0.80,
                "min_score_floor_pct": 0.0,
                "target_tolerance_pct": 3.0,
            },
            description="top-5 score-floor rotation with 80pct exposure and wider rebalance tolerance",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0_exp75",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "target_exposure": 0.75,
                "min_score_floor_pct": 0.0,
            },
            description="top-5 score-floor rotation with 75pct target exposure to test turnover relief",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_40_100_hold2",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 40,
                "long_lookback": 100,
                "short_weight": 0.7,
                "hold_rank_buffer": 2,
            },
            description="faster top-3 target rotation that keeps current holdings within a 2-rank buffer",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top5_60_120_floor0_tol3",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 5,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "min_score_floor_pct": 0.0,
                "target_tolerance_pct": 3.0,
            },
            description="top-5 score-floor rotation with a wider 3pct rebalance tolerance",
        ),
        CandidateSpec(
            candidate_id="target_weight_rotation_top3_60_120_partial_cash",
            strategy="target_weight_rotation",
            params={
                **common,
                "target_top_n": 3,
                "short_lookback": 60,
                "long_lookback": 120,
                "short_weight": 0.6,
                "market_exposure_mode": "benchmark_sma",
                "market_ma_period": 120,
                "bear_target_exposure": 0.55,
            },
            description="top-3 target rotation with a KS11 SMA exposure overlay",
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
    if family in (
        "benchmark_aware_rotation",
        "bench_aware_rotation",
        "relative_rank",
        "exposure_retaining",
    ):
        return build_benchmark_aware_rotation_candidate_specs()
    if family in (
        "target_weight_rotation",
        "target_topn",
        "topn_rotation",
        "monthly_topn",
    ):
        return build_target_weight_rotation_candidate_specs()
    if family == "all":
        return [
            *build_rotation_candidate_specs(),
            *build_momentum_candidate_specs(),
            *build_breakout_candidate_specs(),
            *build_pullback_candidate_specs(),
            *build_benchmark_relative_candidate_specs(),
            *build_risk_budget_candidate_specs(),
            *build_cash_switch_candidate_specs(),
            *build_benchmark_aware_rotation_candidate_specs(),
            *build_target_weight_rotation_candidate_specs(),
        ]
    raise ValueError(
        "candidate_family must be one of: rotation, momentum, breakout, pullback, "
        "benchmark_relative, risk_budget, cash_switch, benchmark_aware_rotation, "
        "target_weight_rotation, all"
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


def close_series_from_ohlcv(df: pd.DataFrame | None) -> pd.Series:
    """Normalize a fetched OHLCV frame into a close series."""
    if df is None or df.empty or "close" not in df.columns:
        return pd.Series(dtype=float)

    data = df.copy()
    if "date" in data.columns:
        data = data.set_index("date")
    idx = pd.to_datetime(data.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    series = data["close"].astype(float).copy()
    series.index = idx.normalize()
    return series[series > 0].groupby(level=0).last().sort_index()


def monthly_rebalance_days(index: pd.Index) -> list[pd.Timestamp]:
    """Return the first available trading day for each month in the index."""
    if len(index) == 0:
        return []
    idx = pd.DatetimeIndex(index).sort_values()
    months = idx.to_series().dt.to_period("M")
    mask = (months != months.shift(1)).fillna(True)
    return [pd.Timestamp(day).normalize() for day in idx[mask.to_numpy()]]


def _score_date_before(index: pd.Index, day: pd.Timestamp) -> pd.Timestamp | None:
    prior = pd.DatetimeIndex(index)[pd.DatetimeIndex(index) < pd.Timestamp(day)]
    if len(prior) == 0:
        return None
    return pd.Timestamp(prior[-1]).normalize()


def _target_weight_score_panel(
    close_panel: pd.DataFrame,
    benchmark_close: pd.Series,
    params: dict[str, Any],
) -> pd.DataFrame:
    short_lb = int(params.get("short_lookback", 60))
    long_lb = int(params.get("long_lookback", 120))
    short_w = float(params.get("short_weight", 0.6))
    score_mode = str(params.get("score_mode", "absolute")).lower().strip()

    composite = (
        short_w * close_panel.pct_change(short_lb)
        + (1.0 - short_w) * close_panel.pct_change(long_lb)
    )
    if score_mode != "benchmark_excess":
        return composite

    if benchmark_close.empty:
        return pd.DataFrame(np.nan, index=close_panel.index, columns=close_panel.columns)

    benchmark_composite = (
        short_w * benchmark_close.pct_change(short_lb)
        + (1.0 - short_w) * benchmark_close.pct_change(long_lb)
    )
    aligned = benchmark_composite.reindex(close_panel.index, method="ffill")
    return composite.sub(aligned, axis=0)


def _target_exposure_for_day(
    day: pd.Timestamp,
    benchmark_close: pd.Series,
    params: dict[str, Any],
) -> float:
    base = max(0.0, min(float(params.get("target_exposure", 0.85)), 1.0))
    mode = str(params.get("market_exposure_mode", "fixed")).lower().strip()
    if mode != "benchmark_sma" or benchmark_close.empty:
        return base

    score_day = _score_date_before(benchmark_close.index, day)
    if score_day is None:
        return base
    ma_period = int(params.get("market_ma_period", 120))
    sma = benchmark_close.rolling(ma_period, min_periods=ma_period).mean()
    if pd.isna(sma.get(score_day, np.nan)):
        return base
    if float(benchmark_close.loc[score_day]) < float(sma.loc[score_day]):
        return max(0.0, min(float(params.get("bear_target_exposure", base)), 1.0))
    return base


def _select_target_weight_targets(
    score_row: pd.Series,
    prices: dict[str, float],
    positions: dict[str, dict[str, float]],
    top_n: int,
    hold_rank_buffer: int,
) -> list[str]:
    ranked = [
        sym
        for sym in score_row.index.tolist()
        if sym in prices and prices.get(sym, 0.0) > 0
    ]
    targets = ranked[:top_n]
    if hold_rank_buffer <= 0 or not positions:
        return targets

    retention_pool = ranked[top_n : top_n + hold_rank_buffer]
    for held in [sym for sym in retention_pool if sym in positions]:
        if held in targets:
            continue
        replacement_idx = next(
            (idx for idx in range(len(targets) - 1, -1, -1) if targets[idx] not in positions),
            None,
        )
        if replacement_idx is None:
            break
        targets[replacement_idx] = held
    return targets


def _execute_target_weight_rebalance(
    *,
    day: pd.Timestamp,
    cash: float,
    positions: dict[str, dict[str, float]],
    prices: dict[str, float],
    targets: list[str],
    target_exposure: float,
    rebalance_tolerance: float,
    risk_manager,
) -> tuple[float, dict[str, dict[str, float]], list[dict[str, Any]], float]:
    nav = cash + sum(
        pos["qty"] * prices.get(sym, 0.0)
        for sym, pos in positions.items()
        if prices.get(sym, 0.0) > 0
    )
    if nav <= 0:
        return cash, positions, [], 0.0

    target_set = {sym for sym in targets if prices.get(sym, 0.0) > 0}
    per_target_value = nav * target_exposure / len(target_set) if target_set else 0.0
    desired_qty: dict[str, float] = {}
    for sym in set(positions) | target_set:
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        desired_qty[sym] = per_target_value / price if sym in target_set else 0.0

    trades: list[dict[str, Any]] = []
    turnover = 0.0

    # Sells first so buys can use freed cash.
    for sym, desired in sorted(desired_qty.items()):
        current = float(positions.get(sym, {}).get("qty", 0.0))
        qty_to_sell = current - desired
        if qty_to_sell <= 1e-9:
            continue
        price = prices[sym]
        if abs(qty_to_sell * price) / nav < rebalance_tolerance:
            continue
        avg_price = float(positions[sym].get("avg_price", price))
        costs = risk_manager.calculate_transaction_costs(
            price,
            qty_to_sell,
            "SELL",
            avg_price=avg_price,
        )
        execution_price = float(costs["execution_price"])
        tax = float(costs.get("tax", 0) or 0) + float(costs.get("capital_gains_tax", 0) or 0)
        commission = float(costs.get("commission", 0) or 0)
        pnl = (execution_price - avg_price) * qty_to_sell - commission - tax
        proceeds = execution_price * qty_to_sell - commission - tax
        cash += proceeds
        turnover += abs(execution_price * qty_to_sell)
        remaining = current - qty_to_sell
        if remaining <= 1e-9:
            positions.pop(sym, None)
        else:
            positions[sym]["qty"] = remaining
        trades.append(
            {
                "date": day,
                "symbol": sym,
                "action": "REBALANCE_SELL",
                "price": execution_price,
                "quantity": qty_to_sell,
                "pnl": pnl,
                "pnl_rate": ((execution_price / avg_price) - 1) * 100 if avg_price > 0 else 0,
            }
        )

    buy_plans = []
    total_outlay = 0.0
    for sym, desired in sorted(desired_qty.items()):
        current = float(positions.get(sym, {}).get("qty", 0.0))
        qty_to_buy = desired - current
        if qty_to_buy <= 1e-9 or prices.get(sym, 0.0) <= 0:
            continue
        if abs(qty_to_buy * prices[sym]) / nav < rebalance_tolerance:
            continue
        costs = risk_manager.calculate_transaction_costs(prices[sym], qty_to_buy, "BUY")
        outlay = float(costs["execution_price"]) * qty_to_buy + float(costs.get("commission", 0) or 0)
        buy_plans.append((sym, qty_to_buy, costs, outlay))
        total_outlay += outlay

    scale = 1.0
    if total_outlay > cash and total_outlay > 0:
        scale = max(cash / total_outlay * 0.998, 0.0)

    for sym, qty, _costs, _outlay in buy_plans:
        qty *= scale
        if qty <= 1e-9:
            continue
        costs = risk_manager.calculate_transaction_costs(prices[sym], qty, "BUY")
        execution_price = float(costs["execution_price"])
        commission = float(costs.get("commission", 0) or 0)
        outlay = execution_price * qty + commission
        if outlay > cash + 1e-6:
            continue
        old_qty = float(positions.get(sym, {}).get("qty", 0.0))
        old_avg = float(positions.get(sym, {}).get("avg_price", execution_price))
        new_qty = old_qty + qty
        new_avg = ((old_qty * old_avg) + (qty * execution_price)) / new_qty
        positions[sym] = {"qty": new_qty, "avg_price": new_avg}
        cash -= outlay
        turnover += abs(execution_price * qty)
        trades.append(
            {
                "date": day,
                "symbol": sym,
                "action": "BUY",
                "price": execution_price,
                "quantity": qty,
                "pnl": 0,
                "pnl_rate": 0,
            }
        )

    return cash, positions, trades, turnover


def run_target_weight_rotation_backtest(
    symbols: list[str],
    start: str,
    end: str,
    capital: float,
    params: dict[str, Any],
    *,
    collector=None,
    risk_manager=None,
) -> dict[str, Any]:
    """Research-only monthly top-N target-weight rotation backtest."""
    from core.data_collector import DataCollector
    from core.risk_manager import RiskManager

    symbols = normalize_symbols(symbols)
    short_lb = int(params.get("short_lookback", 60))
    long_lb = int(params.get("long_lookback", 120))
    warmup_days = max(long_lb * 3, 180)
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    collector = collector or DataCollector()
    risk_manager = risk_manager or RiskManager()

    previous_quiet = getattr(collector, "quiet_ohlcv_log", None)
    if previous_quiet is not None:
        collector.quiet_ohlcv_log = True
    try:
        close_parts = []
        valid_symbols = []
        for sym in symbols:
            df = collector.fetch_korean_stock(sym, fetch_start, end)
            close = close_series_from_ohlcv(df)
            if close.empty:
                continue
            close.name = sym
            close_parts.append(close)
            valid_symbols.append(sym)

        if not close_parts:
            return {
                "equity_curve": pd.DataFrame(),
                "trades": [],
                "target_weight_metrics": {
                    "target_top_n": int(params.get("target_top_n", 0) or 0),
                    "rebalance_count": 0,
                    "avg_slots_filled": 0,
                    "slot_fill_rate_pct": 0,
                },
            }

        close_panel = pd.concat(close_parts, axis=1).sort_index().ffill()
        close_panel = close_panel[~close_panel.index.duplicated(keep="last")]
        benchmark_symbol = str(params.get("benchmark_symbol", "KS11"))
        benchmark_close = close_series_from_ohlcv(
            collector.fetch_korean_stock(benchmark_symbol, fetch_start, end)
        )
        score_panel = _target_weight_score_panel(close_panel, benchmark_close, params)

        eval_index = close_panel.loc[
            (close_panel.index >= pd.Timestamp(start)) & (close_panel.index <= pd.Timestamp(end))
        ].index
        if len(eval_index) == 0:
            return {"equity_curve": pd.DataFrame(), "trades": [], "target_weight_metrics": {}}

        top_n = max(1, int(params.get("target_top_n", 3)))
        tolerance = max(0.0, float(params.get("target_tolerance_pct", 0.0)) / 100.0)
        hold_rank_buffer = max(0, int(params.get("hold_rank_buffer", 0) or 0))
        rebalance_days = set(monthly_rebalance_days(eval_index))
        cash = float(capital)
        positions: dict[str, dict[str, float]] = {}
        trades: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        rebalance_count = 0
        filled_slots: list[int] = []
        target_exposures: list[float] = []
        total_turnover = 0.0

        for day in eval_index:
            day = pd.Timestamp(day).normalize()
            price_row = close_panel.loc[day]
            prices = {
                sym: float(price_row[sym])
                for sym in valid_symbols
                if sym in price_row.index and pd.notna(price_row[sym]) and float(price_row[sym]) > 0
            }

            if day in rebalance_days:
                score_day = _score_date_before(score_panel.index, day)
                targets: list[str] = []
                if score_day is not None:
                    score_row = score_panel.loc[score_day].dropna().sort_values(ascending=False)
                    min_score_floor = params.get("min_score_floor_pct")
                    if min_score_floor is not None:
                        score_row = score_row[score_row >= float(min_score_floor) / 100.0]
                    targets = _select_target_weight_targets(
                        score_row,
                        prices,
                        positions,
                        top_n,
                        hold_rank_buffer,
                    )
                target_exposure = _target_exposure_for_day(day, benchmark_close, params)
                target_exposures.append(target_exposure)
                cash, positions, new_trades, turnover = _execute_target_weight_rebalance(
                    day=day,
                    cash=cash,
                    positions=positions,
                    prices=prices,
                    targets=targets,
                    target_exposure=target_exposure,
                    rebalance_tolerance=tolerance,
                    risk_manager=risk_manager,
                )
                trades.extend(new_trades)
                total_turnover += turnover
                rebalance_count += 1
                filled_slots.append(len([sym for sym in targets if sym in positions]))

            market_value = sum(
                float(pos["qty"]) * prices.get(sym, 0.0)
                for sym, pos in positions.items()
                if prices.get(sym, 0.0) > 0
            )
            value = cash + market_value
            equity_rows.append(
                {
                    "date": day,
                    "value": value,
                    "cash": cash,
                    "n_positions": len(positions),
                    "market_value": market_value,
                }
            )

        years = max(len(equity_rows) / 252, 1 / 252)
        avg_slots = float(np.mean(filled_slots)) if filled_slots else 0.0
        return {
            "equity_curve": pd.DataFrame(equity_rows),
            "trades": trades,
            "target_weight_metrics": {
                "target_top_n": top_n,
                "hold_rank_buffer": hold_rank_buffer,
                "rebalance_count": rebalance_count,
                "avg_slots_filled": round(avg_slots, 2),
                "slot_fill_rate_pct": round(avg_slots / top_n * 100, 1) if top_n else 0,
                "avg_target_exposure_pct": round(
                    float(np.mean(target_exposures)) * 100, 1
                ) if target_exposures else 0,
                "target_weight_turnover_per_year": round(
                    total_turnover / capital / years * 100, 1
                ) if capital > 0 else 0,
            },
        }
    finally:
        if previous_quiet is not None:
            collector.quiet_ohlcv_log = previous_quiet


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
    if metrics.get("sharpe", 0) < 0.45:
        reasons.append("sharpe < 0.45")
    if metrics.get("profit_factor", 0) < 1.2:
        reasons.append("profit_factor < 1.2")
    if metrics.get("wf_positive_rate", 0) < 0.6:
        reasons.append("wf_positive_rate < 0.6")
    if metrics.get("wf_sharpe_positive_rate", 0) < 0.6:
        reasons.append("wf_sharpe_positive_rate < 0.6")
    if metrics.get("mdd", 0) < -20:
        reasons.append("mdd < -20")
    if metrics.get("wf_windows", 0) < 3:
        reasons.append("wf_windows < 3")
    if metrics.get("wf_total_trades", 0) < 30:
        reasons.append("wf_total_trades < 30")
    if metrics.get("total_trades", 0) < 30:
        reasons.append("total_trades < 30")
    if metrics.get("ev_per_trade") is not None and metrics.get("ev_per_trade", 0) <= 0:
        reasons.append("ev_per_trade <= 0")
    if metrics.get("cost_adjusted_cagr") is not None and metrics.get("cost_adjusted_cagr", 0) <= 0:
        reasons.append("cost_adjusted_cagr <= 0")
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

    if spec.strategy == "target_weight_rotation":
        result = run_target_weight_rotation_backtest(
            symbols=symbols,
            start=start,
            end=end,
            capital=capital,
            params=spec.params,
        )
        metrics = calculate_research_metrics(result, capital, benchmark_daily_returns)
        metrics.update(result.get("target_weight_metrics", {}))
        return metrics

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
            "benchmark_aware_rotation",
            "target_weight_rotation",
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

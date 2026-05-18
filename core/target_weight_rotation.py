"""Target-weight rotation planning utilities.

This module is intentionally portfolio-level. It does not expose a
per-symbol strategy signal interface, because target-weight rotation needs to
decide the whole book at once.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_TARGET_WEIGHT_CANDIDATE_ID = (
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1"
)
DEFAULT_LIQUIDITY_LOOKBACK_DAYS = 20
RESEARCH_ONLY_PLAN_PARAMS = (
    "max_new_targets_per_rebalance",
    "max_pairwise_correlation",
)


@dataclass(frozen=True)
class TargetWeightOrder:
    symbol: str
    action: str
    price: float
    quantity: int
    notional: float
    current_quantity: int
    target_quantity: int
    current_weight_pct: float
    target_weight_pct: float
    reason: str


@dataclass(frozen=True)
class TargetWeightPlanValidation:
    allowed: bool
    reason: str
    violations: list[str]
    caps_snapshot: dict[str, Any]


@dataclass(frozen=True)
class TargetWeightPlan:
    candidate_id: str
    as_of_date: str
    trade_day: str
    score_day: str | None
    params_hash: str
    symbols: list[str]
    targets: list[str]
    prices: dict[str, float]
    target_exposure: float
    base_target_exposure: float
    risk_off: bool
    nav: float
    cash_before: float
    market_value_before: float
    cash_after_estimate: float
    gross_exposure_after: float
    target_position_count: int
    orders: list[TargetWeightOrder]
    diagnostics: dict[str, Any]
    target_quantities_after: dict[str, int] | None = None
    position_quantities_before: dict[str, int] | None = None

    @property
    def max_order_notional(self) -> float:
        return max((order.notional for order in self.orders), default=0.0)

    @property
    def buy_notional(self) -> float:
        return sum(order.notional for order in self.orders if order.action == "BUY")

    @property
    def sell_notional(self) -> float:
        return sum(order.notional for order in self.orders if order.action == "SELL")

    @property
    def expected_position_quantities(self) -> dict[str, int]:
        if self.target_quantities_after is not None:
            return {
                symbol: int(quantity)
                for symbol, quantity in self.target_quantities_after.items()
            }
        return {order.symbol: int(order.target_quantity) for order in self.orders}

    @property
    def starting_position_quantities(self) -> dict[str, int]:
        if self.position_quantities_before is not None:
            return {
                symbol: int(quantity)
                for symbol, quantity in self.position_quantities_before.items()
            }
        return {
            order.symbol: int(order.current_quantity)
            for order in self.orders
            if int(order.current_quantity) > 0
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["orders"] = [asdict(order) for order in self.orders]
        return data


def normalize_symbol(value: Any) -> str:
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
    seen: set[str] = set()
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if code and code not in seen:
            normalized.append(code)
            seen.add(code)
    return normalized


def params_hash(params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unsupported_plan_params(params: dict[str, Any]) -> list[str]:
    unsupported = [key for key in RESEARCH_ONLY_PLAN_PARAMS if key in params]

    rebalance_frequency = str(params.get("rebalance_frequency", "monthly") or "monthly").lower().strip()
    if rebalance_frequency not in ("", "monthly"):
        unsupported.append("rebalance_frequency")

    rank_penalty_mode = str(params.get("rank_penalty_mode", "none") or "none").lower().strip()
    if rank_penalty_mode not in ("", "none", "off", "disabled", "downside_risk", "downside", "risk"):
        unsupported.append("rank_penalty_mode")

    target_allocation_mode = str(
        params.get("target_allocation_mode", "equal") or "equal"
    ).lower().strip()
    if target_allocation_mode not in ("", "equal"):
        unsupported.append("target_allocation_mode")

    for key in (
        "correlation_rank_penalty_weight",
        "loss_reentry_guard_trigger_pct",
    ):
        value = params.get(key)
        if value is not None and float(value or 0.0) != 0.0:
            unsupported.append(key)

    return sorted(set(unsupported))


def close_series_from_ohlcv(df: pd.DataFrame | None) -> pd.Series:
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


def daily_value_series_from_ohlcv(df: pd.DataFrame | None) -> pd.Series:
    if df is None or df.empty or "close" not in df.columns or "volume" not in df.columns:
        return pd.Series(dtype=float)

    data = df.copy()
    if "date" in data.columns:
        data = data.set_index("date")
    idx = pd.to_datetime(data.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    close = data["close"].astype(float)
    volume = data["volume"].astype(float)
    series = (close * volume).copy()
    series.index = idx.normalize()
    return series[series > 0].groupby(level=0).last().sort_index()


def _liquidity_diagnostics_from_ohlcv(
    frames: dict[str, pd.DataFrame],
    *,
    trade_day: pd.Timestamp,
    lookback_days: int = DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
) -> dict[str, Any]:
    symbols: dict[str, dict[str, Any]] = {}
    for symbol, frame in frames.items():
        daily_value = daily_value_series_from_ohlcv(frame)
        if daily_value.empty:
            symbols[symbol] = {
                "complete": False,
                "reason": "missing close/volume liquidity data",
                "observations": 0,
                "avg_daily_value": None,
                "last_daily_value": None,
            }
            continue

        window = daily_value[daily_value.index <= trade_day].tail(lookback_days)
        if window.empty:
            symbols[symbol] = {
                "complete": False,
                "reason": "no liquidity rows on or before trade day",
                "observations": 0,
                "avg_daily_value": None,
                "last_daily_value": None,
            }
            continue

        symbols[symbol] = {
            "complete": True,
            "reason": "liquidity window available",
            "observations": int(len(window)),
            "avg_daily_value": round(float(window.mean()), 2),
            "last_daily_value": round(float(window.iloc[-1]), 2),
        }

    return {
        "lookback_days": int(lookback_days),
        "symbols": symbols,
    }


def _fetch_korean_stock(collector: Any, symbol: str, start: str, end: str) -> pd.DataFrame:
    if hasattr(collector, "fetch_korean_stock"):
        return collector.fetch_korean_stock(symbol, start, end)
    return collector.fetch_stock(symbol, start_date=start, end_date=end)


def _score_date_before(index: pd.Index, day: pd.Timestamp) -> pd.Timestamp | None:
    prior = pd.DatetimeIndex(index)[pd.DatetimeIndex(index) < pd.Timestamp(day)]
    if len(prior) == 0:
        return None
    return pd.Timestamp(prior[-1]).normalize()


def _date_payload(dates: dict[str, pd.Timestamp]) -> dict[str, str]:
    return {
        symbol: pd.Timestamp(day).strftime("%Y-%m-%d")
        for symbol, day in sorted(dates.items())
    }


def _benchmark_required_for_target_weight(params: dict[str, Any]) -> bool:
    score_mode = str(params.get("score_mode", "absolute")).lower().strip()
    exposure_mode = str(params.get("market_exposure_mode", "fixed")).lower().strip()
    return score_mode == "benchmark_excess" or exposure_mode.startswith("benchmark_")


def _target_weight_rank_penalty_panel(
    close_panel: pd.DataFrame,
    params: dict[str, Any],
) -> pd.DataFrame | None:
    mode = str(params.get("rank_penalty_mode", "none") or "none").lower().strip()
    if mode in ("none", "off", "disabled", ""):
        return None
    if mode not in ("downside_risk", "downside", "risk"):
        raise ValueError(
            "unsupported_rank_penalty_mode: "
            f"{mode}; expected downside_risk, downside, risk, or none"
        )

    lookback = max(2, int(params.get("rank_penalty_lookback", 60) or 60))
    min_periods = max(
        2,
        min(lookback, int(params.get("rank_penalty_min_periods", lookback) or lookback)),
    )
    downside_weight = max(0.0, float(params.get("downside_vol_penalty_weight", 0.0) or 0.0))
    drawdown_weight = max(0.0, float(params.get("drawdown_penalty_weight", 0.0) or 0.0))
    penalty = pd.DataFrame(0.0, index=close_panel.index, columns=close_panel.columns)

    if downside_weight > 0:
        downside_returns = close_panel.pct_change().clip(upper=0.0)
        downside_vol = (
            downside_returns.rolling(lookback, min_periods=min_periods).std()
            * np.sqrt(252)
        )
        penalty = penalty + downside_vol.fillna(0.0) * downside_weight

    if drawdown_weight > 0:
        rolling_peak = close_panel.rolling(lookback, min_periods=min_periods).max()
        drawdown = (close_panel / rolling_peak - 1.0).clip(upper=0.0).abs()
        penalty = penalty + drawdown.fillna(0.0) * drawdown_weight

    return penalty


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
        score = composite
    elif benchmark_close.empty:
        score = pd.DataFrame(np.nan, index=close_panel.index, columns=close_panel.columns)
    else:
        benchmark_composite = (
            short_w * benchmark_close.pct_change(short_lb)
            + (1.0 - short_w) * benchmark_close.pct_change(long_lb)
        )
        aligned = benchmark_composite.reindex(close_panel.index, method="ffill")
        score = composite.sub(aligned, axis=0)

    rank_penalty = _target_weight_rank_penalty_panel(close_panel, params)
    if rank_penalty is None:
        return score
    return score - rank_penalty


def _target_exposure_for_day(
    day: pd.Timestamp,
    benchmark_close: pd.Series,
    params: dict[str, Any],
) -> float:
    base = max(0.0, min(float(params.get("target_exposure", 0.85)), 1.0))
    mode = str(params.get("market_exposure_mode", "fixed")).lower().strip()
    if mode == "fixed" or benchmark_close.empty:
        return base

    score_day = _score_date_before(benchmark_close.index, day)
    if score_day is None:
        return base

    def bear_exposure() -> float:
        return max(0.0, min(float(params.get("bear_target_exposure", base)), 1.0))

    ma_period = int(params.get("market_ma_period", 120))
    risk_off = False
    if ma_period > 0:
        sma = benchmark_close.rolling(ma_period, min_periods=ma_period).mean()
        if not pd.isna(sma.get(score_day, np.nan)):
            risk_off = float(benchmark_close.loc[score_day]) < float(sma.loc[score_day])
    if mode == "benchmark_sma":
        return bear_exposure() if risk_off else base

    if mode != "benchmark_risk":
        return base

    history = benchmark_close.loc[benchmark_close.index <= score_day].dropna().astype(float)
    if history.empty:
        return base

    drawdown_trigger = params.get("benchmark_drawdown_trigger_pct")
    if drawdown_trigger is not None:
        drawdown_lookback = max(2, int(params.get("benchmark_drawdown_lookback", ma_period or 120)))
        drawdown_window = history.tail(drawdown_lookback)
        rolling_peak = float(drawdown_window.max()) if not drawdown_window.empty else 0.0
        if rolling_peak > 0:
            drawdown_pct = (float(history.iloc[-1]) / rolling_peak - 1.0) * 100
            if drawdown_pct <= -abs(float(drawdown_trigger)):
                risk_off = True

    vol_trigger = params.get("benchmark_vol_trigger_pct")
    if vol_trigger is not None:
        vol_lookback = max(2, int(params.get("benchmark_vol_lookback", 60)))
        returns = history.pct_change().dropna().tail(vol_lookback)
        if len(returns) >= 2:
            realized_vol_pct = float(returns.std()) * np.sqrt(252) * 100
            if realized_vol_pct >= float(vol_trigger):
                risk_off = True

    return bear_exposure() if risk_off else base


def _portfolio_drawdown_guard_enabled(params: dict[str, Any]) -> bool:
    return max(
        0.0,
        float(params.get("portfolio_drawdown_guard_trigger_pct", 0.0) or 0.0),
    ) > 0


def _raise_portfolio_drawdown_guard_state_required() -> None:
    raise ValueError(
        "target_weight_portfolio_drawdown_guard_state_required: "
        "portfolio drawdown guard needs explicit prior equity/peak/cooldown state"
    )


def _apply_portfolio_drawdown_guard(
    *,
    target_exposure: float,
    base_target_exposure: float,
    nav: float,
    params: dict[str, Any],
    state: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    trigger_pct = max(
        0.0,
        float(params.get("portfolio_drawdown_guard_trigger_pct", 0.0) or 0.0),
    )
    enabled = trigger_pct > 0
    if not enabled:
        return target_exposure, {
            "enabled": False,
            "active": False,
            "triggered": False,
            "reason": "portfolio drawdown guard disabled",
            "trigger_pct": 0.0,
            "exposure_pct": 0.0,
            "cooldown_rebalances": 0,
            "cooldown_before": 0,
            "cooldown_after_trigger": 0,
            "cooldown_after_plan": 0,
            "drawdown_pct": 0.0,
            "last_equity_value": round(float(nav), 2),
            "peak_value": round(float(nav), 2),
            "target_exposure_before": round(target_exposure, 4),
            "target_exposure_after": round(target_exposure, 4),
            "state_source": "disabled",
        }

    if state is None:
        _raise_portfolio_drawdown_guard_state_required()

    raw_state = dict(state)
    last_equity = float(
        raw_state.get("last_equity_value", raw_state.get("last_evidence_value", nav)) or nav
    )
    if last_equity <= 0:
        last_equity = float(nav)
    peak_value = float(raw_state.get("peak_value", last_equity) or last_equity)
    peak_value = max(peak_value, last_equity)
    cooldown_before = max(0, int(raw_state.get("cooldown_remaining", 0) or 0))
    guard_exposure = max(
        0.0,
        min(
            float(
                params.get(
                    "portfolio_drawdown_guard_exposure",
                    params.get("bear_target_exposure", base_target_exposure),
                )
                or 0.0
            ),
            base_target_exposure,
        ),
    )
    cooldown_rebalances = max(
        0,
        int(params.get("portfolio_drawdown_guard_cooldown_rebalances", 0) or 0),
    )
    drawdown_pct = (last_equity / peak_value - 1.0) * 100 if peak_value > 0 else 0.0
    triggered = drawdown_pct <= -trigger_pct
    cooldown_after_trigger = cooldown_before
    if triggered:
        cooldown_after_trigger = max(cooldown_after_trigger, cooldown_rebalances + 1)

    active = cooldown_after_trigger > 0
    exposure_after = min(target_exposure, guard_exposure) if active else target_exposure
    cooldown_after_plan = max(0, cooldown_after_trigger - 1) if active else cooldown_after_trigger
    return exposure_after, {
        "enabled": True,
        "active": active,
        "triggered": triggered,
        "reason": (
            "portfolio drawdown guard reduced target exposure"
            if active
            else "portfolio drawdown guard observed without exposure reduction"
        ),
        "trigger_pct": round(trigger_pct, 4),
        "exposure_pct": round(guard_exposure * 100, 2),
        "cooldown_rebalances": cooldown_rebalances,
        "cooldown_before": cooldown_before,
        "cooldown_after_trigger": cooldown_after_trigger,
        "cooldown_after_plan": cooldown_after_plan,
        "drawdown_pct": round(drawdown_pct, 2),
        "last_equity_value": round(last_equity, 2),
        "peak_value": round(peak_value, 2),
        "target_exposure_before": round(target_exposure, 4),
        "target_exposure_after": round(exposure_after, 4),
        "state_source": str(raw_state.get("source", "explicit")),
        "state_record_count": int(raw_state.get("record_count", 0) or 0),
        "latest_record_date": raw_state.get("latest_record_date"),
    }


def _select_target_weight_targets(
    score_row: pd.Series,
    prices: dict[str, float],
    current_positions: dict[str, dict[str, float]],
    top_n: int,
    hold_rank_buffer: int,
    max_targets_per_sector: int | None = None,
    sector_map: dict[str, str] | None = None,
) -> list[str]:
    ranked = [
        sym
        for sym in score_row.index.tolist()
        if sym in prices and prices.get(sym, 0.0) > 0
    ]
    targets = ranked[:top_n]
    if hold_rank_buffer <= 0 or not current_positions:
        return _limit_targets_per_sector(
            ranked=ranked,
            targets=targets,
            top_n=top_n,
            max_targets_per_sector=max_targets_per_sector,
            sector_map=sector_map,
        )

    retention_pool = ranked[top_n : top_n + hold_rank_buffer]
    for held in [sym for sym in retention_pool if sym in current_positions]:
        if held in targets:
            continue
        replacement_idx = next(
            (idx for idx in range(len(targets) - 1, -1, -1) if targets[idx] not in current_positions),
            None,
        )
        if replacement_idx is None:
            break
        targets[replacement_idx] = held
    return _limit_targets_per_sector(
        ranked=ranked,
        targets=targets,
        top_n=top_n,
        max_targets_per_sector=max_targets_per_sector,
        sector_map=sector_map,
    )


def _preferred_share_base_symbol(symbol: str) -> str:
    raw = str(symbol).strip()
    if raw.upper().endswith(".KS") and raw[:-3].isdigit():
        raw = raw[:-3].zfill(6)
    if raw.isdigit() and len(raw) == 6 and raw[-1] != "0":
        return f"{raw[:5]}0"
    return ""


def _sector_lookup(symbol: str, sector_map: dict[str, str]) -> tuple[str, str]:
    raw = str(symbol).strip()
    lookup_keys = [raw]
    if raw.isdigit():
        lookup_keys.append(raw.zfill(6))
    if raw.upper().endswith(".KS") and raw[:-3].isdigit():
        lookup_keys.append(raw[:-3].zfill(6))
    for key in dict.fromkeys(lookup_keys):
        sector = str(sector_map.get(key, "") or "").strip()
        if sector:
            return sector, key
    base_symbol = _preferred_share_base_symbol(raw)
    if base_symbol:
        sector = str(sector_map.get(base_symbol, "") or "").strip()
        if sector:
            return sector, base_symbol
    return "", ""


def _sector_for_symbol(symbol: str, sector_map: dict[str, str]) -> str:
    return _sector_lookup(symbol, sector_map)[0]


def _limit_targets_per_sector(
    *,
    ranked: list[str],
    targets: list[str],
    top_n: int,
    max_targets_per_sector: int | None,
    sector_map: dict[str, str] | None,
) -> list[str]:
    if max_targets_per_sector is None or int(max_targets_per_sector) <= 0:
        return targets
    if not sector_map:
        return targets

    cap = max(1, int(max_targets_per_sector))
    selected: list[str] = []
    sector_counts: dict[str, int] = {}

    def can_add(symbol: str) -> bool:
        if symbol in selected:
            return False
        sector = _sector_for_symbol(symbol, sector_map)
        if sector and sector_counts.get(sector, 0) >= cap:
            return False
        return True

    def add(symbol: str) -> None:
        selected.append(symbol)
        sector = _sector_for_symbol(symbol, sector_map)
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    for sym in targets:
        if len(selected) >= top_n:
            break
        if can_add(sym):
            add(sym)

    for sym in ranked:
        if len(selected) >= top_n:
            break
        if can_add(sym):
            add(sym)

    return selected


def _coerce_positions(positions: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for raw_symbol, raw in (positions or {}).items():
        symbol = normalize_symbol(raw_symbol)
        if hasattr(raw, "quantity"):
            qty = int(getattr(raw, "quantity") or 0)
            avg_price = float(getattr(raw, "avg_price") or 0.0)
        else:
            qty = int(raw.get("quantity", raw.get("qty", 0)) or 0)
            avg_price = float(raw.get("avg_price", 0.0) or 0.0)
        if qty > 0:
            out[symbol] = {"quantity": qty, "avg_price": avg_price}
    return out


def load_canonical_target_weight_spec(
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
):
    from tools.evaluate_and_promote import build_canonical_research_candidate_specs

    specs = build_canonical_research_candidate_specs([candidate_id])
    return specs[0]


def build_target_weight_plan(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    symbols: list[Any],
    params: dict[str, Any],
    cash: float,
    positions: dict[str, Any] | None = None,
    as_of_date: str | datetime | None = None,
    collector: Any | None = None,
    portfolio_drawdown_guard_state: dict[str, Any] | None = None,
) -> TargetWeightPlan:
    """Build a one-day target-weight rebalance plan.

    Scores and benchmark risk overlays always use the latest trading day before
    the planned trade day, matching the research backtest's no-lookahead rule.
    """
    plan_params_unsupported = unsupported_plan_params(dict(params))
    if plan_params_unsupported:
        raise ValueError(
            "target-weight plan does not yet support research-only params: "
            + ", ".join(plan_params_unsupported)
        )
    if _portfolio_drawdown_guard_enabled(dict(params)) and portfolio_drawdown_guard_state is None:
        _raise_portfolio_drawdown_guard_state_required()

    from core.data_collector import DataCollector

    symbols = normalize_symbols(symbols)
    current_positions = _coerce_positions(positions)
    symbol_set = set(symbols)
    position_symbols_outside_universe = [
        symbol for symbol in sorted(current_positions) if symbol not in symbol_set
    ]
    pricing_symbols = normalize_symbols([*symbols, *position_symbols_outside_universe])
    params = dict(params)
    as_of_ts = pd.Timestamp(as_of_date or datetime.now()).normalize()
    end = as_of_ts.strftime("%Y-%m-%d")

    short_lb = int(params.get("short_lookback", 60))
    long_lb = int(params.get("long_lookback", 120))
    ma_period = int(params.get("market_ma_period", 0) or 0)
    vol_lookback = int(params.get("benchmark_vol_lookback", 0) or 0)
    drawdown_lookback = int(params.get("benchmark_drawdown_lookback", 0) or 0)
    warmup_days = max(short_lb * 3, long_lb * 3, ma_period * 3, vol_lookback * 3, drawdown_lookback * 3, 180)
    start = (as_of_ts - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")

    collector = collector or DataCollector()
    max_targets_per_sector_raw = params.get("max_targets_per_sector")
    max_targets_per_sector = (
        max(1, int(max_targets_per_sector_raw))
        if max_targets_per_sector_raw is not None
        else None
    )
    sector_map_for_selection: dict[str, str] | None = None
    missing_sector_symbols: list[str] = []
    inferred_sector_symbols: list[dict[str, str]] = []
    if max_targets_per_sector is not None:
        try:
            sector_map_source = (
                collector.get_sector_map()
                if hasattr(collector, "get_sector_map")
                else DataCollector.get_sector_map()
            )
        except Exception as exc:
            raise ValueError(
                "target_weight_sector_map_missing: "
                f"max_targets_per_sector={max_targets_per_sector}; "
                f"failed to load sector map: {exc}"
            ) from exc
        sector_map_for_selection = {
            normalize_symbol(symbol): str(sector).strip()
            for symbol, sector in dict(sector_map_source or {}).items()
            if str(sector or "").strip()
        }
        if not sector_map_for_selection:
            raise ValueError(
                "target_weight_sector_map_missing: "
                f"max_targets_per_sector={max_targets_per_sector}; "
                "sector map is required for sector-capped target-weight planning"
            )
        inferred_sector_symbols = []
        for symbol in symbols:
            sector, source_symbol = _sector_lookup(symbol, sector_map_for_selection)
            if sector and source_symbol and source_symbol != normalize_symbol(symbol):
                inferred_sector_symbols.append({
                    "symbol": symbol,
                    "source_symbol": source_symbol,
                    "sector": sector,
                })
        missing_sector_symbols = [
            symbol
            for symbol in symbols
            if not _sector_for_symbol(symbol, sector_map_for_selection)
        ]
        if missing_sector_symbols:
            missing_text = ", ".join(missing_sector_symbols[:10])
            if len(missing_sector_symbols) > 10:
                missing_text = f"{missing_text}, +{len(missing_sector_symbols) - 10} more"
            raise ValueError(
                "target_weight_sector_map_incomplete: "
                f"max_targets_per_sector={max_targets_per_sector}; "
                f"missing_symbols={missing_text}; "
                "sector-capped target-weight planning requires sector coverage for all planning symbols"
            )
    close_parts: list[pd.Series] = []
    valid_symbols: list[str] = []
    missing_symbols: list[str] = []
    price_last_dates: dict[str, pd.Timestamp] = {}
    ohlcv_frames: dict[str, pd.DataFrame] = {}
    price_data_symbols: list[str] = []
    for symbol in pricing_symbols:
        frame = _fetch_korean_stock(collector, symbol, start, end)
        close = close_series_from_ohlcv(frame)
        if close.empty:
            if symbol in symbols:
                missing_symbols.append(symbol)
            continue
        close = close[close.index <= as_of_ts]
        if close.empty:
            if symbol in symbols:
                missing_symbols.append(symbol)
            continue
        price_last_dates[symbol] = pd.Timestamp(close.index[-1]).normalize()
        ohlcv_frames[symbol] = frame
        close.name = symbol
        close_parts.append(close)
        price_data_symbols.append(symbol)
        if symbol in symbols:
            valid_symbols.append(symbol)

    missing_position_symbols = [
        symbol for symbol in sorted(current_positions) if symbol not in price_last_dates
    ]
    if missing_position_symbols:
        missing_text = ", ".join(missing_position_symbols)
        raise ValueError(
            "target_weight_position_price_missing: "
            f"positions_without_price={missing_text}; "
            "refresh market data or close unmanaged positions before target-weight planning"
        )

    if not valid_symbols:
        raise ValueError("no valid price data for target-weight planning")

    raw_close_panel = pd.concat(close_parts, axis=1).sort_index()
    raw_close_panel = raw_close_panel[~raw_close_panel.index.duplicated(keep="last")]
    close_panel = raw_close_panel.ffill()
    close_panel = close_panel[close_panel.index <= as_of_ts]
    if close_panel.empty:
        raise ValueError("no price rows on or before as_of_date")

    trade_day = pd.Timestamp(close_panel.index[-1]).normalize()
    stale_price_dates = {
        symbol: day
        for symbol, day in price_last_dates.items()
        if pd.Timestamp(day).normalize() < trade_day
    }
    if stale_price_dates:
        stale_text = ", ".join(
            f"{symbol}={day}"
            for symbol, day in _date_payload(stale_price_dates).items()
        )
        raise ValueError(
            "target_weight_stale_price_data: "
            f"trade_day={trade_day.strftime('%Y-%m-%d')} stale_symbols={stale_text}; "
            "refresh market data before target-weight planning"
        )
    price_row = close_panel.loc[trade_day]
    prices = {
        sym: float(price_row[sym])
        for sym in price_data_symbols
        if sym in price_row.index and pd.notna(price_row[sym]) and float(price_row[sym]) > 0
    }
    if not prices:
        raise ValueError("no valid latest prices for target-weight planning")

    benchmark_symbol = str(params.get("benchmark_symbol", "KS11"))
    benchmark_close = close_series_from_ohlcv(
        _fetch_korean_stock(collector, benchmark_symbol, start, end)
    )
    benchmark_close = benchmark_close[benchmark_close.index <= trade_day]
    benchmark_last_date = (
        pd.Timestamp(benchmark_close.index[-1]).normalize()
        if not benchmark_close.empty
        else None
    )
    score_close_panel = close_panel[valid_symbols]
    score_panel = _target_weight_score_panel(score_close_panel, benchmark_close, params)
    score_day = _score_date_before(score_panel.index, trade_day)
    if (
        score_day is not None
        and _benchmark_required_for_target_weight(params)
        and (benchmark_last_date is None or benchmark_last_date < score_day)
    ):
        latest = benchmark_last_date.strftime("%Y-%m-%d") if benchmark_last_date is not None else "missing"
        raise ValueError(
            "target_weight_benchmark_price_stale: "
            f"benchmark_symbol={benchmark_symbol} trade_day={trade_day.strftime('%Y-%m-%d')} "
            f"score_day={score_day.strftime('%Y-%m-%d')} benchmark_latest={latest}; "
            "refresh benchmark data before target-weight planning"
        )

    targets: list[str] = []
    if score_day is not None:
        score_row = score_panel.loc[score_day].dropna().sort_values(ascending=False)
        min_score_floor = params.get("min_score_floor_pct")
        if min_score_floor is not None:
            score_row = score_row[score_row >= float(min_score_floor) / 100.0]
        targets = _select_target_weight_targets(
            score_row,
            prices,
            current_positions,
            max(1, int(params.get("target_top_n", 3))),
            max(0, int(params.get("hold_rank_buffer", 0) or 0)),
            max_targets_per_sector,
            sector_map_for_selection,
        )

    target_exposure = _target_exposure_for_day(trade_day, benchmark_close, params)
    base_target_exposure = max(0.0, min(float(params.get("target_exposure", 0.85)), 1.0))
    market_target_exposure = target_exposure

    market_value_before = sum(
        pos["quantity"] * prices.get(symbol, 0.0)
        for symbol, pos in current_positions.items()
        if prices.get(symbol, 0.0) > 0
    )
    cash_before = float(cash)
    nav = cash_before + market_value_before
    if nav <= 0:
        raise ValueError("nav must be positive for target-weight planning")

    target_exposure, portfolio_drawdown_guard = _apply_portfolio_drawdown_guard(
        target_exposure=target_exposure,
        base_target_exposure=base_target_exposure,
        nav=nav,
        params=params,
        state=portfolio_drawdown_guard_state,
    )
    risk_off = target_exposure < base_target_exposure - 1e-9

    target_set = {sym for sym in targets if prices.get(sym, 0.0) > 0}
    per_target_value = nav * target_exposure / len(target_set) if target_set else 0.0
    tolerance = max(0.0, float(params.get("target_tolerance_pct", 0.0)) / 100.0)
    position_loss_reduce_trigger_pct = max(
        0.0,
        float(params.get("position_loss_reduce_trigger_pct", 0.0) or 0.0),
    )
    position_loss_reduce_target_fraction = max(
        0.0,
        min(float(params.get("position_loss_reduce_target_fraction", 0.50) or 0.0), 1.0),
    )
    position_loss_reduce_enabled = (
        position_loss_reduce_trigger_pct > 0
        and position_loss_reduce_target_fraction < 1.0
    )
    target_value_multipliers: dict[str, float] = {}
    position_loss_reduce_losses: list[float] = []
    position_loss_reduce_signal_prices: dict[str, float] = {}
    if position_loss_reduce_enabled and target_set and score_day is not None:
        raw_score_row = (
            raw_close_panel.loc[score_day]
            if score_day in raw_close_panel.index
            else pd.Series(dtype=float)
        )
        position_loss_reduce_signal_prices = {
            sym: float(raw_score_row[sym])
            for sym in target_set
            if sym in raw_score_row.index
            and pd.notna(raw_score_row[sym])
            and float(raw_score_row[sym]) > 0
        }
        for sym in sorted(target_set):
            pos = current_positions.get(sym)
            if not pos:
                continue
            qty = int(pos.get("quantity", 0) or 0)
            avg_price = float(pos.get("avg_price", 0.0) or 0.0)
            signal_price = float(position_loss_reduce_signal_prices.get(sym, 0.0) or 0.0)
            if qty <= 0 or avg_price <= 0 or signal_price <= 0:
                continue
            loss_pct = (signal_price / avg_price - 1.0) * 100
            if loss_pct <= -position_loss_reduce_trigger_pct:
                target_value_multipliers[sym] = position_loss_reduce_target_fraction
                position_loss_reduce_losses.append(loss_pct)

    projected_qty: dict[str, int] = {
        sym: int(pos["quantity"])
        for sym, pos in current_positions.items()
        if prices.get(sym, 0.0) > 0
    }
    desired_qty: dict[str, int] = {}
    for sym in set(projected_qty) | target_set:
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        if sym in target_set:
            target_value = per_target_value * target_value_multipliers.get(sym, 1.0)
            target_qty = int(target_value // price)
            if sym in target_value_multipliers and projected_qty.get(sym, 0) > 0:
                target_qty = min(target_qty, projected_qty.get(sym, 0))
            desired_qty[sym] = target_qty
        else:
            desired_qty[sym] = 0

    sell_orders: list[TargetWeightOrder] = []
    skipped_tolerance: list[str] = []
    tolerance_bypass_symbols = set(target_value_multipliers)
    for sym in sorted(desired_qty):
        current_qty = projected_qty.get(sym, 0)
        target_qty = desired_qty[sym]
        delta = target_qty - current_qty
        if delta >= 0:
            continue
        price = prices[sym]
        qty = abs(delta)
        notional = qty * price
        if notional / nav < tolerance and sym not in tolerance_bypass_symbols:
            skipped_tolerance.append(sym)
            continue
        sell_orders.append(
            TargetWeightOrder(
                symbol=sym,
                action="SELL",
                price=round(price, 4),
                quantity=qty,
                notional=round(notional, 2),
                current_quantity=current_qty,
                target_quantity=target_qty,
                current_weight_pct=round(current_qty * price / nav * 100, 2),
                target_weight_pct=round(target_qty * price / nav * 100, 2),
                reason=(
                    "target_weight_position_loss_reduce_sell"
                    if sym in target_value_multipliers
                    else "target_weight_rebalance_sell"
                ),
            )
        )
        projected_qty[sym] = target_qty

    cash_after_sells = cash_before + sum(order.notional for order in sell_orders)

    raw_buy_orders: list[tuple[str, int, float, int, int]] = []
    total_buy_notional = 0.0
    for sym in sorted(desired_qty):
        current_qty = projected_qty.get(sym, 0)
        target_qty = desired_qty[sym]
        delta = target_qty - current_qty
        if delta <= 0:
            continue
        price = prices[sym]
        notional = delta * price
        if notional / nav < tolerance:
            skipped_tolerance.append(sym)
            continue
        raw_buy_orders.append((sym, delta, price, current_qty, target_qty))
        total_buy_notional += notional

    buy_scale = 1.0
    if total_buy_notional > cash_after_sells and total_buy_notional > 0:
        buy_scale = max(cash_after_sells / total_buy_notional * 0.998, 0.0)

    buy_orders: list[TargetWeightOrder] = []
    for sym, qty, price, current_qty, target_qty in raw_buy_orders:
        scaled_qty = int(qty * buy_scale)
        if scaled_qty <= 0:
            skipped_tolerance.append(sym)
            continue
        scaled_target_qty = current_qty + scaled_qty
        notional = scaled_qty * price
        buy_orders.append(
            TargetWeightOrder(
                symbol=sym,
                action="BUY",
                price=round(price, 4),
                quantity=scaled_qty,
                notional=round(notional, 2),
                current_quantity=current_qty,
                target_quantity=scaled_target_qty,
                current_weight_pct=round(current_qty * price / nav * 100, 2),
                target_weight_pct=round(scaled_target_qty * price / nav * 100, 2),
                reason="target_weight_rebalance_buy",
            )
        )
        projected_qty[sym] = scaled_target_qty

    orders = [*sell_orders, *buy_orders]
    position_quantities_before = {
        sym: int(pos["quantity"])
        for sym, pos in sorted(current_positions.items())
    }
    target_quantities_after = {
        sym: int(projected_qty.get(sym, 0))
        for sym in sorted(desired_qty)
    }
    cash_after_estimate = cash_before + sum(o.notional for o in sell_orders) - sum(o.notional for o in buy_orders)
    gross_exposure_after = sum(
        qty * prices.get(sym, 0.0)
        for sym, qty in projected_qty.items()
        if qty > 0 and prices.get(sym, 0.0) > 0
    )
    target_position_count = sum(1 for qty in projected_qty.values() if qty > 0)
    selected_sector_counts: dict[str, int] = {}
    selected_sector_missing_symbols: list[str] = []
    if max_targets_per_sector is not None and sector_map_for_selection:
        for sym in targets:
            sector = _sector_for_symbol(sym, sector_map_for_selection)
            if sector:
                selected_sector_counts[sector] = selected_sector_counts.get(sector, 0) + 1
            else:
                selected_sector_missing_symbols.append(sym)

    return TargetWeightPlan(
        candidate_id=candidate_id,
        as_of_date=as_of_ts.strftime("%Y-%m-%d"),
        trade_day=trade_day.strftime("%Y-%m-%d"),
        score_day=score_day.strftime("%Y-%m-%d") if score_day is not None else None,
        params_hash=params_hash(params),
        symbols=valid_symbols,
        targets=targets,
        prices={sym: round(price, 4) for sym, price in prices.items()},
        target_exposure=round(target_exposure, 4),
        base_target_exposure=round(base_target_exposure, 4),
        risk_off=risk_off,
        nav=round(nav, 2),
        cash_before=round(cash_before, 2),
        market_value_before=round(market_value_before, 2),
        cash_after_estimate=round(cash_after_estimate, 2),
        gross_exposure_after=round(gross_exposure_after, 2),
        target_position_count=target_position_count,
        orders=orders,
        diagnostics={
            "missing_symbols": missing_symbols,
            "position_symbols_outside_universe": position_symbols_outside_universe,
            "missing_position_symbols": missing_position_symbols,
            "skipped_tolerance_symbols": skipped_tolerance,
            "buy_scale": round(buy_scale, 4),
            "benchmark_symbol": benchmark_symbol,
            "market_target_exposure": round(market_target_exposure, 4),
            "portfolio_drawdown_guard": portfolio_drawdown_guard,
            "portfolio_drawdown_guard_enabled": portfolio_drawdown_guard["enabled"],
            "portfolio_drawdown_guard_active": portfolio_drawdown_guard["active"],
            "portfolio_drawdown_guard_triggered": portfolio_drawdown_guard["triggered"],
            "portfolio_drawdown_guard_drawdown_pct": portfolio_drawdown_guard["drawdown_pct"],
            "portfolio_drawdown_guard_cooldown_after_plan": portfolio_drawdown_guard["cooldown_after_plan"],
            "rank_penalty_mode": str(params.get("rank_penalty_mode", "none") or "none").lower().strip(),
            "max_targets_per_sector": max_targets_per_sector,
            "sector_map_size": len(sector_map_for_selection or {}),
            "sector_map_missing_symbols": missing_sector_symbols,
            "sector_map_inferred_symbols": inferred_sector_symbols,
            "selected_sector_counts": selected_sector_counts,
            "selected_sector_missing_symbols": selected_sector_missing_symbols,
            "position_loss_reduce_enabled": position_loss_reduce_enabled,
            "position_loss_reduce_trigger_pct": (
                round(position_loss_reduce_trigger_pct, 4)
                if position_loss_reduce_enabled
                else 0.0
            ),
            "position_loss_reduce_target_fraction_pct": (
                round(position_loss_reduce_target_fraction * 100, 2)
                if position_loss_reduce_enabled
                else 100.0
            ),
            "position_loss_reduce_symbols": sorted(target_value_multipliers),
            "position_loss_reduce_rebalance_count": 1 if target_value_multipliers else 0,
            "position_loss_reduce_position_count": len(target_value_multipliers),
            "position_loss_reduce_worst_loss_pct": (
                round(min(position_loss_reduce_losses), 2)
                if position_loss_reduce_losses
                else 0
            ),
            "position_loss_reduce_signal_price_mode": (
                "prior_close"
                if position_loss_reduce_enabled
                else "none"
            ),
            "position_loss_reduce_signal_prices": {
                sym: round(price, 4)
                for sym, price in sorted(position_loss_reduce_signal_prices.items())
            },
            "price_last_dates": _date_payload(price_last_dates),
            "benchmark_last_date": (
                benchmark_last_date.strftime("%Y-%m-%d")
                if benchmark_last_date is not None
                else None
            ),
            "position_avg_prices_before": {
                sym: float(pos.get("avg_price", 0.0) or 0.0)
                for sym, pos in sorted(current_positions.items())
            },
            "liquidity": _liquidity_diagnostics_from_ohlcv(
                {
                    symbol: ohlcv_frames[symbol]
                    for symbol in price_data_symbols
                    if symbol in ohlcv_frames
                },
                trade_day=trade_day,
            ),
            "generated_at": datetime.now().isoformat(),
        },
        target_quantities_after=target_quantities_after,
        position_quantities_before=position_quantities_before,
    )


def validate_plan_against_pilot(
    plan: TargetWeightPlan,
    pilot_check: Any,
) -> TargetWeightPlanValidation:
    violations: list[str] = []
    caps = getattr(pilot_check, "caps_snapshot", None) or {}

    if not getattr(pilot_check, "allowed", False):
        violations.append(getattr(pilot_check, "reason", "pilot entry blocked"))

    remaining_orders = getattr(pilot_check, "remaining_orders", None)
    if remaining_orders is not None and len(plan.orders) > int(remaining_orders):
        violations.append(f"orders {len(plan.orders)} > remaining_orders {remaining_orders}")

    remaining_exposure = getattr(pilot_check, "remaining_exposure", None)
    if remaining_exposure is not None:
        required_exposure_increase = max(
            0.0,
            float(plan.gross_exposure_after) - float(plan.market_value_before),
        )
        if required_exposure_increase > float(remaining_exposure) + 1e-6:
            violations.append(
                "required exposure increase "
                f"{required_exposure_increase:,.0f} > remaining_exposure {float(remaining_exposure):,.0f}"
            )

    max_notional = caps.get("max_notional_per_trade")
    if max_notional is not None and plan.max_order_notional > float(max_notional):
        violations.append(
            f"max order notional {plan.max_order_notional:,.0f} > cap {float(max_notional):,.0f}"
        )

    max_positions = caps.get("max_concurrent_positions")
    if max_positions is not None and plan.target_position_count > int(max_positions):
        violations.append(
            f"target positions {plan.target_position_count} > cap {int(max_positions)}"
        )

    max_exposure = caps.get("max_gross_exposure")
    if max_exposure is not None and plan.gross_exposure_after > float(max_exposure):
        violations.append(
            f"gross exposure {plan.gross_exposure_after:,.0f} > cap {float(max_exposure):,.0f}"
        )

    if violations:
        return TargetWeightPlanValidation(
            allowed=False,
            reason="; ".join(violations),
            violations=violations,
            caps_snapshot=caps,
        )

    return TargetWeightPlanValidation(
        allowed=True,
        reason="pilot caps satisfied",
        violations=[],
        caps_snapshot=caps,
    )

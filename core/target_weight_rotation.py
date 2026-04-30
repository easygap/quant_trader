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
    "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35"
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

    @property
    def max_order_notional(self) -> float:
        return max((order.notional for order in self.orders), default=0.0)

    @property
    def buy_notional(self) -> float:
        return sum(order.notional for order in self.orders if order.action == "BUY")

    @property
    def sell_notional(self) -> float:
        return sum(order.notional for order in self.orders if order.action == "SELL")

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


def _fetch_korean_stock(collector: Any, symbol: str, start: str, end: str) -> pd.DataFrame:
    if hasattr(collector, "fetch_korean_stock"):
        return collector.fetch_korean_stock(symbol, start, end)
    return collector.fetch_stock(symbol, start_date=start, end_date=end)


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


def _select_target_weight_targets(
    score_row: pd.Series,
    prices: dict[str, float],
    current_positions: dict[str, dict[str, float]],
    top_n: int,
    hold_rank_buffer: int,
) -> list[str]:
    ranked = [
        sym
        for sym in score_row.index.tolist()
        if sym in prices and prices.get(sym, 0.0) > 0
    ]
    targets = ranked[:top_n]
    if hold_rank_buffer <= 0 or not current_positions:
        return targets

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
    return targets


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
) -> TargetWeightPlan:
    """Build a one-day target-weight rebalance plan.

    Scores and benchmark risk overlays always use the latest trading day before
    the planned trade day, matching the research backtest's no-lookahead rule.
    """
    from core.data_collector import DataCollector

    symbols = normalize_symbols(symbols)
    current_positions = _coerce_positions(positions)
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
    close_parts: list[pd.Series] = []
    valid_symbols: list[str] = []
    missing_symbols: list[str] = []
    for symbol in symbols:
        close = close_series_from_ohlcv(_fetch_korean_stock(collector, symbol, start, end))
        if close.empty:
            missing_symbols.append(symbol)
            continue
        close.name = symbol
        close_parts.append(close)
        valid_symbols.append(symbol)

    if not close_parts:
        raise ValueError("no valid price data for target-weight planning")

    close_panel = pd.concat(close_parts, axis=1).sort_index().ffill()
    close_panel = close_panel[~close_panel.index.duplicated(keep="last")]
    close_panel = close_panel[close_panel.index <= as_of_ts]
    if close_panel.empty:
        raise ValueError("no price rows on or before as_of_date")

    trade_day = pd.Timestamp(close_panel.index[-1]).normalize()
    price_row = close_panel.loc[trade_day]
    prices = {
        sym: float(price_row[sym])
        for sym in valid_symbols
        if sym in price_row.index and pd.notna(price_row[sym]) and float(price_row[sym]) > 0
    }
    if not prices:
        raise ValueError("no valid latest prices for target-weight planning")

    benchmark_symbol = str(params.get("benchmark_symbol", "KS11"))
    benchmark_close = close_series_from_ohlcv(
        _fetch_korean_stock(collector, benchmark_symbol, start, end)
    )
    benchmark_close = benchmark_close[benchmark_close.index <= trade_day]
    score_panel = _target_weight_score_panel(close_panel, benchmark_close, params)
    score_day = _score_date_before(score_panel.index, trade_day)

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
        )

    target_exposure = _target_exposure_for_day(trade_day, benchmark_close, params)
    base_target_exposure = max(0.0, min(float(params.get("target_exposure", 0.85)), 1.0))
    risk_off = target_exposure < base_target_exposure - 1e-9

    market_value_before = sum(
        pos["quantity"] * prices.get(symbol, 0.0)
        for symbol, pos in current_positions.items()
        if prices.get(symbol, 0.0) > 0
    )
    cash_before = float(cash)
    nav = cash_before + market_value_before
    if nav <= 0:
        raise ValueError("nav must be positive for target-weight planning")

    target_set = {sym for sym in targets if prices.get(sym, 0.0) > 0}
    per_target_value = nav * target_exposure / len(target_set) if target_set else 0.0
    tolerance = max(0.0, float(params.get("target_tolerance_pct", 0.0)) / 100.0)

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
        desired_qty[sym] = int(per_target_value // price) if sym in target_set else 0

    sell_orders: list[TargetWeightOrder] = []
    skipped_tolerance: list[str] = []
    for sym in sorted(desired_qty):
        current_qty = projected_qty.get(sym, 0)
        target_qty = desired_qty[sym]
        delta = target_qty - current_qty
        if delta >= 0:
            continue
        price = prices[sym]
        qty = abs(delta)
        notional = qty * price
        if notional / nav < tolerance:
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
                reason="target_weight_rebalance_sell",
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
    cash_after_estimate = cash_before + sum(o.notional for o in sell_orders) - sum(o.notional for o in buy_orders)
    gross_exposure_after = sum(
        qty * prices.get(sym, 0.0)
        for sym, qty in projected_qty.items()
        if qty > 0 and prices.get(sym, 0.0) > 0
    )
    target_position_count = sum(1 for qty in projected_qty.values() if qty > 0)

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
            "skipped_tolerance_symbols": skipped_tolerance,
            "buy_scale": round(buy_scale, 4),
            "benchmark_symbol": benchmark_symbol,
            "generated_at": datetime.now().isoformat(),
        },
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

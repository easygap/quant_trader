"""Separate, reproducible close valuations from dated intraday observations.

Only paper ledgers are finalized here. Historical rows lacking positions/quote
dates remain unverified; operating NAVs and their TWR chain are never rewritten.
"""
from datetime import datetime, timedelta
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DIRECT_CLOSE_METHOD = "observed_close_to_close_start_period_flows"


def _local(value):
    return value.astimezone(KST).replace(tzinfo=None) if value.tzinfo else value


def _close_lock():
    from core.runtime_lock import process_runtime_lock
    directory = Path(os.environ.get("QUANT_RUNTIME_LOCK_DIR", Path(__file__).resolve().parents[1] / "data"))
    return process_runtime_lock(directory / ".basket_close_collection.lock", label="바스켓 종가 관측")


def finalize_basket_closes(rebalancer, now=None):
    with _close_lock() as acquired:
        return _finalize_basket_closes(rebalancer, now) if acquired else []


def _finalize_basket_closes(rebalancer, now=None):
    from core.trading_hours import TradingHours
    from core.portfolio_manager import twr_period_return
    from database.models import TradeHistory, OperationEvent, get_session
    from database.repositories import (append_portfolio_valuation, get_portfolio_valuations,
                                       get_cash_flows)

    if rebalancer._ledger_mode() != "paper":
        return []
    now = _local(now or datetime.now(KST))
    calendar = TradingHours(rebalancer.config)
    key = rebalancer.account_key
    valuations = get_portfolio_valuations(key, "paper")
    closed = {v["date"][:10] for v in valuations if v["kind"] == "close"}
    candidates = {}
    for v in valuations:
        day = v["date"][:10]
        cutoff = datetime.combine(datetime.fromisoformat(day).date(), calendar.market_close)
        # Give the provider at least 30 minutes to finalize today's daily bar.
        if (v["kind"] == "intraday" and day not in closed and now >= cutoff + timedelta(minutes=30)
                and calendar.is_trading_day(cutoff)
                and datetime.fromisoformat(v["measured_at"]) <= cutoff):
            candidates[day] = v
    flows = get_cash_flows(key, "paper")
    saved = []
    for day, baseline in sorted(candidates.items()):
        boundary = datetime.fromisoformat(baseline["measured_at"])
        cutoff = datetime.combine(boundary.date(), calendar.market_close)
        context = baseline["context"]
        if "positions" not in context:
            continue
        quantities = {p["symbol"]: int(p["quantity"]) for p in context["positions"]}
        cash = float(baseline["cash"])
        session = get_session()
        try:
            trades = session.query(TradeHistory).filter(
                TradeHistory.account_key == key, TradeHistory.mode == "paper",
                TradeHistory.executed_at > boundary, TradeHistory.executed_at <= cutoff,
            ).order_by(TradeHistory.executed_at, TradeHistory.id).all()
        finally:
            session.close()
        for t in trades:
            if t.action not in ("BUY", "SELL"):
                raise ValueError(f"Unknown trade action: {t.action}")
            sign = 1 if t.action == "BUY" else -1
            quantities[t.symbol] = quantities.get(t.symbol, 0) + sign * t.quantity
            # Slippage is already in the execution amount; do not charge it twice.
            cash -= sign * t.total_amount + float(t.commission or 0) + float(t.tax or 0)
        flow = sum(amount for at, amount in flows if boundary < at <= cutoff)
        cash += flow
        if any(q < 0 for q in quantities.values()) or cash < -1:
            raise ValueError(f"Inconsistent close ledger: {key} {day}")
        holdings = []
        for symbol, quantity in quantities.items():
            if quantity == 0:
                continue
            df = rebalancer.data_collector.fetch_korean_stock(symbol, day, day)
            if df is None or df.empty or "close" not in df:
                break
            dates = df["date"] if "date" in df else df.index
            matches = [i for i, d in enumerate(dates) if str(d)[:10] == day]
            if len(matches) != 1:
                break
            price = float(df["close"].iloc[matches[0]])
            if not math.isfinite(price) or price <= 0:
                break
            holdings.append({"symbol": symbol, "quantity": quantity, "price": price,
                             "quote_date": day, "source": rebalancer.data_collector.get_last_source_info().get("source") or "unknown",
                             "observed_at": now.isoformat()})
        else:
            total = cash + sum(p["quantity"] * p["price"] for p in holdings)
            if baseline["total_value"] + flow <= 0:
                raise ValueError(f"Close return has no funded baseline: {key} {day}")
            index = (1 + baseline["cumulative_return"] / 100) * (1 + twr_period_return(baseline["total_value"], total, flow))
            prior = [v for v in valuations if v["kind"] == "close" and v["date"][:10] < day]
            # Once direct observations start at index=1, never splice the old
            # operating intraday index back into that prospective close series.
            direct_chain = prior and prior[-1]["context"].get("return_method") == DIRECT_CLOSE_METHOD
            if direct_chain:
                prior_close = prior[-1]
                flow = sum(amount for at, amount in flows
                           if datetime.fromisoformat(prior_close["measured_at"]) < at <= cutoff)
                if prior_close["total_value"] + flow <= 0:
                    raise ValueError("Close return has no funded previous close")
                index = (1 + prior_close["cumulative_return"] / 100) * total / (prior_close["total_value"] + flow)
            prior_index = 1 + prior[-1]["cumulative_return"] / 100 if prior else None
            daily = (index / prior_index - 1) * 100 if prior_index and prior_index > 0 else 0.0
            peak = max([1.0, index] + [1 + v["cumulative_return"] / 100 for v in prior])
            close_context = {"valuation_kind": "close", "positions": holdings, "quote_dates": [day],
                             "baseline_valuation_id": baseline["id"], "baseline_measured_at": boundary.isoformat(),
                             "trade_ids": [t.id for t in trades], "flow_since": flow,
                             "return_method": "linked_measurements_start_period_flows",
                             "daily_return_available": prior_index is not None}
            if direct_chain:
                with get_session() as session:
                    cutoff_trade_ids = [row[0] for row in session.query(TradeHistory.id).filter(
                        TradeHistory.account_key == key, TradeHistory.mode == "paper",
                        TradeHistory.executed_at <= cutoff,
                    ).order_by(TradeHistory.executed_at, TradeHistory.id).all()]
                close_context.update(return_method=DIRECT_CLOSE_METHOD, first_close_in_series=False,
                                     inventory_baseline_valuation_id=baseline["id"],
                                     baseline_valuation_id=prior_close["id"], baseline_measured_at=prior_close["measured_at"],
                                     ledger_cutoff_at=cutoff.isoformat(), flow_since=flow, trade_ids=cutoff_trade_ids,
                                     initial_capital=float(rebalancer.portfolio_mgr.initial_capital), observation_only=True)
            append_portfolio_valuation(account_key=key, mode="paper", day=day, measured_at=cutoff,
                                       total_value=total, cash=cash, cumulative_return=(index - 1) * 100,
                                       daily_return=daily, mdd=(1 - index / peak) * 100, context=close_context)
            saved.append(day)
            valuations = get_portfolio_valuations(key, "paper")
            continue
        # A stale/missing quote is visible even though no false close was stored.
        from core.cycle_observability import record_cycle_event
        with get_session() as session:
            seen = session.query(OperationEvent.id).filter_by(
                event_type="CLOSE_VALUATION_MISSING_PRICE", strategy=key, mode="paper", detail=f"{day}:{symbol}",
            ).first()
        if not seen:
            record_cycle_event("CLOSE_VALUATION_MISSING_PRICE", f"종가 평가 보류: {key} {day} {symbol} 당일 가격 미확인",
                               strategy=key, mode="paper", severity="warning", detail=f"{day}:{symbol}")
    return saved


def _finite(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean ledger number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("invalid ledger number")
    return number


def _close_coverage(config, baskets, day):
    """Read presence only; never initialize a database or claim source validation."""
    expected = [name for name, basket in baskets.items() if basket.get("enabled") is True]
    result = {"session": day, "expected": len(expected), "present": 0, "missing": len(expected),
              "verification": "presence_only", "baskets": [
                  {"basket": name, "status": "missing", "close_count": 0, "latest_close_session": None}
                  for name in expected]}
    path = Path(config.database.get("sqlite_path", "data/quant_trader.db"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if config.database.get("type") == "postgresql" or not path.is_file():
        return {**result, "status": "unavailable", "reason": "close_ledger_missing"}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA query_only=ON")
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_valuations'").fetchone()
            if not table:
                return {**result, "status": "missing", "reason": "close_schema_missing"}
            for item in result["baskets"]:
                key = f"basket_rebalance:{item['basket']}"
                count, latest = connection.execute(
                    "SELECT SUM(CASE WHEN substr(date,1,10)=? THEN 1 ELSE 0 END), MAX(substr(date,1,10)) "
                    "FROM portfolio_valuations WHERE account_key=? AND mode='paper' AND kind='close'",
                    (day, key),
                ).fetchone()
                item.update(close_count=int(count or 0), latest_close_session=latest,
                            status="present" if count else "missing")
        result["present"] = sum(item["status"] == "present" for item in result["baskets"])
        result["missing"] = result["expected"] - result["present"]
        return {**result, "status": "complete" if not result["missing"] else "missing"}
    except sqlite3.Error:
        return {**result, "status": "unavailable", "reason": "close_ledger_read_failed"}


def _collect_one_close(name, basket, config, collector, now, cutoff):
    """Observe today's close using the actual paper trade/flow ledger at cutoff."""
    from database.models import TradeHistory, Position, CashFlow, get_session
    from database.repositories import append_portfolio_valuation, get_portfolio_valuations

    key, day = f"basket_rebalance:{name}", cutoff.date().isoformat()
    valuations = get_portfolio_valuations(key, "paper", kind="close")
    if any(row["date"][:10] == day for row in valuations):
        return {"basket": name, "status": "already_recorded", "day": day, "saved": False}
    with get_session() as session:
        trades = session.query(TradeHistory).filter_by(account_key=key, mode="paper").order_by(TradeHistory.executed_at, TradeHistory.id).all()
        flows = session.query(CashFlow).filter_by(account_key=key, mode="paper").all()
        positions = session.query(Position).filter_by(account_key=key, mode="paper").all()
    initial = _finite(basket.get("initial_capital", config.risk_params.get("position_sizing", {}).get("initial_capital", 10000000)), positive=True)
    cash, quantities, current_quantities, trade_ids = initial, {}, {}, []
    for trade in trades:
        if trade.action not in {"BUY", "SELL"} or trade.executed_at > now:
            raise ValueError("invalid trade chronology or side")
        quantity = _finite(trade.quantity, positive=True)
        if int(quantity) != quantity:
            raise ValueError("noninteger ledger quantity")
        sign = 1 if trade.action == "BUY" else -1
        current_quantities[trade.symbol] = current_quantities.get(trade.symbol, 0) + sign * int(quantity)
        if trade.executed_at > cutoff:
            continue
        amount, commission, tax = (_finite(getattr(trade, field)) for field in ("total_amount", "commission", "tax"))
        if amount <= 0 or min(commission, tax) < 0:
            raise ValueError("negative trade amount or costs")
        quantities[trade.symbol] = quantities.get(trade.symbol, 0) + sign * int(quantity)
        cash -= sign * amount + commission + tax
        trade_ids.append(trade.id)
    held = {}
    for position in positions:
        quantity = _finite(position.quantity)
        if quantity < 0 or int(quantity) != quantity or position.symbol in held:
            raise ValueError("invalid current position quantity")
        if quantity:
            held[position.symbol] = int(quantity)
    if held != {symbol: q for symbol, q in current_quantities.items() if q}:
        raise ValueError("paper position and trade ledger disagree")
    valid_flows = [(flow.occurred_at, _finite(flow.amount)) for flow in flows]
    if any(at > now for at, _ in valid_flows):
        raise ValueError("cash flow in the future")
    cash += sum(amount for at, amount in valid_flows if at <= cutoff)
    if cash < -1 or any(q < 0 for q in quantities.values()):
        raise ValueError("invalid cutoff cash or inventory")
    holdings = []
    for symbol, quantity in sorted(quantities.items()):
        if not quantity:
            continue
        frame = collector.fetch_korean_stock(symbol, day, day)
        if frame is None or frame.empty or "close" not in frame:
            raise ValueError("exact session close missing")
        dates = frame["date"] if "date" in frame else frame.index
        matches = [i for i, value in enumerate(dates) if str(value)[:10] == day]
        if len(matches) != 1:
            raise ValueError("exact session close missing or duplicated")
        source_info = collector.get_last_source_info()
        source = (source_info.get("history") or {}).get(symbol) or source_info.get("source")
        if not isinstance(source, str) or not source or source == "unknown":
            raise ValueError("close source unavailable")
        holdings.append({"symbol": symbol, "quantity": quantity,
                         "price": _finite(frame["close"].iloc[matches[0]], positive=True),
                         "quote_date": day, "source": source, "observed_at": now.isoformat()})
    total = cash + sum(row["quantity"] * row["price"] for row in holdings)
    _finite(total, positive=True)
    prior = [row for row in valuations if row["date"][:10] < day]
    baseline = prior[-1] if prior else None
    boundary = datetime.fromisoformat(baseline["measured_at"]) if baseline else cutoff
    flow = sum(amount for at, amount in valid_flows if boundary < at <= cutoff)
    if baseline:
        denominator = _finite(baseline["total_value"], positive=True) + flow
        if denominator <= 0:
            raise ValueError("close interval has no funded baseline")
        previous_index = 1 + _finite(baseline["cumulative_return"]) / 100
        _finite(previous_index, positive=True)
        index = previous_index * total / denominator
        daily = (index / previous_index - 1) * 100
    else:
        index, daily = 1.0, 0.0  # First observation anchors a series, not historical profit.
    peak = max([1.0, index] + [1 + row["cumulative_return"] / 100 for row in prior])
    context = {"valuation_kind": "close", "positions": holdings, "quote_dates": [day],
               "return_method": DIRECT_CLOSE_METHOD, "first_close_in_series": baseline is None,
               "baseline_valuation_id": baseline["id"] if baseline else None,
               "baseline_measured_at": boundary.isoformat(), "ledger_cutoff_at": cutoff.isoformat(),
               "flow_since": flow, "trade_ids": trade_ids, "initial_capital": initial,
               "daily_return_available": baseline is not None, "observation_only": True}
    append_portfolio_valuation(account_key=key, mode="paper", day=day, measured_at=cutoff,
                               total_value=total, cash=cash, cumulative_return=(index - 1) * 100,
                               daily_return=daily, mdd=(1 - index / peak) * 100, context=context)
    return {"basket": name, "status": "recorded", "day": day, "saved": True,
            "total_value": total, "positions": len(holdings), "first_close_in_series": baseline is None}


def collect_basket_closes(config=None, *, now=None, collector_factory=None):
    """After-close observation only; before settlement inspect presence read-only.

    This intentionally never invents yesterday's inventory from today's holdings.
    Existing finalize_basket_closes may recover dated genuine intraday baselines.
    """
    from config.config_loader import Config
    from core.trading_hours import TradingHours
    config = config or Config.get()
    observed = _local(now or datetime.now(KST))
    calendar = TradingHours(config)
    cutoff = datetime.combine(observed.date(), calendar.market_close)
    ready = calendar.is_trading_day(observed) and observed >= cutoff + timedelta(minutes=30)
    completed = observed if ready else observed - timedelta(days=1)
    while not calendar.is_trading_day(completed):
        completed -= timedelta(days=1)
    completed_day = completed.date().isoformat()
    report = {"schema_version": 1, "observed_at": observed.isoformat(), "day": observed.date().isoformat(),
              "expected_session": completed_day, "last_completed_session": completed_day,
              "observation_only": True, "orders_submitted": 0, "rows_saved": 0, "baskets": []}
    if str(config.trading.get("mode", "paper")).lower() == "live":
        return {**report, "status": "disabled", "reason": "paper_ledgers_only"}
    from core.basket_rebalancer import BasketRebalancer
    baskets = BasketRebalancer._load_baskets_config()
    report["coverage"] = _close_coverage(config, baskets, completed_day)
    if not ready:
        missing = report["coverage"]["status"] != "complete"
        return {**report, "status": "previous_close_missing" if missing else "awaiting_close",
                "reason": "last_completed_session_close_missing" if missing else "same_day_final_close_not_available_yet"}
    from core.data_collector import DataCollector
    factory = collector_factory or (lambda: DataCollector(config))
    with _close_lock() as acquired:
        if not acquired:
            return {**report, "status": "busy", "reason": "close_collection_already_running"}
        for name, basket in baskets.items():
            if basket.get("enabled") is not True:
                continue
            try:
                result = _collect_one_close(name, basket, config, factory(), observed, cutoff)
            except Exception as exc:
                result = {"basket": name, "status": "unavailable", "saved": False,
                          "reason": type(exc).__name__}
            report["baskets"].append(result)
    report["rows_saved"] = sum(row["saved"] for row in report["baskets"])
    successes = sum(row["status"] in {"recorded", "already_recorded"} for row in report["baskets"])
    report["status"] = "completed" if report["baskets"] and successes == len(report["baskets"]) else ("partial" if successes else "unavailable")
    report["coverage"] = _close_coverage(config, baskets, completed_day)
    return report

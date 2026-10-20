"""Read-only basket diagnostics. Historical evidence is not order authorization."""
from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta


def snapshot_entry_status(snapshot, basket, risk_params):
    """Report risk blocks visible in the last measurement, never promise a fill."""
    result = {
        "status": "RECHECK_REQUIRED", "allowed": None, "reasons": [],
        "basis": "latest_snapshot", "measured_at": None,
        "note": "주문 직전 가격·잔고·거래 시간과 전체 한도를 다시 확인합니다.",
    }
    if not snapshot:
        result["note"] = "평가 기록 없음 — 주문 가능 여부 미확인"
        return result
    result["measured_at"] = str(snapshot.get("created_at") or snapshot.get("date") or "")
    # An explicit basket drawdown rule owns both MDD and daily-loss policy.
    from core.basket_risk import delegates_account_drawdown_guard

    delegated = delegates_account_drawdown_guard(basket or {})
    limits = risk_params.get("drawdown") or {}
    if not delegated:
        for field, limit_field, label in (
            ("mdd", "max_portfolio_mdd", "낙폭"),
            ("daily_return", "max_daily_loss", "일일 손실"),
        ):
            try:
                value = float(snapshot.get(field))
                limit = float(limits.get(limit_field) or 0) * 100
                loss = abs(value) if field == "mdd" else -value
                if not math.isfinite(value) or not math.isfinite(limit):
                    raise ValueError("nonfinite")
                if limit > 0 and loss >= limit:
                    result["reasons"].append(f"최근 평가 기준 {label} {loss:.2f}% ≥ 한도 {limit:.2f}%")
            except (TypeError, ValueError):
                result["note"] = "손실 한도 자료 미확인 — 주문 직전 재검사 필요"
    if result["reasons"]:
        result.update(status="BLOCKED", allowed=False)
    return result


def allocation_diagnostics(total, cash, positions, targets, defensive_symbol=None, tolerance=0.08):
    """positions contain measured market values, never acquisition-cost weights."""
    if not math.isfinite(float(total or 0)) or total <= 0:
        return {"status": "UNKNOWN", "assets": [], "issues": ["시장가격 평가 자료 없음"]}
    try:
        measured = {p["symbol"]: float(p["quantity"]) * float(p["price"]) for p in positions}
    except (TypeError, ValueError, KeyError):
        return {"status": "UNKNOWN", "assets": [], "issues": ["종목별 시장가격 미확인"]}
    if not all(math.isfinite(v) and v >= 0 for v in measured.values()):
        return {"status": "UNKNOWN", "assets": [], "issues": ["종목별 평가금액 확인 필요"]}
    if abs(sum(measured.values()) + cash - total) > max(1.0, total * 1e-6):
        return {"status": "UNKNOWN", "assets": [], "issues": ["종목별 평가금액과 총평가액 불일치"]}
    assets = []
    groups = [("equity", "주식", [s for s in set(measured) | set(targets) if s != defensive_symbol])]
    if defensive_symbol:
        groups.append(("defensive", "CD·방어 자산", [defensive_symbol]))
    for key, label, symbols in groups:
        actual = sum(measured.get(s, 0) for s in symbols) / total
        target = sum(targets.get(s, 0) for s in symbols)
        assets.append({"asset": key, "label": label, "actual": actual, "target": target, "gap": actual - target})
    target_cash = max(0.0, 1 - sum(targets.values()))
    assets.append({"asset": "cash", "label": "현금", "actual": cash / total, "target": target_cash, "gap": cash / total - target_cash})
    issues = [f"{a['label']} 비중 {a['actual']:.1%}, 목표 {a['target']:.1%} ({a['gap'] * 100:+.1f}%p)"
              for a in assets if abs(a["gap"]) > tolerance]
    return {"status": "ATTENTION" if issues else "OK", "assets": assets, "issues": issues}


def monthly_contribution_status(plan, flows, track_start, today):
    """Closed calendar months only; partial deposits do not reset the schedule.

    The opening month is funded by initial capital. The current month remains
    pending unless an explicit due_day has elapsed. No synthetic deposit is made.
    """
    result = {"status": "OK", "months": [], "missing_amount": 0.0, "issues": []}
    if not plan.get("enabled") or plan.get("cadence", "monthly") != "monthly" or not track_start:
        return result
    amount = float(plan.get("amount") or 0)
    if amount <= 0:
        return result
    start = date.fromisoformat(str(track_start)[:10])
    today = date.fromisoformat(str(today)[:10])
    month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    paid = {}
    for flow in flows:
        occurred = str((flow.get("occurred_at") if isinstance(flow, dict) else flow[0]) or "")
        value = float((flow.get("amount") if isinstance(flow, dict) else flow[1]) or 0)
        if occurred[:10] <= str(today) and value > 0:
            paid[occurred[:7]] = paid.get(occurred[:7], 0) + value
    while month <= today.replace(day=1):
        due_day = plan.get("due_day")
        due = month < today.replace(day=1)
        if due_day is not None:
            due = today >= month.replace(day=min(max(int(due_day), 1), calendar.monthrange(month.year, month.month)[1]))
        key = month.strftime("%Y-%m")
        shortfall = max(0.0, amount - paid.get(key, 0))
        result["months"].append({"month": key, "planned": amount, "recorded": paid.get(key, 0), "due": due, "shortfall": shortfall})
        if due and shortfall:
            result["status"] = "ATTENTION"
            result["missing_amount"] += shortfall
            result["issues"].append(f"{key} 적립 기록 {paid.get(key, 0):,.0f}/{amount:,.0f}원 — {shortfall:,.0f}원 확인 필요")
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def valuation_context(market_snapshot, positions, measured_at, valuation_date, market_close):
    """A daily bar becomes a close only after the session and on its own date."""
    holdings = []
    for p in positions:
        quote = market_snapshot.get(p.symbol) or {}
        holdings.append({"symbol": p.symbol, "quantity": p.quantity, "price": quote.get("price"),
                         "quote_date": quote.get("quote_date"), "source": quote.get("source", "unknown"),
                         "observed_at": quote.get("observed_at")})
    dates = {p["quote_date"] for p in holdings}
    day = str(valuation_date)[:10]
    same_date = dates == {day} or not holdings
    kind = "unverified"
    if same_date and day == str(measured_at.date()):
        if measured_at.time() < market_close:
            kind = "intraday"
        elif all(p["observed_at"] and datetime.fromisoformat(p["observed_at"]).time() >= market_close for p in holdings):
            kind = "close"
    return {"valuation_kind": kind, "positions": holdings, "quote_dates": sorted(str(d) for d in dates),
            "measured_at": measured_at.isoformat(), "valuation_date": day}


def read_basket_diagnostics(basket, account_key, risk_params, mode="paper", today=None):
    """Shared dashboard/health view; DB reads only, no broker/price requests."""
    from database.models import PortfolioSnapshot, get_session
    from database.repositories import get_cash_flows, get_portfolio_valuations
    from zoneinfo import ZoneInfo

    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    session = get_session()
    try:
        rows = session.query(PortfolioSnapshot).filter_by(account_key=account_key, mode=mode).order_by(PortfolioSnapshot.date).all()
        latest = rows[-1] if rows else None
        snap = {k: getattr(latest, k) for k in ("date", "created_at", "total_value", "cash", "mdd", "daily_return")} if latest else None
        first = rows[0].date if rows else None
    finally:
        session.close()
    valuations = get_portfolio_valuations(account_key, mode)
    matched = [v for v in valuations if snap and snap["created_at"] and v["measured_at"] == snap["created_at"].isoformat()
               and v["total_value"] == snap["total_value"] and v["cash"] == snap["cash"]]
    latest_measurement = matched[-1] if matched else None
    context = (latest_measurement or {}).get("context") or {}
    closes = [v for v in valuations if v["kind"] == "close"]
    return {
        "entry_status": snapshot_entry_status(snap, basket, risk_params),
        "allocation": context.get("allocation") or {"status": "UNKNOWN", "assets": [], "issues": ["종목별 시장가격 측정 기록 대기"]},
        "plan_diagnostics": context.get("plan_diagnostics") or [],
        "measurement": {"kind": (latest_measurement or {}).get("kind", "unverified"),
                        "measured_at": str(snap["created_at"]) if snap else None,
                        "quote_dates": context.get("quote_dates") or [], "positions": context.get("positions") or []},
        "latest_close": closes[-1] if closes else None,
        "contribution_status": monthly_contribution_status(basket.get("contribution_plan") or {}, get_cash_flows(account_key, mode), first, today),
    }

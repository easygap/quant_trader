"""결측 NAV 스냅샷 복원 — 원장 재생 + 과거 시세로 빠진 날을 되살린다.

왜 필요한가:
일일 사이클이 하루 안 돌면 그 영업일 스냅샷은 영구 결측이 된다. `_nav_attribution_date`가
'오늘이 거래일이면 오늘'로 귀속하기 때문에, 다음날 실행은 어제를 메우지 못하고 자기 날짜로만
저장한다(2026-08-18 결측이 8/19~8/26 내내 그대로 남은 이유). 승격 게이트의 스냅샷 커버리지
기준이 95%라 결측 며칠이 판정을 뒤집는다.

무엇을 복원할 수 있고 무엇은 못 하나:
  포지션·현금  원장(trade_history + cash_flows) 재생으로 **정확히** 복원된다.
               실측 81일치를 재구성해 대조한 결과 현금은 전부 일치했다.
  NAV          정확히는 복원 못 한다. 기존 스냅샷은 10:07 장중 마크인데 그 시점의 틱은
               남아 있지 않다. 일별 OHLC로 근사할 뿐이다. 실측 대비 오차는
               시가+종가 중간값 기준 평균 0.45~0.53%, 최대 1.9%였다(시가 0.6%, 종가 0.8%).
               그래서 중간값을 쓴다.

그래서 복원분은 `reconstructed=True`로 표시하고 평가가 실측과 나눠 표기한다.
커버리지 95% 게이트는 '시스템이 실제로 돌았는가'를 보는 장치이므로, 보정분이 실측인 척하면
게이트가 목적을 잃는다. 채우되, 무엇이 채워진 것인지 항상 보이게 한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger


def _as_date(v: Any) -> date:
    return v.date() if isinstance(v, datetime) else v


def replay_ledger(
    account_key: str,
    as_of: date,
    initial_capital: float,
    mode: str = "paper",
) -> dict[str, Any]:
    """as_of 장마감 시점의 포지션·현금·투자원금을 원장 재생으로 구한다.

    반환: {"positions": {symbol: qty}, "cost_basis": {symbol: 원금}, "cash": float}
    거래가 없던 구간이면 앞뒤 스냅샷과 정확히 일치한다(검증됨).
    """
    from database.models import CashFlow, TradeHistory, get_session

    boundary = datetime.combine(_as_date(as_of), datetime.max.time())
    session = get_session()
    try:
        trades = (
            session.query(TradeHistory)
            .filter(
                TradeHistory.mode == mode,
                TradeHistory.account_key == account_key,
                TradeHistory.executed_at <= boundary,
            )
            .order_by(TradeHistory.id.asc())
            .all()
        )
        flows = (
            session.query(CashFlow)
            .filter(
                CashFlow.mode == mode,
                CashFlow.account_key == account_key,
                CashFlow.occurred_at <= boundary,
            )
            .all()
        )
        rows = [
            (t.symbol, str(t.action).upper(), int(t.quantity or 0),
             float(t.total_amount or 0), float(t.commission or 0) + float(t.tax or 0))
            for t in trades
        ]
        flow_total = sum(float(f.amount or 0) for f in flows)
    finally:
        session.close()

    cash = float(initial_capital) + flow_total
    qty: dict[str, int] = {}
    cost: dict[str, float] = {}
    for symbol, action, q, amount, fee in rows:
        if action == "BUY":
            qty[symbol] = qty.get(symbol, 0) + q
            cost[symbol] = cost.get(symbol, 0.0) + amount
            cash -= amount + fee
        else:
            held = qty.get(symbol, 0)
            # 매도분만큼 원가를 비례 차감한다(평균단가 유지).
            if held > 0:
                cost[symbol] = cost.get(symbol, 0.0) * max(0, held - q) / held
            qty[symbol] = held - q
            cash += amount - fee

    positions = {s: q for s, q in qty.items() if q > 0}
    return {
        "positions": positions,
        "cost_basis": {s: round(cost.get(s, 0.0), 0) for s in positions},
        "cash": cash,
    }


def historical_mark(symbol: str, day: date, collector: Any = None) -> float | None:
    """그날의 평가 기준가 — 시가와 종가의 중간값.

    기존 스냅샷이 10:07 장중 마크라 종가로 재구성하면 계통 오차가 커진다(평균 0.8%).
    중간값이 실측에 가장 가까웠다(평균 0.45~0.53%). 해당 일자 데이터가 없으면 None.
    """
    try:
        import FinanceDataReader as fdr
        import pandas as pd

        target = _as_date(day)
        df = fdr.DataReader(
            symbol,
            (target - timedelta(days=10)).isoformat(),
            (target + timedelta(days=1)).isoformat(),
        )
        if df is None or df.empty:
            return None
        stamp = pd.Timestamp(target)
        if stamp not in df.index:
            return None  # 그날 거래 데이터 없음 — 근사 금지(휴장·상폐 등)
        row = df.loc[stamp]
        o, c = float(row.get("Open", 0) or 0), float(row.get("Close", 0) or 0)
        if o > 0 and c > 0:
            return (o + c) / 2
        return c or o or None
    except Exception as exc:
        logger.debug("과거 시세 조회 실패 {} {}: {}", symbol, day, exc)
        return None


def find_missing_trading_days(
    config: Any,
    account_key: str,
    since: date,
    until: date,
    mode: str = "paper",
) -> list[date]:
    """since~until 사이 영업일 중 이 계정 스냅샷이 없는 날."""
    from core.trading_hours import TradingHours
    from database.models import PortfolioSnapshot, get_session

    session = get_session()
    try:
        have = {
            _as_date(r[0])
            for r in session.query(PortfolioSnapshot.date)
            .filter(
                PortfolioSnapshot.mode == mode,
                PortfolioSnapshot.account_key == account_key,
            )
            .all()
        }
    finally:
        session.close()

    th = TradingHours(config)
    out: list[date] = []
    d = _as_date(since)
    end = _as_date(until)
    while d <= end:
        if th.is_trading_day(d) and d not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


def reconstruct_snapshot(
    config: Any,
    account_key: str,
    day: date,
    initial_capital: float,
    mode: str = "paper",
) -> dict[str, Any] | None:
    """그날의 스냅샷 값을 계산한다. 가격을 못 구하면 None(억지로 채우지 않는다).

    반환: save_portfolio_snapshot에 넘길 수 있는 dict.
    """
    from core.portfolio_manager import twr_period_return
    from database.models import CashFlow, PortfolioSnapshot, get_session

    state = replay_ledger(account_key, day, initial_capital, mode=mode)
    positions, cash = state["positions"], state["cash"]

    stock_value = 0.0
    for symbol, qty in positions.items():
        mark = historical_mark(symbol, day)
        if mark is None:
            logger.warning(
                "복원 보류 {} {} — {} 시세 없음 (가짜 NAV 방지)", account_key, day, symbol,
            )
            return None
        stock_value += mark * qty

    total_value = stock_value + cash
    if total_value <= 0:
        return None

    boundary = datetime.combine(_as_date(day), datetime.max.time())
    session = get_session()
    try:
        prev = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.mode == mode,
                PortfolioSnapshot.account_key == account_key,
                PortfolioSnapshot.date < datetime.combine(_as_date(day), datetime.min.time()),
            )
            .order_by(PortfolioSnapshot.date.desc())
            .first()
        )
        prev_val = float(prev.total_value) if prev else None
        prev_cum = float(prev.cumulative_return or 0.0) if prev else None
        prev_peak = float(prev.peak_value or 0.0) if prev else 0.0
        prev_date = _as_date(prev.date) if prev else None
        flows_before = sum(
            float(f.amount or 0)
            for f in session.query(CashFlow).filter(
                CashFlow.mode == mode,
                CashFlow.account_key == account_key,
                CashFlow.occurred_at <= boundary,
            ).all()
        )
        flow_between = 0.0
        if prev_date is not None:
            flow_between = sum(
                float(f.amount or 0)
                for f in session.query(CashFlow).filter(
                    CashFlow.mode == mode,
                    CashFlow.account_key == account_key,
                    CashFlow.occurred_at > datetime.combine(prev_date, datetime.max.time()),
                    CashFlow.occurred_at <= boundary,
                ).all()
            )
    finally:
        session.close()

    # 누적수익률: 계정에 외부 흐름이 없으면 기존 산식 그대로(하위 호환),
    # 있으면 직전 스냅샷에 구간 TWR을 연결한다 — PortfolioManager와 같은 규칙.
    if flows_before == 0:
        cumulative = ((total_value / initial_capital) - 1) * 100 if initial_capital > 0 else 0.0
    elif prev_val is not None and prev_val > 0:
        r = twr_period_return(prev_val, total_value, flow_between)
        cumulative = ((1 + (prev_cum or 0.0) / 100) * (1 + r) - 1) * 100
    else:
        cumulative = twr_period_return(initial_capital, total_value, flows_before) * 100

    daily_return = 0.0
    if prev_val is not None and prev_val > 0:
        denom = prev_val + flow_between
        if denom > 0:
            daily_return = (total_value / denom - 1) * 100

    peak = max(prev_peak, total_value, float(initial_capital))
    mdd = ((peak - total_value) / peak) * 100 if peak > 0 else 0.0

    return {
        "total_value": round(total_value, 0),
        "cash": round(cash, 0),
        "invested": round(sum(state["cost_basis"].values()), 0),
        "daily_return": round(daily_return, 4),
        "cumulative_return": round(cumulative, 4),
        "mdd": round(mdd, 4),
        "peak_value": round(peak, 0),
        "position_count": len(positions),
        "snapshot_date": datetime.combine(_as_date(day), datetime.min.time()),
    }


def backfill_account(
    config: Any,
    account_key: str,
    initial_capital: float,
    since: date,
    until: date,
    mode: str = "paper",
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """결측 영업일을 찾아 복원한다. 반환: 채운(또는 채울) 날의 요약 목록."""
    from database.repositories import save_portfolio_snapshot

    missing = find_missing_trading_days(config, account_key, since, until, mode=mode)
    if not missing:
        return []

    filled: list[dict[str, Any]] = []
    for day in missing:
        snap = reconstruct_snapshot(config, account_key, day, initial_capital, mode=mode)
        if snap is None:
            continue
        if not dry_run:
            ok = save_portfolio_snapshot(
                account_key=account_key, mode=mode, reconstructed=True, **snap,
            )
            if not ok:
                logger.warning("복원 스냅샷 저장 실패 {} {}", account_key, day)
                continue
        filled.append({"date": day, **snap})
        logger.info(
            "결측 복원{} {} {} — NAV {:,.0f}원 (원장 재생 + 당일 시가·종가 중간값)",
            " (DRY RUN)" if dry_run else "", account_key, day, snap["total_value"],
        )
    return filled

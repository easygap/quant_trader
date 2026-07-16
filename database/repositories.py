"""
데이터 접근 계층 (Repository)
- DB CRUD 함수 제공
- 주가 데이터, 매매 기록, 포지션, 포트폴리오 스냅샷 관리
- 모든 함수에 @with_retry 적용 — WAL 체크포인트 중 일시적 locked에도 안전
"""

import json
import re
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy import and_, text
from sqlalchemy.exc import IntegrityError
from loguru import logger

from database.models import (
    get_session, with_retry, StockPrice, TradeHistory,
    Position, PortfolioSnapshot, DailyReport, FailedOrder,
    PendingOrderGuard, OrderRecord as DbOrderRecord, CashFlow,
    OperationEvent,
)


TRADING_HALT_SET = "TRADING_HALT_SET"
TRADING_HALT_CLEARED = "TRADING_HALT_CLEARED"
TRADING_HALT_STRATEGY = "global_trading_halt"
_TRADING_HALT_ADVISORY_LOCK_KEY = 0x51484C54  # "QHLT"


class TradingHaltStateConflict(RuntimeError):
    """HALT 해제 기준 이벤트가 이미 최신 상태가 아닐 때 발생한다."""


# =============================================================
# 주가 데이터 관련
# =============================================================

@with_retry
def save_stock_prices(symbol: str, df: pd.DataFrame):
    """
    주가 데이터를 DB에 저장 (중복 무시)

    Args:
        symbol: 종목 코드
        df: OHLCV 데이터프레임 (컬럼: date, open, high, low, close, volume)
    """
    session = get_session()
    try:
        saved_count = 0
        for _, row in df.iterrows():
            # 기존 데이터 확인 (중복 방지)
            existing = session.query(StockPrice).filter(
                and_(StockPrice.symbol == symbol, StockPrice.date == row["date"])
            ).first()

            if existing is None:
                price = StockPrice(
                    symbol=symbol,
                    date=row["date"],
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=int(row["volume"]),
                )
                session.add(price)
                saved_count += 1

        session.commit()
        logger.info("종목 {} 주가 데이터 {}건 저장 완료", symbol, saved_count)
    except Exception as e:
        session.rollback()
        logger.error("종목 {} 주가 데이터 저장 실패: {}", symbol, e)
        raise
    finally:
        session.close()


@with_retry
def get_stock_prices(
    symbol: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    종목의 주가 데이터를 DataFrame으로 반환

    Args:
        symbol: 종목 코드
        start_date: 시작일 (없으면 전체)
        end_date: 종료일 (없으면 오늘까지)

    Returns:
        OHLCV 데이터프레임
    """
    session = get_session()
    try:
        query = session.query(StockPrice).filter(StockPrice.symbol == symbol)

        if start_date:
            query = query.filter(StockPrice.date >= start_date)
        if end_date:
            query = query.filter(StockPrice.date <= end_date)

        query = query.order_by(StockPrice.date)
        results = query.all()

        if not results:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        data = [{
            "date": r.date,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        } for r in results]

        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        return df
    finally:
        session.close()


# =============================================================
# 매매 기록 관련
# =============================================================

@with_retry
def save_trade(
    symbol: str,
    action: str,
    price: float,
    quantity: int,
    commission: float = 0,
    tax: float = 0,
    slippage: float = 0,
    strategy: str = "",
    signal_score: float = 0,
    reason: str = "",
    mode: str = "paper",
    account_key: str = "",
    signal_at: datetime = None,
    order_at: datetime = None,
    expected_price: Optional[float] = None,
    actual_slippage_pct: Optional[float] = None,
    execution_session_id: str = "",
    order_id: str = "",
) -> TradeHistory:
    """매매 기록 저장. signal_at/order_at/expected_price는 paper monitoring용."""
    price_gap = round(price - expected_price, 1) if expected_price is not None else None
    session = get_session()
    try:
        trade = TradeHistory(
            account_key=account_key or "",
            symbol=symbol,
            action=action,
            price=price,
            quantity=quantity,
            total_amount=price * quantity,
            commission=commission,
            tax=tax,
            slippage=slippage,
            expected_price=expected_price,
            actual_slippage_pct=actual_slippage_pct,
            execution_session_id=execution_session_id or "",
            order_id=order_id or "",
            strategy=strategy,
            signal_score=signal_score,
            reason=reason,
            mode=mode,
            signal_at=signal_at,
            order_at=order_at or datetime.now(),
            price_gap=price_gap,
        )
        session.add(trade)
        session.commit()
        # 커밋으로 만료된 속성 재적재 — 호출부(보상 롤백)가 세션 밖에서 id를 읽는다.
        # 이거 없으면 반환 객체 접근이 DetachedInstanceError.
        session.refresh(trade)
        session.expunge(trade)
        logger.info("매매 기록 저장: {} {} {}주 @ {:,.0f}원", action, symbol, quantity, price)
        return trade
    except Exception as e:
        session.rollback()
        logger.error("매매 기록 저장 실패: {}", e)
        raise
    finally:
        session.close()


def delete_trade_by_id(trade_id: int) -> bool:
    """매매 기록 1건 삭제 — 체결 반영(포지션 저장) 실패 시의 보상 롤백 전용.

    매매만 남고 포지션이 없으면 현금만 차감된 반쪽 원장이 돼 유령 낙폭과
    리스크 가드 오발동을 만든다(2026-07-07 실측: -41% 스냅샷 4일). 일반 삭제
    용도로 쓰지 말 것 — 트랙레코드는 불변이 원칙이다.
    """
    session = get_session()
    try:
        row = session.query(TradeHistory).filter(TradeHistory.id == trade_id).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
        logger.warning(
            "매매 기록 보상 삭제: id={} {} {} {}주 — 포지션 반영 실패에 따른 원장 정합 복구",
            trade_id, row.action, row.symbol, row.quantity,
        )
        return True
    except Exception as e:
        session.rollback()
        logger.error("매매 기록 보상 삭제 실패: id={} — {}", trade_id, e)
        raise
    finally:
        session.close()


@with_retry
def get_trade_history(
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    account_key: Optional[str] = None,
    execution_session_id: Optional[str] = None,
) -> List[TradeHistory]:
    """매매 기록 조회 (account_key 지정 시 해당 계좌만)."""
    session = get_session()
    try:
        query = session.query(TradeHistory)
        if account_key is not None:
            query = query.filter(TradeHistory.account_key == (account_key or ""))
        if symbol:
            query = query.filter(TradeHistory.symbol == symbol)
        if mode:
            query = query.filter(TradeHistory.mode == mode)
        if execution_session_id is not None:
            query = query.filter(TradeHistory.execution_session_id == (execution_session_id or ""))
        if start_date:
            query = query.filter(TradeHistory.executed_at >= start_date)
        if end_date:
            query = query.filter(TradeHistory.executed_at <= end_date)

        return query.order_by(TradeHistory.executed_at.desc()).all()
    finally:
        session.close()


@with_retry
def get_recent_sell_trades(
    limit: int = 20,
    mode: Optional[str] = None,
    account_key: Optional[str] = None,
) -> List[TradeHistory]:
    """최근 매도 거래만 시간 역순으로 조회 (성과 열화 감지용)."""
    all_trades = get_trade_history(mode=mode, account_key=account_key)
    sells = []
    for t in all_trades:
        if (t.action or "").upper() in ("SELL", "STOP_LOSS", "TAKE_PROFIT", "TAKE_PROFIT_PARTIAL", "TRAILING_STOP"):
            sells.append(t)
            if len(sells) >= limit:
                break
    return sells


@with_retry
def count_monthly_buy_trades(
    symbol: str,
    *,
    mode: Optional[str] = None,
    account_key: Optional[str] = None,
    at: Optional[datetime] = None,
) -> int:
    """해당 월의 동일 종목 BUY 체결 기록 수를 반환한다."""
    dt = at or datetime.now()
    month_start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    session = get_session()
    try:
        query = session.query(TradeHistory).filter(
            TradeHistory.symbol == symbol,
            TradeHistory.action == "BUY",
            TradeHistory.executed_at >= month_start,
            TradeHistory.executed_at < next_month,
        )
        if mode:
            query = query.filter(TradeHistory.mode == mode)
        if account_key is not None:
            query = query.filter(TradeHistory.account_key == (account_key or ""))
        return int(query.count())
    finally:
        session.close()


@with_retry
def get_trade_cash_summary(
    mode: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    account_key: Optional[str] = None,
) -> dict:
    """매매 기록 기준 현금 흐름 요약.

    TradeHistory.price는 실제 체결가다. 슬리피지는 체결가에 이미 반영된
    진단값이므로 현금 흐름에는 수수료와 세금만 별도 비용으로 반영한다.
    """
    trades = get_trade_history(mode=mode, start_date=start_date, end_date=end_date, account_key=account_key)

    cash_delta = 0.0
    buy_count = 0
    sell_count = 0
    total_commission = 0.0
    total_tax = 0.0
    total_slippage = 0.0

    for trade in trades:
        action = (trade.action or "").upper()
        total_amount = trade.total_amount or 0
        cash_costs = (trade.commission or 0) + (trade.tax or 0)

        total_commission += trade.commission or 0
        total_tax += trade.tax or 0
        total_slippage += trade.slippage or 0

        if action == "BUY":
            buy_count += 1
            cash_delta -= (total_amount + cash_costs)
        else:
            sell_count += 1
            cash_delta += (total_amount - cash_costs)

    return {
        "cash_delta": round(cash_delta, 0),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_trades": len(trades),
        "commission": round(total_commission, 0),
        "tax": round(total_tax, 0),
        "slippage": round(total_slippage, 0),
    }


def get_strategy_performance_summary(
    mode: Optional[str] = None,
    account_key: Optional[str] = None,
    days: int = 30,
) -> dict[str, dict]:
    """
    전략별 성과 분리 측정.

    Returns:
        {"scoring": {"trades": 12, "wins": 7, "win_rate": 58.3, "total_pnl": 150000, "total_cost": 5000}, ...}
    """
    since = datetime.now() - timedelta(days=days) if days > 0 else None
    trades = get_trade_history(mode=mode, start_date=since, account_key=account_key)
    by_strategy: dict[str, dict] = {}
    for t in trades:
        strat = t.strategy or "unknown"
        if strat not in by_strategy:
            by_strategy[strat] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "total_cost": 0.0}
        action = (t.action or "").upper()
        if action == "BUY":
            continue
        s = by_strategy[strat]
        s["trades"] += 1
        pnl = _extract_pnl_from_reason(t.reason or "")
        s["total_pnl"] += pnl
        s["total_cost"] += (t.commission or 0) + (t.tax or 0) + (t.slippage or 0)
        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1
    for strat, s in by_strategy.items():
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0.0
    return by_strategy


def _extract_pnl_from_reason(reason: str) -> float:
    """OrderExecutor가 reason에 남긴 'PnL: 12,345원' 값을 파싱."""
    if not reason:
        return 0.0
    match = re.search(r"PnL:\s*([\-0-9,]+)원", reason)
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0


@with_retry
def get_daily_trade_summary(
    date: Optional[datetime] = None,
    mode: Optional[str] = None,
    account_key: Optional[str] = None,
) -> dict:
    """특정 일자의 매매 요약."""
    dt = date or datetime.now()
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    trades = get_trade_history(mode=mode, start_date=start, end_date=end, account_key=account_key)

    realized_pnl = 0.0
    winning_trades = 0
    losing_trades = 0

    for trade in trades:
        if (trade.action or "").upper() == "BUY":
            continue
        pnl = _extract_pnl_from_reason(trade.reason or "")
        realized_pnl += pnl
        if pnl > 0:
            winning_trades += 1
        else:
            losing_trades += 1

    cash_summary = get_trade_cash_summary(mode=mode, start_date=start, end_date=end, account_key=account_key)

    return {
        "date": start,
        "total_trades": cash_summary["total_trades"],
        "buy_count": cash_summary["buy_count"],
        "sell_count": cash_summary["sell_count"],
        "realized_pnl": round(realized_pnl, 0),
        "total_commission": cash_summary["commission"],
        "total_tax": cash_summary["tax"],
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
    }


# =============================================================
# 포지션 관련
# =============================================================

def _ledger_mode(mode: str = "paper") -> str:
    """장부 mode를 소문자로 정규화한다. 빈 값은 하위 호환상 paper."""
    return str(mode or "paper").strip().lower() or "paper"


@with_retry
def save_position(
    symbol: str,
    avg_price: float,
    quantity: int,
    stop_loss_price: float = None,
    take_profit_price: float = None,
    trailing_stop_price: float = None,
    strategy: str = "",
    account_key: str = "",
    mode: str = "paper",
) -> Position:
    """포지션 저장 (신규 또는 업데이트). mode+account_key 장부별 격리."""
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        position = session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()

        if position:
            # 기존 포지션 업데이트 (추가 매수 시 평균 단가 재계산)
            total_invested = position.avg_price * position.quantity + avg_price * quantity
            total_quantity = position.quantity + quantity
            position.avg_price = total_invested / total_quantity
            position.quantity = total_quantity
            position.total_invested = total_invested
            if stop_loss_price:
                position.stop_loss_price = stop_loss_price
            if take_profit_price:
                position.take_profit_price = take_profit_price
            if trailing_stop_price:
                position.trailing_stop_price = trailing_stop_price
            position.highest_price = max(position.highest_price or 0, avg_price)
            # 추가 매수로 평균단가가 바뀌면 1차 부분 익절 목표도 새로 잡히므로 재발동 허용.
            position.partial_tp_done = False
        else:
            # 신규 포지션
            position = Position(
                mode=md,
                account_key=ak,
                symbol=symbol,
                avg_price=avg_price,
                quantity=quantity,
                total_invested=avg_price * quantity,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                trailing_stop_price=trailing_stop_price,
                highest_price=avg_price,
                strategy=strategy,
            )
            session.add(position)

        session.commit()
        logger.info("포지션 저장: {} {}주 평균가 {:,.0f}원", symbol, position.quantity, position.avg_price)
        return position
    except Exception as e:
        session.rollback()
        logger.error("포지션 저장 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def replace_position_from_broker(
    symbol: str,
    avg_price: float,
    quantity: int,
    stop_loss_price: float = None,
    take_profit_price: float = None,
    trailing_stop_price: float = None,
    strategy: str = "",
    account_key: str = "",
    mode: str = "paper",
) -> Position:
    """
    브로커 잔고 기준으로 포지션을 절대값 보정한다.

    save_position()은 추가 매수 경로라 기존 수량에 더하므로, KIS↔DB 자동보정에서는
    이 함수를 사용해 quantity/avg_price/total_invested를 브로커 값으로 덮어쓴다.
    """
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        qty = int(quantity)
        if qty <= 0:
            raise ValueError("브로커 보정 수량은 1주 이상이어야 합니다")

        position = session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()
        if position:
            position.avg_price = float(avg_price)
            position.quantity = qty
            position.total_invested = float(avg_price) * qty
            position.highest_price = max(position.highest_price or 0, float(avg_price))
            if strategy:
                position.strategy = strategy
        else:
            position = Position(
                mode=md,
                account_key=ak,
                symbol=symbol,
                avg_price=float(avg_price),
                quantity=qty,
                total_invested=float(avg_price) * qty,
                highest_price=float(avg_price),
                strategy=strategy,
            )
            session.add(position)

        if stop_loss_price is not None:
            position.stop_loss_price = stop_loss_price
        if take_profit_price is not None:
            position.take_profit_price = take_profit_price
        if trailing_stop_price is not None:
            position.trailing_stop_price = trailing_stop_price

        session.commit()
        logger.info("브로커 기준 포지션 보정: {} {}주 평균가 {:,.0f}원", symbol, qty, float(avg_price))
        return position
    except Exception as e:
        session.rollback()
        logger.error("브로커 기준 포지션 보정 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def get_position(
    symbol: str, account_key: str = "", mode: str = "paper"
) -> Optional[Position]:
    """특정 mode+계좌+종목의 포지션 조회."""
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        return session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()
    finally:
        session.close()


@with_retry
def get_all_positions(
    account_key: Optional[str] = None, mode: str = "paper"
) -> List[Position]:
    """지정 mode의 모든 포지션 조회 (account_key는 선택 필터)."""
    session = get_session()
    try:
        query = session.query(Position).filter(Position.mode == _ledger_mode(mode))
        if account_key is not None:
            query = query.filter(Position.account_key == (account_key or ""))
        return query.all()
    finally:
        session.close()


@with_retry
def delete_position(symbol: str, account_key: str = "", mode: str = "paper"):
    """포지션 삭제 (전량 매도 시)."""
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).delete()
        session.commit()
        logger.info("포지션 삭제: {}", symbol)
    except Exception as e:
        session.rollback()
        logger.error("포지션 삭제 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def reduce_position(
    symbol: str, sell_qty: int, account_key: str = "", mode: str = "paper"
) -> Optional[Position]:
    """
    부분 매도: 수량만 감소, 평균 단가 유지.
    남은 수량이 0이면 delete_position 후 None 반환.
    """
    if sell_qty <= 0:
        return get_position(symbol, account_key=account_key, mode=mode)
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        position = session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()
        if not position:
            return None
        remaining = position.quantity - sell_qty
        if remaining <= 0:
            session.delete(position)
            session.commit()
            logger.info("포지션 감소(전량): {} — 삭제됨", symbol)
            return None
        position.quantity = remaining
        position.total_invested = position.avg_price * remaining
        session.commit()
        logger.info("포지션 감소: {} {}주 남음 (평균가 {:,.0f})", symbol, remaining, position.avg_price)
        return position
    except Exception as e:
        session.rollback()
        logger.error("포지션 감소 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def update_trailing_stop(
    symbol: str,
    current_price: float,
    trailing_rate: float,
    account_key: str = "",
    mode: str = "paper",
):
    """
    트레일링 스탑 가격 업데이트
    - 현재가가 최고가를 경신하면 스탑가도 갱신
    """
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        position = session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()
        if position and current_price > (position.highest_price or 0):
            position.highest_price = current_price
            position.trailing_stop_price = current_price * (1 - trailing_rate)
            session.commit()
    except Exception as e:
        session.rollback()
        logger.error("트레일링 스탑 업데이트 실패: {}", e)
    finally:
        session.close()


@with_retry
def update_position_targets(
    symbol: str,
    stop_loss_price: float = None,
    take_profit_price: float = None,
    trailing_stop_price: float = None,
    account_key: str = "",
    partial_tp_done: bool = None,
    mode: str = "paper",
):
    """
    포지션의 손절/익절/트레일링 가격을 업데이트 (부분 매도 후 재조정 등).
    None인 필드는 변경하지 않음.
    """
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        position = session.query(Position).filter(
            Position.mode == md,
            Position.account_key == ak,
            Position.symbol == symbol,
        ).first()
        if not position:
            return
        if stop_loss_price is not None:
            position.stop_loss_price = stop_loss_price
        if take_profit_price is not None:
            position.take_profit_price = take_profit_price
        if trailing_stop_price is not None:
            position.trailing_stop_price = trailing_stop_price
        if partial_tp_done is not None:
            position.partial_tp_done = bool(partial_tp_done)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("포지션 타겟 업데이트 실패: {}", e)
    finally:
        session.close()


def update_stop_loss_price(
    symbol: str,
    stop_loss_price: float,
    account_key: str = "",
    mode: str = "paper",
):
    # 래칟 손절 갱신: Position.stop_loss_price 한 필드만 update_position_targets로 위임.
    update_position_targets(
        symbol,
        stop_loss_price=stop_loss_price,
        account_key=account_key,
        mode=mode,
    )


# =============================================================
# 포트폴리오 스냅샷 관련
# =============================================================

@with_retry
def save_portfolio_snapshot(
    total_value: float,
    cash: float,
    invested: float,
    daily_return: float = 0,
    cumulative_return: float = 0,
    mdd: float = 0,
    position_count: int = 0,
    account_key: str = "",
    peak_value: float = None,
    snapshot_date: datetime = None,
    mode: str = "paper",
):
    """일일 포트폴리오 스냅샷 저장 (mode+account_key 장부별 격리).

    snapshot_date: 스냅샷 귀속 날짜(자정으로 정규화). 미지정 시 오늘.
    비거래일 보충 실행에서 NAV의 가격 기준일(직전 거래일)로 귀속할 때 사용.
    """
    session = get_session()
    try:
        base = snapshot_date or datetime.now()
        if not isinstance(base, datetime):
            base = datetime(base.year, base.month, base.day)
        today = base.replace(hour=0, minute=0, second=0, microsecond=0)
        ak = account_key or ""
        md = _ledger_mode(mode)
        snapshot = PortfolioSnapshot(
            mode=md,
            account_key=ak,
            date=today,
            total_value=total_value,
            cash=cash,
            invested=invested,
            daily_return=daily_return,
            cumulative_return=cumulative_return,
            mdd=mdd,
            peak_value=peak_value,
            position_count=position_count,
        )
        # merge by (mode, account_key, date)
        existing = session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.mode == md,
            PortfolioSnapshot.account_key == ak,
            PortfolioSnapshot.date == today,
        ).first()
        if existing:
            existing.total_value = total_value
            existing.cash = cash
            existing.invested = invested
            existing.daily_return = daily_return
            existing.cumulative_return = cumulative_return
            existing.mdd = mdd
            existing.peak_value = peak_value
            existing.position_count = position_count
            # created_at은 '이 값이 마지막으로 측정된 시각'이다 — TWR 체인의 유입 경계가
            # 이 시각을 쓰므로, 같은 날 재실행(upsert) 때 갱신하지 않으면 재실행 전에
            # 반영된 입금이 다음 날 구간에 이중 산입돼 수익률이 영구 왜곡된다(적대적 리뷰 HIGH).
            existing.created_at = datetime.now()
        else:
            session.add(snapshot)
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        logger.error("포트폴리오 스냅샷 저장 실패: {}", e)
        return False  # 호출부가 실패를 인지해야 한다 — 삼키면 거짓 SNAPSHOT_SAVED가 기록됨
    finally:
        session.close()


@with_retry
def get_latest_peak_value(
    account_key: str = "", mode: str = "paper"
) -> float | None:
    """DB에서 가장 최근 스냅샷의 peak_value를 복구. 없으면 None."""
    session = get_session()
    try:
        ak = account_key or ""
        md = _ledger_mode(mode)
        row = (
            session.query(PortfolioSnapshot.peak_value)
            .filter(
                PortfolioSnapshot.mode == md,
                PortfolioSnapshot.account_key == ak,
                PortfolioSnapshot.peak_value.isnot(None),
            )
            .order_by(PortfolioSnapshot.date.desc())
            .first()
        )
        return float(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.debug("peak_value 복구 실패 (신규 DB 또는 컬럼 미존재): {}", e)
        return None
    finally:
        session.close()


# =============================================================
# 외부 현금 흐름 (입금/출금) — 적립식 지원 (docs/POCKET_TRACK_PLAN.md §4)
# =============================================================

@with_retry
def record_cash_flow(
    amount: float,
    account_key: str = "",
    occurred_at: Optional[datetime] = None,
    note: str = "",
    mode: str = "paper",
    request_id: Optional[str] = None,
) -> int:
    """외부 현금 흐름(+입금/-출금)을 기록하고 id를 반환한다.

    입금은 수익이 아니다 — TWR 계산이 이 기록으로 구간을 나눈다. amount=0은 무의미
    하므로 ValueError. NaN/Inf는 합산(현금·원금·TWR)을 통째로 오염시키므로 최종
    방어선인 여기서도 차단한다(NaN은 truthy라 `if not amount`를 통과한다).
    """
    import math

    if not amount or not math.isfinite(float(amount)):
        raise ValueError("amount는 0이 아닌 유한한 숫자여야 합니다 (+입금 / -출금)")
    ledger_mode = _ledger_mode(mode)
    normalized_account = account_key or ""
    normalized_note = str(note or "")[:200]
    normalized_request_id = str(request_id or "").strip() or None
    if normalized_request_id and len(normalized_request_id) > 64:
        raise ValueError("request_id는 64자 이하여야 합니다")

    session = get_session()
    try:
        def _existing_idempotent_row():
            if not normalized_request_id:
                return None
            return (
                session.query(CashFlow)
                .filter(
                    CashFlow.mode == ledger_mode,
                    CashFlow.account_key == normalized_account,
                    CashFlow.request_id == normalized_request_id,
                )
                .first()
            )

        def _validated_existing_id(row):
            if row is None:
                return None
            if (
                float(row.amount) != float(amount)
                or str(row.note or "") != normalized_note
            ):
                raise ValueError(
                    "같은 request_id가 다른 입금 내용에 사용되었습니다"
                )
            return int(row.id)

        existing_id = _validated_existing_id(_existing_idempotent_row())
        if existing_id is not None:
            return existing_id

        row = CashFlow(
            account_key=normalized_account,
            amount=float(amount),
            occurred_at=occurred_at or datetime.now(),
            note=normalized_note,
            mode=ledger_mode,
            request_id=normalized_request_id,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            # 동시 재시도가 고유 인덱스에서 경합한 경우 이미 기록된 동일 행을 반환한다.
            existing_id = _validated_existing_id(_existing_idempotent_row())
            if existing_id is not None:
                return existing_id
            raise
        return int(row.id)
    finally:
        session.close()


@with_retry
def has_cash_flows(account_key: str = "", mode: str = "paper") -> bool:
    """계정에 외부 현금 흐름 기록이 하나라도 있는가 — TWR 분기 판정용.

    순합(net)이 아니라 존재 여부로 판정한다: +100 뒤 -100처럼 순합 0이어도 구간
    수익률은 이미 흐름의 영향을 받았으므로 TWR 경로를 유지해야 한다(적대적 리뷰 low).
    """
    session = get_session()
    try:
        return (
            session.query(CashFlow.id)
            .filter(
                CashFlow.mode == _ledger_mode(mode),
                CashFlow.account_key == (account_key or ""),
            )
            .first()
        ) is not None
    finally:
        session.close()


@with_retry
def get_cash_flows(account_key: str = "", mode: str = "paper") -> list:
    """계정의 외부 현금 흐름 목록 [(occurred_at, amount)...] — 시간가중 자본 계산용."""
    session = get_session()
    try:
        rows = (
            session.query(CashFlow)
            .filter(
                CashFlow.mode == _ledger_mode(mode),
                CashFlow.account_key == (account_key or ""),
            )
            .order_by(CashFlow.occurred_at.asc())
            .all()
        )
        return [(r.occurred_at, float(r.amount or 0)) for r in rows]
    finally:
        session.close()


@with_retry
def get_recent_cash_flows(
    account_key: str = "", limit: int = 12, mode: str = "paper"
) -> list:
    """최근 입금/출금 내역 [{occurred_at, amount, note}...] 최신순 — 대시보드 표시용."""
    session = get_session()
    try:
        rows = (
            session.query(CashFlow)
            .filter(
                CashFlow.mode == _ledger_mode(mode),
                CashFlow.account_key == (account_key or ""),
            )
            .order_by(CashFlow.occurred_at.desc())
            .limit(int(limit))
            .all()
        )
        return [
            {
                "occurred_at": str(r.occurred_at)[:16],
                "amount": float(r.amount or 0),
                "note": r.note or "",
            }
            for r in rows
        ]
    finally:
        session.close()


@with_retry
def get_cash_flow_total(
    account_key: str = "",
    until: Optional[datetime] = None,
    mode: str = "paper",
) -> float:
    """계정의 외부 현금 흐름 순합(입금-출금). until 지정 시 그 시각 이하만."""
    session = get_session()
    try:
        query = session.query(CashFlow).filter(
            CashFlow.mode == _ledger_mode(mode),
            CashFlow.account_key == (account_key or ""),
        )
        if until is not None:
            query = query.filter(CashFlow.occurred_at <= until)
        return float(sum(r.amount or 0 for r in query.all()))
    finally:
        session.close()


@with_retry
def get_cash_flow_total_between(
    account_key: str,
    after: datetime,
    until: datetime,
    mode: str = "paper",
) -> float:
    """(after, until] 구간의 외부 현금 흐름 순합 — TWR 구간 수익률 보정용.

    after는 배타(직전 스냅샷 시각), until은 포함(이번 스냅샷 시각). 스냅샷 날짜는
    자정 정규화이므로 같은 날 입금은 그 날 스냅샷 구간에 귀속된다.
    """
    session = get_session()
    try:
        rows = (
            session.query(CashFlow)
            .filter(
                CashFlow.mode == _ledger_mode(mode),
                CashFlow.account_key == (account_key or ""),
            )
            .filter(CashFlow.occurred_at > after)
            .filter(CashFlow.occurred_at <= until)
            .all()
        )
        return float(sum(r.amount or 0 for r in rows))
    finally:
        session.close()


@with_retry
def get_latest_snapshot_summary(
    account_key: str = "", mode: str = "paper"
) -> Optional[dict]:
    """가장 최근 스냅샷의 (date, created_at, total_value, cumulative_return) 요약.

    TWR 체인 계산용 — created_at은 실제 측정 시각이라 '직전 측정 이후 유입' 경계로
    쓴다(date는 자정 귀속이라 같은 날 이른 입금을 이중 산입할 수 있음).
    """
    session = get_session()
    try:
        row = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.mode == _ledger_mode(mode),
                PortfolioSnapshot.account_key == (account_key or ""),
            )
            .order_by(PortfolioSnapshot.date.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "date": row.date,
            "created_at": row.created_at,
            "total_value": float(row.total_value),
            "cumulative_return": float(row.cumulative_return or 0.0),
        }
    finally:
        session.close()


@with_retry
def get_max_cumulative_return(
    account_key: str = "", mode: str = "paper"
) -> Optional[float]:
    """계정 스냅샷의 최대 누적수익률(%). TWR 지수 기준 MDD의 피크 복원용."""
    session = get_session()
    try:
        row = (
            session.query(PortfolioSnapshot.cumulative_return)
            .filter(
                PortfolioSnapshot.mode == _ledger_mode(mode),
                PortfolioSnapshot.account_key == (account_key or ""),
                PortfolioSnapshot.cumulative_return.isnot(None),
            )
            .order_by(PortfolioSnapshot.cumulative_return.desc())
            .first()
        )
        return float(row[0]) if row and row[0] is not None else None
    finally:
        session.close()


@with_retry
def get_portfolio_snapshots(
    days: int = 30,
    account_key: Optional[str] = None,
    mode: str = "paper",
) -> pd.DataFrame:
    """최근 N일간 지정 mode의 포트폴리오 스냅샷 조회."""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        query = session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.mode == _ledger_mode(mode),
            PortfolioSnapshot.date >= since,
        )
        if account_key is not None:
            query = query.filter(PortfolioSnapshot.account_key == (account_key or ""))
        results = query.order_by(PortfolioSnapshot.date).all()

        if not results:
            return pd.DataFrame()

        data = [{
            "date": r.date,
            "created_at": r.created_at,   # 실제 측정 시각 — TWR 유입 경계용
            "total_value": r.total_value,
            "cash": r.cash,
            "invested": r.invested,
            "daily_return": r.daily_return,
            "cumulative_return": r.cumulative_return,
            "mdd": r.mdd,
            "position_count": r.position_count,
        } for r in results]

        return pd.DataFrame(data)
    finally:
        session.close()


@with_retry
def get_portfolio_snapshots_between(
    start_date: datetime,
    end_date: datetime,
    account_key: Optional[str] = None,
    mode: str = "paper",
) -> List[dict]:
    """지정 기간 내 포트폴리오 스냅샷 목록 조회 (일별, 시간 무시). account_key 지정 시 해당 계좌만."""
    session = get_session()
    try:
        start_naive = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_naive = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        query = session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.mode == _ledger_mode(mode),
            PortfolioSnapshot.date >= start_naive,
            PortfolioSnapshot.date <= end_naive,
        )
        if account_key is not None:
            query = query.filter(PortfolioSnapshot.account_key == (account_key or ""))
        results = query.order_by(PortfolioSnapshot.date).all()
        return [
            {
                "date": r.date,
                "total_value": r.total_value,
                "cash": r.cash,
                "invested": r.invested,
                "cumulative_return": r.cumulative_return,
            }
            for r in results
        ]
    finally:
        session.close()


@with_retry
def get_paper_performance_metrics(
    start_date: datetime,
    end_date: datetime,
    mode: str = "paper",
    initial_capital: Optional[float] = None,
    account_key: Optional[str] = None,
) -> Optional[dict]:
    """
    모의투자(paper) 기간별 성과 지표 계산.
    스냅샷이 있으면 구간 시가/종가 포트폴리오 가치로 수익률 계산,
    없으면 해당 구간 매도 거래의 실현손익 합계로 대체 수익률 추정.
    account_key 지정 시 해당 계좌만 집계.
    """
    snapshots = get_portfolio_snapshots_between(
        start_date, end_date, account_key=account_key, mode=mode
    )
    trades = get_trade_history(mode=mode, start_date=start_date, end_date=end_date, account_key=account_key)
    sell_actions = ("SELL", "STOP_LOSS", "TAKE_PROFIT", "TAKE_PROFIT_PARTIAL", "TRAILING_STOP")
    sell_trades = [t for t in trades if (t.action or "").upper() in sell_actions]

    total_return_pct = None
    initial_value = initial_capital
    final_value = initial_capital

    if len(snapshots) >= 2:
        initial_value = snapshots[0]["total_value"]
        final_value = snapshots[-1]["total_value"]
        if initial_value and initial_value > 0:
            total_return_pct = ((final_value / initial_value) - 1) * 100
    elif initial_capital is not None and initial_capital > 0:
        if len(snapshots) == 1:
            final_value = snapshots[0]["total_value"]
            total_return_pct = ((final_value / initial_capital) - 1) * 100
            initial_value = initial_capital
        else:
            realized_pnl = sum(
                _extract_pnl_from_reason(t.reason or "") for t in sell_trades
            )
            total_return_pct = (realized_pnl / initial_capital) * 100
            initial_value = initial_capital
            final_value = initial_capital + realized_pnl

    if total_return_pct is None:
        return None

    winning = sum(1 for t in sell_trades if _extract_pnl_from_reason(t.reason or "") > 0)
    losing = len(sell_trades) - winning
    win_rate = (winning / len(sell_trades) * 100) if sell_trades else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": len(sell_trades),
        "winning_trades": winning,
        "losing_trades": losing,
        "initial_value": round(initial_value, 0),
        "final_value": round(final_value, 0),
    }


# =============================================================
# 일일 리포트 관련
# =============================================================

@with_retry
def save_daily_report(
    date: datetime,
    total_trades: int = 0,
    buy_count: int = 0,
    sell_count: int = 0,
    realized_pnl: float = 0,
    unrealized_pnl: float = 0,
    total_commission: float = 0,
    total_tax: float = 0,
    winning_trades: int = 0,
    losing_trades: int = 0,
    report_text: str = "",
    account_key: str = "",
):
    """일일 리포트 저장 또는 업데이트 (account_key: 전략별 계좌 구분)."""
    session = get_session()
    try:
        report_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        ak = account_key or ""
        report = session.query(DailyReport).filter(
            DailyReport.account_key == ak, DailyReport.date == report_date
        ).first()

        if report is None:
            report = DailyReport(account_key=ak, date=report_date)
            session.add(report)

        report.total_trades = total_trades
        report.buy_count = buy_count
        report.sell_count = sell_count
        report.realized_pnl = realized_pnl
        report.unrealized_pnl = unrealized_pnl
        report.total_commission = total_commission
        report.total_tax = total_tax
        report.winning_trades = winning_trades
        report.losing_trades = losing_trades
        report.report_text = report_text
        session.commit()
        return report
    except Exception as e:
        session.rollback()
        logger.error("일일 리포트 저장 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def get_daily_reports(days: int = 30, account_key: Optional[str] = None) -> pd.DataFrame:
    """최근 N일 일일 리포트 조회 (account_key 지정 시 해당 계좌만)."""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        query = session.query(DailyReport).filter(DailyReport.date >= since)
        if account_key is not None:
            query = query.filter(DailyReport.account_key == (account_key or ""))
        results = query.order_by(DailyReport.date.desc()).all()

        if not results:
            return pd.DataFrame()

        return pd.DataFrame([{
            "date": r.date,
            "total_trades": r.total_trades,
            "buy_count": r.buy_count,
            "sell_count": r.sell_count,
            "realized_pnl": r.realized_pnl,
            "unrealized_pnl": r.unrealized_pnl,
            "total_commission": r.total_commission,
            "total_tax": r.total_tax,
            "winning_trades": r.winning_trades,
            "losing_trades": r.losing_trades,
            "report_text": r.report_text,
        } for r in results])
    finally:
        session.close()


# =============================================================
# 전역 거래 HALT 킬스위치 (OperationEvent append-only 상태 로그)
# =============================================================

def _required_halt_text(value: str, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field}는 빈 값일 수 없습니다")
    return text[:max_length]


def _trading_halt_state_from_event(event: OperationEvent | None) -> dict:
    if event is None:
        return {
            "halted": False,
            "event_id": None,
            "event_type": None,
            "reason": "",
            "source": "",
            "mode": None,
            "created_at": None,
            "detail": {},
        }

    detail = {}
    if event.detail:
        try:
            decoded = json.loads(event.detail)
            if isinstance(decoded, dict):
                detail = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            # 이벤트 종류가 상태의 최종 근거이므로 detail 파싱 실패가
            # 상태 판정을 느슨하게 만들어서는 안 된다.
            detail = {}

    return {
        "halted": event.event_type == TRADING_HALT_SET,
        "event_id": int(event.id),
        "event_type": event.event_type,
        "reason": str(detail.get("reason") or event.message or ""),
        "source": str(detail.get("source") or ""),
        "mode": event.mode,
        "created_at": event.created_at,
        "detail": detail,
    }


@with_retry
def get_trading_halt_state() -> dict:
    """최신 전역 HALT 전환 이벤트를 읽는다.

    조회 예외는 호출자가 반드시 받도록 전파한다. 주문 경로는 이를
    fail-closed BUY 차단으로 변환하며, SELL은 이 함수를 호출하지 않는다.
    """
    session = get_session()
    try:
        event = (
            session.query(OperationEvent)
            .filter(OperationEvent.strategy == TRADING_HALT_STRATEGY)
            .filter(OperationEvent.event_type.in_((TRADING_HALT_SET, TRADING_HALT_CLEARED)))
            .order_by(OperationEvent.id.desc())
            .first()
        )
        return _trading_halt_state_from_event(event)
    finally:
        session.close()


def _record_trading_halt_transition(
    event_type: str,
    reason: str,
    *,
    source: str,
    mode: str,
    detail: Optional[dict] = None,
    expected_active_event_id: Optional[int] = None,
) -> dict:
    reason_text = _required_halt_text(reason, "reason", 2000)
    source_text = _required_halt_text(source, "source", 200)
    mode_text = str(mode or "live").strip().lower()[:20] or "live"
    payload = dict(detail or {})
    payload.update({
        "reason": reason_text,
        "source": source_text,
        "global": True,
    })

    session = get_session()
    try:
        # SET/CLEAR 전환을 같은 직렬화 지점에 묶는다. CLEAR가 상태를 읽은 뒤
        # 새 SET을 덮어쓰는 lost-update를 막으려면 CLEAR만 잠가서는 부족하다.
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _TRADING_HALT_ADVISORY_LOCK_KEY},
            )
        else:  # 지원 여부를 증명할 수 없는 DB에서는 안전하게 전환을 거부한다.
            raise RuntimeError(f"HALT 전환 락 미지원 DB dialect: {dialect}")

        if event_type == TRADING_HALT_CLEARED:
            latest = (
                session.query(OperationEvent)
                .filter(OperationEvent.strategy == TRADING_HALT_STRATEGY)
                .filter(
                    OperationEvent.event_type.in_(
                        (TRADING_HALT_SET, TRADING_HALT_CLEARED)
                    )
                )
                .order_by(OperationEvent.id.desc())
                .first()
            )
            latest_state = _trading_halt_state_from_event(latest)
            if not latest_state["halted"]:
                raise TradingHaltStateConflict(
                    "활성 HALT가 없거나 이미 해제되어 CLEAR를 거부합니다"
                )
            if int(latest_state["event_id"]) != int(expected_active_event_id):
                raise TradingHaltStateConflict(
                    "HALT 상태가 해제 확인 이후 변경되었습니다: "
                    f"expected_event_id={expected_active_event_id}, "
                    f"current_event_id={latest_state['event_id']}"
                )

        event = OperationEvent(
            event_type=event_type,
            severity="critical" if event_type == TRADING_HALT_SET else "warning",
            symbol=None,
            strategy=TRADING_HALT_STRATEGY,
            message=reason_text,
            detail=json.dumps(payload, ensure_ascii=False, default=str),
            mode=mode_text,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        state = _trading_halt_state_from_event(event)
        logger.log(
            "CRITICAL" if state["halted"] else "WARNING",
            "전역 거래 HALT 상태 전환: halted={} event_id={} source={} reason={}",
            state["halted"],
            state["event_id"],
            source_text,
            reason_text,
        )
        return state
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@with_retry
def set_trading_halt(
    reason: str,
    *,
    source: str = "operator",
    mode: str = "live",
    detail: Optional[dict] = None,
) -> dict:
    """전 계좌·전 전략 BUY를 막는 영속 HALT를 설정하고 감사 이벤트를 남긴다."""
    return _record_trading_halt_transition(
        TRADING_HALT_SET,
        reason,
        source=source,
        mode=mode,
        detail=detail,
    )


@with_retry
def clear_trading_halt(
    reason: str,
    *,
    source: str = "operator",
    mode: str = "live",
    confirmed: bool = False,
    expected_active_event_id: Optional[int] = None,
    detail: Optional[dict] = None,
) -> dict:
    """명시적 운영자 확인 후 HALT를 해제하고 append-only 감사 이벤트를 남긴다."""
    if confirmed is not True:
        raise ValueError("HALT 해제에는 confirmed=True 운영자 확인이 필요합니다")
    try:
        expected_id = int(expected_active_event_id)
    except (TypeError, ValueError):
        raise ValueError("HALT 해제에는 expected_active_event_id가 필요합니다") from None
    if expected_id <= 0:
        raise ValueError("expected_active_event_id는 양수여야 합니다")
    payload = dict(detail or {})
    payload["confirmed"] = True
    payload["expected_active_event_id"] = expected_id
    return _record_trading_halt_transition(
        TRADING_HALT_CLEARED,
        reason,
        source=source,
        mode=mode,
        detail=payload,
        expected_active_event_id=expected_id,
    )


# =============================================================
# 주문 실패 Dead-letter 큐
# =============================================================

@with_retry
def save_failed_order(
    symbol: str,
    action: str,
    price: float,
    quantity: int,
    reason: str = "",
    strategy: str = "",
    signal_score: float = 0,
    retry_count: int = 0,
    mode: str = "paper",
    error_detail: str = "",
    account_key: str = "",
):
    """재시도 실패한 주문을 dead-letter 테이블에 저장."""
    session = get_session()
    try:
        record = FailedOrder(
            account_key=account_key,
            symbol=symbol,
            action=action,
            price=price,
            quantity=quantity,
            reason=reason,
            strategy=strategy,
            signal_score=signal_score,
            retry_count=retry_count,
            mode=mode,
            error_detail=error_detail,
        )
        session.add(record)
        session.commit()
        logger.info("Dead-letter 저장: {} {} {}주 @{} (사유: {})", symbol, action, quantity, price, reason)
        return record.id
    except Exception as e:
        session.rollback()
        logger.error("Dead-letter 저장 실패: {}", e)
        return None
    finally:
        session.close()


@with_retry
def get_pending_failed_orders(account_key: str | None = None) -> list:
    """미처리(pending) 상태의 실패 주문 목록 반환. account_key가 None 또는 미전달이면 전체 조회."""
    session = get_session()
    try:
        q = session.query(FailedOrder).filter(FailedOrder.status == "pending")
        if account_key is not None and account_key != "":
            q = q.filter(FailedOrder.account_key == account_key)
        return q.order_by(FailedOrder.failed_at).all()
    finally:
        session.close()


@with_retry
def resolve_failed_order(order_id: int, status: str = "retried"):
    """실패 주문의 상태를 변경 (retried / cancelled)."""
    session = get_session()
    try:
        record = session.query(FailedOrder).get(order_id)
        if record:
            record.status = status
            record.resolved_at = datetime.now()
            session.commit()
            logger.info("Dead-letter #{} 상태 변경 → {}", order_id, status)
    except Exception as e:
        session.rollback()
        logger.error("Dead-letter 상태 변경 실패: {}", e)
    finally:
        session.close()


# =============================================================
# 주문 상태 추적 (OrderRecord)
# =============================================================

OPEN_ORDER_RECORD_STATUSES = ("SUBMITTED", "ACKED", "PARTIAL_FILLED")


def _status_value(status) -> str:
    return getattr(status, "value", status) or ""


@with_retry
def save_order_record(order) -> None:
    """상태기계 OrderRecord를 DB order_records에 upsert한다."""
    session = get_session()
    try:
        status = _status_value(getattr(order, "status", "NEW"))
        record = session.query(DbOrderRecord).filter(
            DbOrderRecord.order_id == order.order_id
        ).first()
        if record is None:
            record = DbOrderRecord(order_id=order.order_id)
            session.add(record)

        record.account_key = getattr(order, "account_key", "") or ""
        record.symbol = order.symbol
        record.action = order.action
        record.status = status
        record.broker_order_id = getattr(order, "broker_order_id", None)
        record.requested_qty = int(getattr(order, "requested_qty", 0) or 0)
        record.requested_price = float(getattr(order, "requested_price", 0) or 0)
        record.filled_qty = int(getattr(order, "filled_qty", 0) or 0)
        record.filled_price = float(getattr(order, "filled_price", 0) or 0)
        record.remaining_qty = int(getattr(order, "remaining_qty", 0) or 0)
        record.commission = float(getattr(order, "commission", 0) or 0)
        record.tax = float(getattr(order, "tax", 0) or 0)
        record.slippage = float(getattr(order, "slippage", 0) or 0)
        record.reject_reason = getattr(order, "reject_reason", "") or ""
        record.strategy = getattr(order, "strategy", "") or ""
        record.signal_score = float(getattr(order, "signal_score", 0) or 0)
        record.mode = getattr(order, "mode", "") or "paper"
        record.created_at = getattr(order, "created_at", None) or record.created_at or datetime.now()
        record.submitted_at = getattr(order, "submitted_at", None)
        record.filled_at = getattr(order, "filled_at", None)
        record.updated_at = getattr(order, "updated_at", None) or datetime.now()
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("주문 상태 저장 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def get_open_order_records(
    symbol: str | None = None,
    account_key: str = "",
    mode: str | None = "live",
) -> list[dict]:
    """DB에 남은 미완료 주문 상태를 반환한다."""
    session = get_session()
    try:
        q = session.query(DbOrderRecord).filter(
            DbOrderRecord.status.in_(OPEN_ORDER_RECORD_STATUSES)
        )
        if symbol:
            q = q.filter(DbOrderRecord.symbol == symbol)
        if account_key is not None:
            q = q.filter(DbOrderRecord.account_key == (account_key or ""))
        if mode:
            q = q.filter(DbOrderRecord.mode == mode)
        records = q.order_by(DbOrderRecord.updated_at.desc()).all()
        return [
            {
                "order_id": r.order_id,
                "account_key": r.account_key,
                "symbol": r.symbol,
                "action": r.action,
                "status": r.status,
                "broker_order_id": r.broker_order_id,
                "requested_qty": r.requested_qty,
                "requested_price": r.requested_price,
                "filled_qty": r.filled_qty or 0,
                "filled_price": r.filled_price or 0,
                "remaining_qty": r.remaining_qty or 0,
                "strategy": r.strategy or "",
                "mode": r.mode or "",
                "created_at": r.created_at,
                "submitted_at": r.submitted_at,
                "filled_at": r.filled_at,
                "updated_at": r.updated_at,
            }
            for r in records
        ]
    finally:
        session.close()


@with_retry
def reconcile_order_record(
    order_id: str,
    *,
    status: str = "RECONCILED",
    filled_qty: int | None = None,
    filled_price: float | None = None,
    remaining_qty: int | None = 0,
    reason: str = "",
) -> dict | None:
    """브로커 대조가 끝난 order_records 행을 open 상태에서 제외한다."""
    session = get_session()
    try:
        record = session.query(DbOrderRecord).filter(
            DbOrderRecord.order_id == order_id
        ).first()
        if record is None:
            return None

        record.status = str(status)
        if filled_qty is not None:
            record.filled_qty = int(filled_qty)
        if filled_price is not None:
            record.filled_price = float(filled_price)
        if remaining_qty is not None:
            record.remaining_qty = int(remaining_qty)
        if reason:
            record.reject_reason = reason
        if record.status == "RECONCILED" and record.filled_qty and not record.filled_at:
            record.filled_at = datetime.now()
        record.updated_at = datetime.now()
        session.commit()
        return {
            "order_id": record.order_id,
            "account_key": record.account_key,
            "symbol": record.symbol,
            "action": record.action,
            "status": record.status,
            "broker_order_id": record.broker_order_id,
            "requested_qty": record.requested_qty,
            "filled_qty": record.filled_qty or 0,
            "filled_price": record.filled_price or 0,
            "remaining_qty": record.remaining_qty or 0,
            "reason": record.reject_reason or "",
        }
    except Exception as e:
        session.rollback()
        logger.error("주문 상태 대조 완료 처리 실패: {}", e)
        raise
    finally:
        session.close()


@with_retry
def has_open_order_record(symbol: str, account_key: str = "", mode: str | None = "live") -> bool:
    """미완료 주문 상태가 DB에 남아 있으면 True. 조회 실패는 fail-closed 처리한다."""
    try:
        return bool(get_open_order_records(symbol=symbol, account_key=account_key, mode=mode))
    except Exception as e:
        logger.error("미완료 주문 상태 조회 실패 — 주문 차단: {}", e)
        return True


# =============================================================
# 중복 주문 방지 (DB 영속 가드)
# =============================================================

@with_retry
def claim_order_guard(symbol: str, expires_at: datetime) -> bool:
    """동일 종목 주문권을 DB UNIQUE 제약으로 원자적으로 획득한다.

    ``has_pending`` 뒤 ``mark_pending`` 하는 check-then-set은 두 프로세스가
    동시에 통과할 수 있다. 만료 행 삭제와 신규 INSERT를 한 트랜잭션에서
    수행하고, UNIQUE 충돌은 정상적인 claim 실패로 처리한다. 그 밖의 DB
    오류는 호출자에게 전파해 주문 경로가 fail-closed 하게 한다.
    """
    symbol_text = str(symbol or "").strip()
    if not symbol_text:
        raise ValueError("order guard symbol은 빈 값일 수 없습니다")

    session = get_session()
    try:
        now = datetime.now()
        session.query(PendingOrderGuard).filter(
            PendingOrderGuard.symbol == symbol_text,
            PendingOrderGuard.expires_at <= now,
        ).delete(synchronize_session=False)
        session.add(PendingOrderGuard(symbol=symbol_text, expires_at=expires_at))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@with_retry
def save_order_guard(symbol: str, expires_at: datetime):
    """중복 주문 방지 레코드 저장 (upsert)."""
    session = get_session()
    try:
        existing = session.query(PendingOrderGuard).filter(
            PendingOrderGuard.symbol == symbol
        ).first()
        if existing:
            existing.expires_at = expires_at
        else:
            session.add(PendingOrderGuard(symbol=symbol, expires_at=expires_at))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.debug("OrderGuard DB 저장 실패: {}", e)
    finally:
        session.close()


@with_retry
def has_pending_order_guard(symbol: str) -> bool:
    """DB에 유효한(미만료) 중복 주문 방지 레코드가 있는지 확인."""
    session = get_session()
    try:
        now = datetime.now()
        record = session.query(PendingOrderGuard).filter(
            PendingOrderGuard.symbol == symbol,
            PendingOrderGuard.expires_at > now,
        ).first()
        return record is not None
    except Exception:
        return False
    finally:
        session.close()


@with_retry
def clear_order_guard(symbol: str):
    """중복 주문 방지 레코드 제거."""
    session = get_session()
    try:
        session.query(PendingOrderGuard).filter(
            PendingOrderGuard.symbol == symbol
        ).delete()
        session.commit()
    except Exception as e:
        session.rollback()
        logger.debug("OrderGuard DB 제거 실패: {}", e)
    finally:
        session.close()

"""
데이터 접근 계층 (Repository)
- DB CRUD 함수 제공
- 주가 데이터, 매매 기록, 포지션, 포트폴리오 스냅샷 관리
- 모든 함수에 @with_retry 적용 — WAL 체크포인트 중 일시적 locked에도 안전
"""

import re
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy import and_
from loguru import logger

from database.models import (
    get_session, with_retry, StockPrice, TradeHistory,
    Position, PortfolioSnapshot, DailyReport, FailedOrder
)


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
) -> TradeHistory:
    """매매 기록 저장 (account_key: 전략별 계좌 구분, 다중 계좌 시 사용)."""
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
            strategy=strategy,
            signal_score=signal_score,
            reason=reason,
            mode=mode,
        )
        session.add(trade)
        session.commit()
        logger.info("매매 기록 저장: {} {} {}주 @ {:,.0f}원", action, symbol, quantity, price)
        return trade
    except Exception as e:
        session.rollback()
        logger.error("매매 기록 저장 실패: {}", e)
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
def get_trade_cash_summary(
    mode: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    account_key: Optional[str] = None,
) -> dict:
    """매매 기록 기준 현금 흐름 요약."""
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
        costs = (trade.commission or 0) + (trade.tax or 0) + (trade.slippage or 0)

        total_commission += trade.commission or 0
        total_tax += trade.tax or 0
        total_slippage += trade.slippage or 0

        if action == "BUY":
            buy_count += 1
            cash_delta -= (total_amount + costs)
        else:
            sell_count += 1
            cash_delta += (total_amount - costs)

    return {
        "cash_delta": round(cash_delta, 0),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_trades": len(trades),
        "commission": round(total_commission, 0),
        "tax": round(total_tax, 0),
        "slippage": round(total_slippage, 0),
    }


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
) -> Position:
    """포지션 저장 (신규 또는 업데이트). account_key: 전략별 계좌 구분."""
    session = get_session()
    try:
        ak = account_key or ""
        position = session.query(Position).filter(
            Position.account_key == ak, Position.symbol == symbol
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
        else:
            # 신규 포지션
            position = Position(
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
def get_position(symbol: str, account_key: str = "") -> Optional[Position]:
    """특정 종목의 포지션 조회 (account_key 지정 시 해당 계좌만)."""
    session = get_session()
    try:
        ak = account_key or ""
        return session.query(Position).filter(
            Position.account_key == ak, Position.symbol == symbol
        ).first()
    finally:
        session.close()


@with_retry
def get_all_positions(account_key: Optional[str] = None) -> List[Position]:
    """모든 포지션 조회 (account_key 지정 시 해당 계좌만)."""
    session = get_session()
    try:
        query = session.query(Position)
        if account_key is not None:
            query = query.filter(Position.account_key == (account_key or ""))
        return query.all()
    finally:
        session.close()


@with_retry
def delete_position(symbol: str, account_key: str = ""):
    """포지션 삭제 (전량 매도 시)."""
    session = get_session()
    try:
        ak = account_key or ""
        session.query(Position).filter(
            Position.account_key == ak, Position.symbol == symbol
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
def reduce_position(symbol: str, sell_qty: int, account_key: str = "") -> Optional[Position]:
    """
    부분 매도: 수량만 감소, 평균 단가 유지.
    남은 수량이 0이면 delete_position 후 None 반환.
    """
    if sell_qty <= 0:
        return get_position(symbol, account_key=account_key)
    session = get_session()
    try:
        ak = account_key or ""
        position = session.query(Position).filter(
            Position.account_key == ak, Position.symbol == symbol
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
def update_trailing_stop(symbol: str, current_price: float, trailing_rate: float, account_key: str = ""):
    """
    트레일링 스탑 가격 업데이트
    - 현재가가 최고가를 경신하면 스탑가도 갱신
    """
    session = get_session()
    try:
        ak = account_key or ""
        position = session.query(Position).filter(
            Position.account_key == ak, Position.symbol == symbol
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
):
    """일일 포트폴리오 스냅샷 저장 (account_key: 전략별 계좌 구분)."""
    session = get_session()
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        ak = account_key or ""
        snapshot = PortfolioSnapshot(
            account_key=ak,
            date=today,
            total_value=total_value,
            cash=cash,
            invested=invested,
            daily_return=daily_return,
            cumulative_return=cumulative_return,
            mdd=mdd,
            position_count=position_count,
        )
        # merge by (account_key, date)
        existing = session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.account_key == ak, PortfolioSnapshot.date == today
        ).first()
        if existing:
            existing.total_value = total_value
            existing.cash = cash
            existing.invested = invested
            existing.daily_return = daily_return
            existing.cumulative_return = cumulative_return
            existing.mdd = mdd
            existing.position_count = position_count
        else:
            session.add(snapshot)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("포트폴리오 스냅샷 저장 실패: {}", e)
    finally:
        session.close()


@with_retry
def get_portfolio_snapshots(days: int = 30, account_key: Optional[str] = None) -> pd.DataFrame:
    """최근 N일간 포트폴리오 스냅샷 조회 (account_key 지정 시 해당 계좌만)."""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        query = session.query(PortfolioSnapshot).filter(PortfolioSnapshot.date >= since)
        if account_key is not None:
            query = query.filter(PortfolioSnapshot.account_key == (account_key or ""))
        results = query.order_by(PortfolioSnapshot.date).all()

        if not results:
            return pd.DataFrame()

        data = [{
            "date": r.date,
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
) -> List[dict]:
    """지정 기간 내 포트폴리오 스냅샷 목록 조회 (일별, 시간 무시). account_key 지정 시 해당 계좌만."""
    session = get_session()
    try:
        start_naive = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_naive = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        query = session.query(PortfolioSnapshot).filter(
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
    snapshots = get_portfolio_snapshots_between(start_date, end_date, account_key=account_key)
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
def get_pending_failed_orders(account_key: str = "") -> list:
    """미처리(pending) 상태의 실패 주문 목록 반환."""
    session = get_session()
    try:
        q = session.query(FailedOrder).filter(FailedOrder.status == "pending")
        if account_key:
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

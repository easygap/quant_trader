"""
데이터 접근 계층 (Repository)
- DB CRUD 함수 제공
- 주가 데이터, 매매 기록, 포지션, 포트폴리오 스냅샷 관리
"""

from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy import and_
from loguru import logger

from database.models import (
    get_session, StockPrice, TradeHistory,
    Position, PortfolioSnapshot, DailyReport
)


# =============================================================
# 주가 데이터 관련
# =============================================================

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
) -> TradeHistory:
    """매매 기록 저장"""
    session = get_session()
    try:
        trade = TradeHistory(
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


def get_trade_history(
    symbol: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[TradeHistory]:
    """매매 기록 조회"""
    session = get_session()
    try:
        query = session.query(TradeHistory)
        if symbol:
            query = query.filter(TradeHistory.symbol == symbol)
        if start_date:
            query = query.filter(TradeHistory.executed_at >= start_date)
        if end_date:
            query = query.filter(TradeHistory.executed_at <= end_date)

        return query.order_by(TradeHistory.executed_at.desc()).all()
    finally:
        session.close()


# =============================================================
# 포지션 관련
# =============================================================

def save_position(
    symbol: str,
    avg_price: float,
    quantity: int,
    stop_loss_price: float = None,
    take_profit_price: float = None,
    trailing_stop_price: float = None,
    strategy: str = "",
) -> Position:
    """포지션 저장 (신규 또는 업데이트)"""
    session = get_session()
    try:
        position = session.query(Position).filter(Position.symbol == symbol).first()

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


def get_position(symbol: str) -> Optional[Position]:
    """특정 종목의 포지션 조회"""
    session = get_session()
    try:
        return session.query(Position).filter(Position.symbol == symbol).first()
    finally:
        session.close()


def get_all_positions() -> List[Position]:
    """모든 포지션 조회"""
    session = get_session()
    try:
        return session.query(Position).all()
    finally:
        session.close()


def delete_position(symbol: str):
    """포지션 삭제 (전량 매도 시)"""
    session = get_session()
    try:
        session.query(Position).filter(Position.symbol == symbol).delete()
        session.commit()
        logger.info("포지션 삭제: {}", symbol)
    except Exception as e:
        session.rollback()
        logger.error("포지션 삭제 실패: {}", e)
        raise
    finally:
        session.close()


def update_trailing_stop(symbol: str, current_price: float, trailing_rate: float):
    """
    트레일링 스탑 가격 업데이트
    - 현재가가 최고가를 경신하면 스탑가도 갱신
    """
    session = get_session()
    try:
        position = session.query(Position).filter(Position.symbol == symbol).first()
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

def save_portfolio_snapshot(
    total_value: float,
    cash: float,
    invested: float,
    daily_return: float = 0,
    cumulative_return: float = 0,
    mdd: float = 0,
    position_count: int = 0,
):
    """일일 포트폴리오 스냅샷 저장"""
    session = get_session()
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        snapshot = PortfolioSnapshot(
            date=today,
            total_value=total_value,
            cash=cash,
            invested=invested,
            daily_return=daily_return,
            cumulative_return=cumulative_return,
            mdd=mdd,
            position_count=position_count,
        )
        session.merge(snapshot)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("포트폴리오 스냅샷 저장 실패: {}", e)
    finally:
        session.close()


def get_portfolio_snapshots(days: int = 30) -> pd.DataFrame:
    """최근 N일간 포트폴리오 스냅샷 조회"""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        results = session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.date >= since
        ).order_by(PortfolioSnapshot.date).all()

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

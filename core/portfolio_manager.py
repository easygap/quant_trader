"""
포트폴리오 관리 모듈
- 보유 포지션 관리, 잔고/수익률 추적
"""

from datetime import datetime
from loguru import logger

from config.config_loader import Config
from database.repositories import (
    get_all_positions, save_portfolio_snapshot,
)


class PortfolioManager:
    """
    포트폴리오 관리자
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.initial_capital = self.config.risk_params.get(
            "position_sizing", {}
        ).get("initial_capital", 10000000)
        self._peak_value = self.initial_capital
        logger.info("PortfolioManager 초기화 (초기 자본: {:,.0f}원)", self.initial_capital)

    def get_portfolio_summary(self, current_prices: dict = None) -> dict:
        """
        포트폴리오 현황 요약

        Args:
            current_prices: {종목코드: 현재가} 딕셔너리

        Returns:
            포트폴리오 요약 딕셔너리
        """
        positions = get_all_positions()
        current_prices = current_prices or {}

        invested = 0
        current_value = 0
        position_details = []

        for pos in positions:
            price = current_prices.get(pos.symbol, pos.avg_price)
            pos_value = price * pos.quantity
            pos_invested = pos.avg_price * pos.quantity
            pnl = pos_value - pos_invested
            pnl_rate = ((price / pos.avg_price) - 1) * 100 if pos.avg_price > 0 else 0

            invested += pos_invested
            current_value += pos_value

            position_details.append({
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "invested": pos_invested,
                "current_value": pos_value,
                "pnl": pnl,
                "pnl_rate": pnl_rate,
            })

        # 현금 = 초기자본 - 투자금 (단순화)
        cash = self.initial_capital - invested
        total_value = cash + current_value
        total_return = ((total_value / self.initial_capital) - 1) * 100

        # MDD 계산
        if total_value > self._peak_value:
            self._peak_value = total_value
        mdd = ((self._peak_value - total_value) / self._peak_value) * 100

        return {
            "total_value": round(total_value, 0),
            "cash": round(cash, 0),
            "invested": round(invested, 0),
            "current_value": round(current_value, 0),
            "total_return": round(total_return, 2),
            "mdd": round(mdd, 2),
            "position_count": len(positions),
            "positions": position_details,
        }

    def save_daily_snapshot(self, current_prices: dict = None):
        """일일 포트폴리오 스냅샷 저장"""
        summary = self.get_portfolio_summary(current_prices)

        save_portfolio_snapshot(
            total_value=summary["total_value"],
            cash=summary["cash"],
            invested=summary["invested"],
            cumulative_return=summary["total_return"],
            mdd=summary["mdd"],
            position_count=summary["position_count"],
        )

        logger.info(
            "포트폴리오 스냅샷 저장: 총={:,.0f}원 | 수익={:.2f}% | MDD={:.2f}%",
            summary["total_value"], summary["total_return"], summary["mdd"],
        )

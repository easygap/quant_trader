"""
리스크 관리 모듈
- 포지션 사이징 (1% 룰)
- 손절/익절/트레일링 스탑 가격 계산
- MDD 계산 및 매매 중단 판단
"""

import pandas as pd
import numpy as np
from loguru import logger

from config.config_loader import Config


class RiskManager:
    """
    리스크 관리자

    사용법:
        rm = RiskManager()
        qty = rm.calculate_position_size(capital, entry_price, stop_price)
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.risk_params = self.config.risk_params

        # 현재 MDD 추적
        self._peak_value = 0
        self._is_halted = False  # 매매 중단 상태

        logger.info("RiskManager 초기화 완료")

    # =============================================================
    # 포지션 사이징
    # =============================================================

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> int:
        """
        1% 룰 기반 포지션 크기 계산

        Args:
            capital: 현재 총 자본
            entry_price: 매수 예정 가격
            stop_loss_price: 손절 가격

        Returns:
            매수 가능 수량
        """
        max_risk = self.risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)
        risk_amount = capital * max_risk  # 최대 손실 가능 금액

        risk_per_share = abs(entry_price - stop_loss_price)
        if risk_per_share <= 0:
            logger.warning("손절 폭이 0 이하 — 포지션 계산 불가")
            return 0

        quantity = int(risk_amount / risk_per_share)

        # 분산 투자 제한 확인
        max_ratio = self.risk_params.get("diversification", {}).get("max_position_ratio", 0.20)
        max_invest = capital * max_ratio
        max_by_ratio = int(max_invest / entry_price)

        final_qty = min(quantity, max_by_ratio)

        logger.info(
            "포지션 계산: 자본={:,.0f} | 진입가={:,.0f} | 손절가={:,.0f} | "
            "1% 룰={}주 | 비중제한={}주 | 최종={}주",
            capital, entry_price, stop_loss_price,
            quantity, max_by_ratio, final_qty,
        )

        return max(final_qty, 0)

    # =============================================================
    # 손절/익절 가격 계산
    # =============================================================

    def calculate_stop_loss(
        self,
        entry_price: float,
        atr: float = None,
    ) -> float:
        """
        손절 가격 계산

        Args:
            entry_price: 매수가
            atr: ATR 값 (변동성 기반 손절 시 필요)

        Returns:
            손절 가격
        """
        sl_config = self.risk_params.get("stop_loss", {})
        sl_type = sl_config.get("type", "fixed")

        if sl_type == "atr" and atr is not None:
            multiplier = sl_config.get("atr_multiplier", 2.0)
            stop_price = entry_price - (atr * multiplier)
        else:
            fixed_rate = sl_config.get("fixed_rate", 0.03)
            stop_price = entry_price * (1 - fixed_rate)

        logger.debug("손절가 계산: 매수가={:,.0f} → 손절가={:,.0f}", entry_price, stop_price)
        return round(stop_price, 0)

    def calculate_take_profit(self, entry_price: float) -> dict:
        """
        익절 가격 계산

        Args:
            entry_price: 매수가

        Returns:
            {
                "target_1": 1차 익절가 (부분 익절),
                "target_final": 최종 익절가,
                "partial_ratio": 1차 매도 비율,
            }
        """
        tp_config = self.risk_params.get("take_profit", {})
        fixed_rate = tp_config.get("fixed_rate", 0.10)
        partial_exit = tp_config.get("partial_exit", True)
        partial_ratio = tp_config.get("partial_ratio", 0.5)
        partial_target = tp_config.get("partial_target", 0.06)

        target_final = entry_price * (1 + fixed_rate)

        result = {
            "target_final": round(target_final, 0),
            "partial_ratio": partial_ratio if partial_exit else 1.0,
        }

        if partial_exit:
            result["target_1"] = round(entry_price * (1 + partial_target), 0)

        return result

    def calculate_trailing_stop(
        self,
        highest_price: float,
        atr: float = None,
    ) -> float:
        """
        트레일링 스탑 가격 계산

        Args:
            highest_price: 보유 중 최고가
            atr: ATR 값

        Returns:
            트레일링 스탑 가격
        """
        ts_config = self.risk_params.get("trailing_stop", {})

        if not ts_config.get("enabled", True):
            return 0

        ts_type = ts_config.get("type", "fixed")

        if ts_type == "atr" and atr is not None:
            multiplier = ts_config.get("atr_multiplier", 3.0)
            stop_price = highest_price - (atr * multiplier)
        else:
            fixed_rate = ts_config.get("fixed_rate", 0.03)
            stop_price = highest_price * (1 - fixed_rate)

        return round(stop_price, 0)

    # =============================================================
    # MDD 관리
    # =============================================================

    def check_mdd(self, current_value: float) -> dict:
        """
        MDD(최대 낙폭) 확인

        Args:
            current_value: 현재 포트폴리오 평가금액

        Returns:
            {
                "mdd": 현재 MDD (%), 
                "is_halted": 매매 중단 여부,
                "peak": 최고점,
            }
        """
        # 최고점 갱신
        if current_value > self._peak_value:
            self._peak_value = current_value

        # MDD 계산
        if self._peak_value > 0:
            mdd = (self._peak_value - current_value) / self._peak_value
        else:
            mdd = 0

        # 매매 중단 확인
        max_mdd = self.risk_params.get("drawdown", {}).get("max_portfolio_mdd", 0.15)
        if mdd >= max_mdd:
            if not self._is_halted:
                logger.warning(
                    "🚨 MDD 한도 도달! MDD={:.2f}% (한도: {:.2f}%) — 매매 중단",
                    mdd * 100, max_mdd * 100,
                )
                self._is_halted = True
        else:
            if self._is_halted and mdd < max_mdd * 0.5:
                # MDD가 한도의 50% 이하로 회복되면 재개
                logger.info("MDD 회복 — 매매 재개 (축소 규모)")
                self._is_halted = False

        return {
            "mdd": round(mdd * 100, 2),
            "is_halted": self._is_halted,
            "peak": self._peak_value,
        }

    def check_daily_loss(self, daily_pnl: float, capital: float) -> bool:
        """
        일일 손실 한도 확인

        Args:
            daily_pnl: 당일 손익
            capital: 총 자본

        Returns:
            True이면 매매 계속, False이면 중단
        """
        max_daily = self.risk_params.get("drawdown", {}).get("max_daily_loss", 0.03)
        daily_loss_rate = abs(daily_pnl) / capital if capital > 0 and daily_pnl < 0 else 0

        if daily_loss_rate >= max_daily:
            logger.warning(
                "🚨 일일 손실 한도 도달! 손실률={:.2f}% (한도: {:.2f}%)",
                daily_loss_rate * 100, max_daily * 100,
            )
            return False

        return True

    # =============================================================
    # 분산 투자 체크
    # =============================================================

    def check_diversification(
        self,
        current_positions: int,
        position_value: float,
        total_value: float,
    ) -> dict:
        """
        분산 투자 규칙 확인

        Args:
            current_positions: 현재 보유 종목 수
            position_value: 해당 종목 투자 금액
            total_value: 총 포트폴리오 가치

        Returns:
            {
                "can_buy": 매수 가능 여부,
                "reason": 불가 사유 (해당 시),
            }
        """
        div_config = self.risk_params.get("diversification", {})
        max_positions = div_config.get("max_positions", 10)
        max_ratio = div_config.get("max_position_ratio", 0.20)
        min_cash = div_config.get("min_cash_ratio", 0.20)

        # 최대 종목 수 초과
        if current_positions >= max_positions:
            return {"can_buy": False, "reason": f"최대 보유 종목({max_positions}개) 초과"}

        # 단일 종목 비중 초과
        if total_value > 0 and (position_value / total_value) > max_ratio:
            return {"can_buy": False, "reason": f"단일 종목 비중 {max_ratio*100:.0f}% 초과"}

        return {"can_buy": True, "reason": ""}

    # =============================================================
    # 거래 비용 계산
    # =============================================================

    def calculate_transaction_costs(
        self,
        price: float,
        quantity: int,
        action: str = "BUY",
    ) -> dict:
        """
        거래 비용 계산 (수수료 + 세금 + 슬리피지)

        Args:
            price: 체결 가격
            quantity: 체결 수량
            action: "BUY" 또는 "SELL"

        Returns:
            {
                "commission": 수수료,
                "tax": 세금 (매도 시만),
                "slippage": 슬리피지,
                "total_cost": 총 비용,
                "effective_price": 비용 반영 실효 가격,
            }
        """
        costs = self.risk_params.get("transaction_costs", {})
        amount = price * quantity

        commission = amount * costs.get("commission_rate", 0.00015)
        slippage = amount * costs.get("slippage", 0.0005)

        tax = 0
        if action.upper() == "SELL":
            tax = amount * costs.get("tax_rate", 0.002)

        total_cost = commission + tax + slippage

        # 실효 가격 (매수 시 높게, 매도 시 낮게)
        if action.upper() == "BUY":
            effective_price = price * (1 + costs.get("commission_rate", 0) + costs.get("slippage", 0))
        else:
            effective_price = price * (
                1 - costs.get("commission_rate", 0) - costs.get("slippage", 0) - costs.get("tax_rate", 0)
            )

        return {
            "commission": round(commission, 0),
            "tax": round(tax, 0),
            "slippage": round(slippage, 0),
            "total_cost": round(total_cost, 0),
            "effective_price": round(effective_price, 0),
        }

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


def _get_tick_size(price: float) -> int:
    """
    KRX 호가 단위 (원).
    가격대별: 2천원미만 1원, 5천원미만 5원, 2만원미만 10원, 5만원미만 50원,
    20만원미만 100원, 50만원미만 500원, 이상 1000원.
    """
    if price <= 0:
        return 1
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


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

    @staticmethod
    def _value_in_krw_for_symbol(symbol: str, value: float) -> float:
        """미국 티커면 USD 가치를 DataCollector 환율로 원화 환산 (비중·업종 합산용)."""
        try:
            from core.data_collector import DataCollector

            if DataCollector.is_us_ticker(symbol or ""):
                r = DataCollector.get_usd_krw_rate()
                if r and r > 0:
                    return float(value) * r
        except Exception as e:
            logger.debug("원화 환산 생략 ({}): {}", symbol, e)
        return float(value)

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
        # 진입가 유효성 검사 (분모 0 방지)
        if entry_price <= 0:
            logger.warning("진입가가 0 이하 — 포지션 계산 불가 (entry_price={})", entry_price)
            return 0

        if capital <= 0:
            logger.warning("자본이 0 이하 — 포지션 계산 불가")
            return 0

        max_risk = self.risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)
        risk_amount = capital * max_risk  # 최대 손실 가능 금액

        risk_per_share = abs(entry_price - stop_loss_price)
        if risk_per_share <= 0:
            logger.warning("손절 폭이 0 이하 — 포지션 계산 불가")
            return 0

        # ATR≈0 등 극단적으로 작은 손절 폭 방어 (진입가 대비 최소 0.1% 이상)
        min_risk_per_share = entry_price * 0.001
        if risk_per_share < min_risk_per_share:
            logger.warning(
                "손절 폭이 극소 — 포지션 계산 불가 (risk_per_share={:.2f} < min={:.2f})",
                risk_per_share, min_risk_per_share,
            )
            return 0

        quantity = int(risk_amount / risk_per_share)

        # 분산 투자 제한 확인 (entry_price > 0 검증 완료됨)
        max_ratio = self.risk_params.get("diversification", {}).get("max_position_ratio", 0.20)
        max_invest = capital * max_ratio
        max_by_ratio = int(max_invest / entry_price)

        final_qty = min(quantity, max_by_ratio)
        final_qty = max(final_qty, 0)

        logger.info(
            "포지션 계산: 자본={:,.0f} | 진입가={:,.0f} | 손절가={:,.0f} | "
            "1% 룰={}주 | 비중제한={}주 | 최종={}주",
            capital, entry_price, stop_loss_price,
            quantity, max_by_ratio, final_qty,
        )

        return final_qty

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
            recovery_scale = self.risk_params.get("drawdown", {}).get("recovery_scale", 0.5)
            if self._is_halted and mdd < max_mdd * (1 - recovery_scale):
                # MDD가 한도 대비 회복되면 재개 (recovery_scale: 복귀 시 축소 비율)
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
        available_cash: float = None,
        current_invested: float = 0,
        symbol: str = "",
        sector_map: dict | None = None,
        positions: list | None = None,
    ) -> dict:
        """
        분산 투자 규칙 확인 (종목 수·비중·투자비율·현금 + 업종 비중)

        Args:
            current_positions: 현재 보유 종목 수
            position_value: 해당 종목 투자 금액
            total_value: 총 포트폴리오 가치
            available_cash: 가용 현금
            current_invested: 현재 총 투자 금액
            symbol: 매수 대상 종목코드 (업종 체크용)
            sector_map: {종목코드: 업종명} 딕셔너리 (None이면 업종 체크 스킵)
            positions: 현재 보유 Position 객체 리스트 (업종 비중 계산용)

        Returns:
            {"can_buy": bool, "reason": str}
        """
        div_config = self.risk_params.get("diversification", {})
        max_positions = div_config.get("max_positions", 10)
        max_ratio = div_config.get("max_position_ratio", 0.20)
        max_investment_ratio = div_config.get("max_investment_ratio", 0.70)
        min_cash = div_config.get("min_cash_ratio", 0.20)

        position_value = self._value_in_krw_for_symbol(symbol, float(position_value or 0))
        current_invested = float(current_invested or 0)

        if current_positions >= max_positions:
            return {"can_buy": False, "reason": f"최대 보유 종목({max_positions}개) 초과"}

        if total_value > 0 and (position_value / total_value) > max_ratio:
            return {"can_buy": False, "reason": f"단일 종목 비중 {max_ratio*100:.0f}% 초과"}

        if total_value > 0:
            projected_invested_ratio = (current_invested + position_value) / total_value
            if projected_invested_ratio > max_investment_ratio:
                return {
                    "can_buy": False,
                    "reason": f"전체 투자 비중 {max_investment_ratio*100:.0f}% 초과",
                }

        if available_cash is not None and total_value > 0:
            remaining_cash = available_cash - position_value
            remaining_cash_ratio = remaining_cash / total_value
            if remaining_cash_ratio < min_cash:
                return {
                    "can_buy": False,
                    "reason": f"최소 현금 비중 {min_cash*100:.0f}% 미만",
                }

        # 업종별 최대 비중 체크
        max_sector_ratio = div_config.get("max_sector_ratio")
        if (
            max_sector_ratio is not None
            and max_sector_ratio > 0
            and total_value > 0
            and symbol
            and sector_map
            and positions is not None
        ):
            target_sector = sector_map.get(symbol, "")
            if target_sector:
                sector_invested = sum(
                    self._value_in_krw_for_symbol(
                        getattr(p, "symbol", ""),
                        float(getattr(p, "total_invested", 0) or 0),
                    )
                    for p in positions
                    if sector_map.get(getattr(p, "symbol", ""), "") == target_sector
                )
                projected = sector_invested + position_value
                if projected / total_value > max_sector_ratio:
                    return {
                        "can_buy": False,
                        "reason": (
                            f"업종 '{target_sector}' 비중 {projected / total_value * 100:.0f}% > "
                            f"상한 {max_sector_ratio * 100:.0f}%"
                        ),
                    }

        return {"can_buy": True, "reason": ""}

    # =============================================================
    # 전략 성과 열화 감지
    # =============================================================

    def check_recent_performance(self, recent_sell_trades: list) -> dict:
        """
        최근 매도 거래 승률로 성과 열화 여부 판단.
        시장 국면 변화로 전략이 손실을 낼 경우 신규 매수 중단.

        Args:
            recent_sell_trades: 최근 매도 거래 리스트 (각 항목에 reason 등 PnL 정보 있음)

        Returns:
            {"allowed": 매수 허용 여부, "win_rate": 승률(0~1), "reason": 사유}
        """
        cfg = self.risk_params.get("performance_degradation", {})
        if not cfg.get("enabled", False):
            return {"allowed": True, "win_rate": None, "reason": ""}

        min_win_rate = float(cfg.get("min_win_rate", 0.35))
        min_sample = max(5, int(cfg.get("recent_trades", 20)) // 2)

        if not recent_sell_trades or len(recent_sell_trades) < min_sample:
            return {"allowed": True, "win_rate": None, "reason": ""}

        wins = 0
        for t in recent_sell_trades:
            pnl = getattr(t, "pnl", None)
            if pnl is None and getattr(t, "reason", None):
                from database.repositories import _extract_pnl_from_reason
                pnl = _extract_pnl_from_reason(t.reason or "")
            if pnl is not None and pnl > 0:
                wins += 1
        n = len(recent_sell_trades)
        win_rate = wins / n if n > 0 else 0

        if win_rate < min_win_rate:
            logger.warning(
                "🚨 전략 성과 열화: 최근 {}건 승률 {:.1f}% (기준 {:.0f}% 미만) — 신규 매수 중단",
                n, win_rate * 100, min_win_rate * 100,
            )
            return {
                "allowed": False,
                "win_rate": win_rate,
                "reason": f"최근 {n}건 승률 {win_rate*100:.1f}% (기준 {min_win_rate*100:.0f}% 미만)",
            }
        return {"allowed": True, "win_rate": win_rate, "reason": ""}

    # =============================================================
    # 거래 비용 계산
    # =============================================================

    def calculate_transaction_costs(
        self,
        price: float,
        quantity: int,
        action: str = "BUY",
        avg_daily_volume: float = None,
        avg_price: float = None,
    ) -> dict:
        """
        거래 비용 계산 (수수료 + 증권거래세 + 슬리피지 + 양도소득세(선택))

        Args:
            price: 체결 가격
            quantity: 체결 수량
            action: "BUY" 또는 "SELL"
            avg_daily_volume: 일평균 거래량 (동적 슬리피지용)
            avg_price: 매도 시 평균 매입 단가 (양도소득세 계산용; 대주주 해당 시)

        Returns:
            commission(수수료), tax(증권거래세+농특세 매도 시 0.20%), capital_gains_tax(양도소득세, 설정 시),
            slippage, total_cost, effective_price 등.
        """
        costs = self.risk_params.get("transaction_costs", {})
        amount = price * quantity

        commission = amount * costs.get("commission_rate", 0.00015)
        dynamic = costs.get("dynamic_slippage", {})
        slippage_rate_fixed = costs.get("slippage", 0.0005)
        slippage_ticks = costs.get("slippage_ticks", 2)
        tick = _get_tick_size(price)
        participation_rate = 0.0
        slippage_multiplier = 1.0

        if avg_daily_volume and avg_daily_volume > 0:
            participation_rate = quantity / avg_daily_volume

        if dynamic.get("enabled", True) and participation_rate > 0:
            warn_threshold = dynamic.get("warn_at_volume_ratio", 0.01)
            critical_threshold = dynamic.get("critical_at_volume_ratio", 0.03)
            warn_multiplier = dynamic.get("warn_slippage_multiplier", 2.0)
            critical_multiplier = dynamic.get("critical_slippage_multiplier", 4.0)

            if participation_rate >= critical_threshold:
                slippage_multiplier = critical_multiplier
            elif participation_rate >= warn_threshold:
                slippage_multiplier = warn_multiplier

        # 호가 단위/거래량 기반 슬리피지: max(고정 비율, tick_size * N틱) * multiplier
        slippage_per_share = max(price * slippage_rate_fixed, tick * slippage_ticks)
        slippage_per_share *= slippage_multiplier
        slippage = slippage_per_share * quantity
        slippage_rate_effective = slippage_per_share / price if price > 0 else 0

        # 증권거래세+농특세: 매도 금액의 0.20% (2026년~ 코스피·코스닥 동일)
        tax = 0
        if action.upper() == "SELL":
            tax = amount * costs.get("tax_rate", 0.0020)

        # 양도소득세 (대주주 해당 시만; enabled 시 실현 이익에 대해 부과)
        capital_gains_tax = 0
        if action.upper() == "SELL" and avg_price is not None and quantity > 0:
            cgt_cfg = costs.get("capital_gains_tax", {}) or {}
            if cgt_cfg.get("enabled", False):
                gain = (price - avg_price) * quantity
                if gain > 0:
                    capital_gains_tax = gain * cgt_cfg.get("rate", 0.20)

        total_cost = commission + tax + slippage + capital_gains_tax

        # 실효 가격 (매수 시 높게, 매도 시 낮게; 증권거래세·슬리피지 반영, 양도소득세는 별도)
        if action.upper() == "BUY":
            effective_price = price * (1 + costs.get("commission_rate", 0) + slippage_rate_effective)
            execution_price = price + slippage_per_share
        else:
            effective_price = price * (
                1 - costs.get("commission_rate", 0) - slippage_rate_effective - costs.get("tax_rate", 0)
            )
            execution_price = max(0, price - slippage_per_share)

        return {
            "commission": round(commission, 0),
            "tax": round(tax, 0),
            "capital_gains_tax": round(capital_gains_tax, 0),
            "slippage": round(slippage, 0),
            "total_cost": round(total_cost, 0),
            "effective_price": round(effective_price, 0),
            "execution_price": round(execution_price, 0),
            "slippage_per_share": round(slippage_per_share, 4),
            "slippage_multiplier": round(slippage_multiplier, 2),
            "participation_rate": round(participation_rate, 6),
        }

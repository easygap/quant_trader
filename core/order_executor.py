"""
주문 실행 모듈
- 실제 주문(KIS API) 또는 페이퍼 트레이딩(시뮬레이션) 실행
- 매수/매도 주문 처리
- 거래시간 체크 / 블랙스완 감지 연동
- 주문 실패 시 재시도 (최대 3회, 지수 백오프)
- 결과를 DB에 기록
"""

import time as time_mod
from datetime import datetime
from loguru import logger

from config.config_loader import Config
from api.kis_api import KISApi
from core.risk_manager import RiskManager
from database.repositories import (
    save_trade, save_position, delete_position,
    get_position, get_all_positions,
)
from monitoring.logger import log_trade


class OrderExecutor:
    """
    주문 실행기

    - mode == "paper": 가상 매매 (로그만 기록)
    - mode == "live": 실제 KIS API 주문
    - 거래 시간 외 주문 방지
    - 블랙스완 쿨다운 중 주문 차단
    - 주문 실패 시 최대 3회 재시도
    """

    MAX_RETRIES = 3  # 주문 재시도 최대 횟수

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.mode = self.config.trading.get("mode", "paper")
        self.risk_manager = RiskManager(self.config)
        self.kis_api = KISApi()

        # 거래 시간 / 블랙스완 체크 모듈
        from core.trading_hours import TradingHours
        from core.blackswan_detector import BlackSwanDetector
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)

        if self.mode == "live":
            self.kis_api.authenticate()

        logger.info("OrderExecutor 초기화 완료 (모드: {})", self.mode)

    def execute_buy(
        self,
        symbol: str,
        price: float,
        capital: float,
        atr: float = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
    ) -> dict:
        """
        매수 주문 실행

        Args:
            symbol: 종목 코드
            price: 현재가 (매수 예정 가격)
            capital: 현재 총 자본
            atr: ATR 값 (변동성 기반 손절 시)
            signal_score: 매매 신호 점수
            reason: 매매 사유
            strategy: 전략명

        Returns:
            주문 결과 딕셔너리
        """
        # 손절가 계산
        stop_loss = self.risk_manager.calculate_stop_loss(price, atr)

        # 포지션 크기 계산 (1% 룰)
        quantity = self.risk_manager.calculate_position_size(capital, price, stop_loss)

        if quantity <= 0:
            logger.warning("종목 {} 매수 수량 0 — 주문 스킵", symbol)
            return {"success": False, "reason": "계산된 수량이 0"}

        # 분산 투자 체크
        positions = get_all_positions()
        div_check = self.risk_manager.check_diversification(
            len(positions), price * quantity, capital
        )
        if not div_check["can_buy"]:
            logger.warning("종목 {} 매수 불가: {}", symbol, div_check["reason"])
            return {"success": False, "reason": div_check["reason"]}

        # 거래 비용 계산
        costs = self.risk_manager.calculate_transaction_costs(price, quantity, "BUY")

        # 익절가 계산
        tp_info = self.risk_manager.calculate_take_profit(price)

        # 트레일링 스탑 계산
        trailing_stop = self.risk_manager.calculate_trailing_stop(price, atr)

        # 주문 전 안전 체크
        pre_check = self._pre_order_check()
        if not pre_check["allowed"]:
            return {"success": False, "reason": pre_check["reason"]}

        # 주문 실행 (재시도 포함)
        order_result = None
        if self.mode == "live":
            order_result = self._execute_with_retry(
                self.kis_api.buy_order, symbol, quantity, int(price)
            )
            if order_result is None:
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후)"}

        # DB에 매매 기록 저장
        save_trade(
            symbol=symbol,
            action="BUY",
            price=price,
            quantity=quantity,
            commission=costs["commission"],
            tax=0,
            slippage=costs["slippage"],
            strategy=strategy,
            signal_score=signal_score,
            reason=reason,
            mode=self.mode,
        )

        # 포지션 저장
        save_position(
            symbol=symbol,
            avg_price=price,
            quantity=quantity,
            stop_loss_price=stop_loss,
            take_profit_price=tp_info["target_final"],
            trailing_stop_price=trailing_stop,
            strategy=strategy,
        )

        # 매매 로그
        log_trade("BUY", symbol, price, quantity, reason)

        result = {
            "success": True,
            "symbol": symbol,
            "action": "BUY",
            "price": price,
            "quantity": quantity,
            "total_amount": price * quantity,
            "stop_loss": stop_loss,
            "take_profit": tp_info["target_final"],
            "trailing_stop": trailing_stop,
            "costs": costs,
            "mode": self.mode,
        }

        logger.info(
            "✅ 매수 완료: {} {}주 @ {:,.0f}원 | 손절={:,.0f} | 익절={:,.0f}",
            symbol, quantity, price, stop_loss, tp_info["target_final"],
        )

        return result

    def execute_sell(
        self,
        symbol: str,
        price: float,
        quantity: int = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
    ) -> dict:
        """
        매도 주문 실행

        Args:
            symbol: 종목 코드
            price: 현재가 (매도 예정 가격)
            quantity: 매도 수량 (None이면 전량 매도)
            signal_score: 매매 신호 점수
            reason: 매매 사유
            strategy: 전략명

        Returns:
            주문 결과 딕셔너리
        """
        position = get_position(symbol)
        if not position:
            logger.warning("종목 {} 보유 포지션 없음 — 매도 스킵", symbol)
            return {"success": False, "reason": "보유 포지션 없음"}

        sell_qty = quantity or position.quantity

        # 거래 비용 계산
        costs = self.risk_manager.calculate_transaction_costs(price, sell_qty, "SELL")

        # 수익률 계산
        pnl = (price - position.avg_price) * sell_qty
        pnl_rate = ((price / position.avg_price) - 1) * 100

        # 주문 전 안전 체크
        pre_check = self._pre_order_check()
        if not pre_check["allowed"]:
            return {"success": False, "reason": pre_check["reason"]}

        # 주문 실행 (재시도 포함)
        if self.mode == "live":
            order_result = self._execute_with_retry(
                self.kis_api.sell_order, symbol, sell_qty, int(price)
            )
            if order_result is None:
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후)"}

        # DB에 매매 기록 저장
        save_trade(
            symbol=symbol,
            action="SELL",
            price=price,
            quantity=sell_qty,
            commission=costs["commission"],
            tax=costs["tax"],
            slippage=costs["slippage"],
            strategy=strategy,
            signal_score=signal_score,
            reason=f"{reason} | PnL: {pnl:,.0f}원 ({pnl_rate:.2f}%)",
            mode=self.mode,
        )

        # 전량 매도 시 포지션 삭제, 부분 매도 시 수량 업데이트
        if sell_qty >= position.quantity:
            delete_position(symbol)
        else:
            # 부분 매도 — 남은 수량으로 포지션 업데이트
            remaining = position.quantity - sell_qty
            save_position(
                symbol=symbol,
                avg_price=position.avg_price,
                quantity=-sell_qty,  # save_position이 기존에 더하므로 빼기
                strategy=strategy,
            )

        # 매매 로그
        log_trade("SELL", symbol, price, sell_qty, f"{reason} (수익: {pnl_rate:.2f}%)")

        result = {
            "success": True,
            "symbol": symbol,
            "action": "SELL",
            "price": price,
            "quantity": sell_qty,
            "total_amount": price * sell_qty,
            "pnl": round(pnl, 0),
            "pnl_rate": round(pnl_rate, 2),
            "costs": costs,
            "mode": self.mode,
        }

        emoji = "📈" if pnl >= 0 else "📉"
        logger.info(
            "{} 매도 완료: {} {}주 @ {:,.0f}원 | 수익: {:,.0f}원 ({:.2f}%)",
            emoji, symbol, sell_qty, price, pnl, pnl_rate,
        )

        return result

    def check_stop_loss_take_profit(self, symbol: str, current_price: float) -> dict:
        """
        보유 종목의 손절/익절/트레일링 스탑 체크

        Args:
            symbol: 종목 코드
            current_price: 현재가

        Returns:
            {"action": "STOP_LOSS" / "TAKE_PROFIT" / "TRAILING_STOP" / None}
        """
        position = get_position(symbol)
        if not position:
            return {"action": None}

        # 트레일링 스탑 업데이트 (고점 경신 시)
        trailing_rate = self.risk_manager.risk_params.get(
            "trailing_stop", {}
        ).get("fixed_rate", 0.03)

        from database.repositories import update_trailing_stop
        update_trailing_stop(symbol, current_price, trailing_rate)

        # 손절 체크
        if position.stop_loss_price and current_price <= position.stop_loss_price:
            logger.warning(
                "🚨 손절 발동: {} 현재가={:,.0f} ≤ 손절가={:,.0f}",
                symbol, current_price, position.stop_loss_price,
            )
            return {"action": "STOP_LOSS", "price": current_price}

        # 트레일링 스탑 체크
        if position.trailing_stop_price and current_price <= position.trailing_stop_price:
            logger.warning(
                "📉 트레일링 스탑 발동: {} 현재가={:,.0f} ≤ 스탑가={:,.0f}",
                symbol, current_price, position.trailing_stop_price,
            )
            return {"action": "TRAILING_STOP", "price": current_price}

        # 익절 체크
        if position.take_profit_price and current_price >= position.take_profit_price:
            logger.info(
                "🎯 익절 도달: {} 현재가={:,.0f} ≥ 익절가={:,.0f}",
                symbol, current_price, position.take_profit_price,
            )
            return {"action": "TAKE_PROFIT", "price": current_price}

        return {"action": None}

    # =============================================================
    # 안전 체크 및 재시도
    # =============================================================

    def _pre_order_check(self) -> dict:
        """
        주문 전 안전 체크 (거래 시간 + 블랙스완)

        Returns:
            {"allowed": True/False, "reason": 사유}
        """
        # 페이퍼/백테스트 모드에서는 시간 체크 스킵
        if self.mode != "live":
            return {"allowed": True, "reason": ""}

        # 거래 시간 체크
        time_check = self.trading_hours.can_place_order()
        if not time_check["allowed"]:
            logger.warning("⏰ 주문 차단: {}", time_check["reason"])
            return time_check

        # 블랙스완 쿨다운 체크
        bs_check = self.blackswan.can_trade()
        if not bs_check["allowed"]:
            logger.warning("🚨 주문 차단: {}", bs_check["reason"])
            return bs_check

        return {"allowed": True, "reason": ""}

    def _execute_with_retry(self, order_func, *args) -> dict:
        """
        주문 함수를 최대 3회 재시도
        지수 백오프: 1초 → 2초 → 4초

        Args:
            order_func: kis_api.buy_order 또는 kis_api.sell_order
            *args: 주문 함수 인자

        Returns:
            주문 결과 또는 None (모든 재시도 실패)
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            result = order_func(*args)
            if result is not None:
                if attempt > 1:
                    logger.info("주문 성공 ({}회째 시도)", attempt)
                return result

            if attempt < self.MAX_RETRIES:
                wait = 2 ** (attempt - 1)  # 1, 2, 4초
                logger.warning(
                    "주문 실패 — {}초 후 재시도 ({}/{})",
                    wait, attempt, self.MAX_RETRIES,
                )
                time_mod.sleep(wait)

        logger.error("주문 최종 실패 ({}회 재시도 모두 실패)", self.MAX_RETRIES)
        return None

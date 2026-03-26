"""
주문 실행 모듈
- 실제 주문(KIS API) 또는 페이퍼 트레이딩(시뮬레이션) 실행
- 매수/매도 주문 처리
- 거래시간 체크 / 블랙스완 감지 연동
- 주문 실패 시 재시도 (최대 3회, 지수 백오프)
- 결과를 DB에 기록
- 재시작 복구: `reconcile_open_orders_after_crash()`로 KIS 미체결 목록 확인·로깅 (잔고 정합은 `PortfolioManager.sync_with_broker`)
"""

import time as time_mod
from datetime import datetime, timedelta
from loguru import logger

from config.config_loader import Config
from api.kis_api import KISApi
from core.risk_manager import RiskManager
from database.repositories import (
    save_trade, save_position, delete_position, reduce_position,
    get_position, get_all_positions, save_failed_order,
)
from monitoring.logger import log_trade
from core.order_guard import OrderGuard
from core.position_lock import PositionLock


class OrderExecutor:
    """
    주문 실행기

    - mode == "paper": 가상 매매 (로그만 기록)
    - mode == "live": 실제 KIS API 주문
    - account_key: 전략별 계좌 구분 (다중 계좌 시 DB·KIS 계좌 분리)
    - 거래 시간 외 주문 방지
    - 블랙스완 쿨다운 중 주문 차단
    - 주문 실패 시 최대 3회 재시도
    """

    MAX_RETRIES = 3  # 주문 재시도 최대 횟수

    def __init__(self, config: Config = None, account_key: str = ""):
        self.config = config or Config.get()
        self.account_key = account_key or ""
        self.mode = self.config.trading.get("mode", "paper")
        self.risk_manager = RiskManager(self.config)
        account_no = self.config.get_account_no(self.account_key) if self.account_key else None
        self.kis_api = KISApi(account_no=account_no)

        # 거래 시간 / 블랙스완 체크 모듈
        from core.trading_hours import TradingHours
        from core.blackswan_detector import BlackSwanDetector
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)

        self._sector_map: dict | None = None

        if self.mode == "live":
            self.kis_api.authenticate()

        logger.info("OrderExecutor 초기화 완료 (모드: {})", self.mode)

    def _get_sector_map_cached(self) -> dict:
        """업종 매핑을 한 번만 조회하고 캐시한다. 실패 시 빈 dict."""
        if self._sector_map is None:
            try:
                from core.data_collector import DataCollector
                self._sector_map = DataCollector.get_sector_map()
            except Exception:
                self._sector_map = {}
        return self._sector_map

    def execute_buy(
        self,
        symbol: str,
        price: float,
        capital: float,
        available_cash: float = None,
        current_invested: float = None,
        atr: float = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
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
            avg_daily_volume: 일평균 거래량 (제공 시 거래량 기반 동적 슬리피지 적용, 소형주 등 고슬리피지 반영)

        Returns:
            주문 결과 딕셔너리
        """
        with PositionLock():
            return self._execute_buy_impl(
                symbol, price, capital, available_cash, current_invested, atr, signal_score, reason, strategy, avg_daily_volume
            )

    def _execute_buy_impl(
        self,
        symbol: str,
        price: float,
        capital: float,
        available_cash: float = None,
        current_invested: float = None,
        atr: float = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
    ) -> dict:
        """매수 주문 실제 로직 (Lock 내부에서 호출)."""
        if self._should_block_new_buy_volatility_window():
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                "[OrderExecutor] 장 초반/마감 변동성 구간 신규 매수 차단: {} {}",
                symbol, now_str,
            )
            return {"success": False, "reason": "장 초반/마감 진입 차단 시간대"}

        # 시장 국면에 따른 손절/익절 배수 조정
        regime_sl_mult = 1.0
        regime_tp_mult = 1.0
        try:
            from core.market_regime import get_regime_adjusted_params
            regime_adj = get_regime_adjusted_params(self.config)
            regime_sl_mult = regime_adj.get("stop_loss_multiplier", 1.0)
            regime_tp_mult = regime_adj.get("take_profit_multiplier", 1.0)
            if regime_sl_mult != 1.0 or regime_tp_mult != 1.0:
                logger.info(
                    "시장 국면 [{}]: 손절×{:.2f}, 익절×{:.2f}",
                    regime_adj["regime"], regime_sl_mult, regime_tp_mult,
                )
        except Exception:
            pass

        # 손절가 계산 (국면 배수 적용)
        stop_loss = self.risk_manager.calculate_stop_loss(price, atr, regime_multiplier=regime_sl_mult)

        # 포지션 크기 계산 (1% 룰 + 신호 강도 스케일링)
        quantity = self.risk_manager.calculate_position_size(
            capital, price, stop_loss, signal_score=signal_score,
        )

        if quantity <= 0:
            logger.warning("종목 {} 매수 수량 0 — 주문 스킵", symbol)
            return {"success": False, "reason": "계산된 수량이 0"}

        # 상관관계 기반 포지션 축소
        positions = get_all_positions(account_key=self.account_key if self.account_key else None)
        existing_symbols = [p.symbol for p in positions]
        corr_result = self.risk_manager.check_correlation_risk(symbol, existing_symbols)
        if corr_result["scale"] < 1.0:
            scaled_qty = max(1, int(quantity * corr_result["scale"]))
            logger.info(
                "종목 {} 상관관계 축소: {}주 → {}주 ({})",
                symbol, quantity, scaled_qty, corr_result["reason"],
            )
            quantity = scaled_qty

        # 갭 리스크 체크: 당일 시가가 전일 종가 대비 큰 폭 갭업이면 매수 차단
        gap_cfg = (self.config.risk_params or {}).get("gap_risk", {})
        if gap_cfg.get("enabled", False) and gap_cfg.get("gap_up_entry_block", 0) > 0:
            try:
                from core.data_collector import DataCollector
                df_recent = DataCollector().fetch_stock(symbol)
                if df_recent is not None and len(df_recent) >= 2:
                    prev_close = float(df_recent["close"].iloc[-2])
                    today_open = float(df_recent["open"].iloc[-1]) if "open" in df_recent.columns else price
                    if prev_close > 0:
                        gap_pct = (today_open - prev_close) / prev_close
                        if gap_pct >= gap_cfg["gap_up_entry_block"]:
                            msg = f"갭업 +{gap_pct*100:.1f}% (기준 +{gap_cfg['gap_up_entry_block']*100:.0f}%) — 추격매수 차단"
                            logger.warning("종목 {} 매수 스킵: {}", symbol, msg)
                            return {"success": False, "reason": msg}
            except Exception as e:
                logger.debug("갭 리스크 체크 스킵: {}", e)

        # 실적 발표일 필터: 전후 N일 이내이면 신규 매수 금지
        skip_earnings_days = int(self.config.trading.get("skip_earnings_days", 0))
        if skip_earnings_days > 0:
            from core.earnings_filter import is_near_earnings
            near, reason_earn = is_near_earnings(symbol, skip_days=skip_earnings_days)
            if near:
                logger.warning("종목 {} 매수 스킵 (실적 필터): {}", symbol, reason_earn)
                return {"success": False, "reason": reason_earn}

        # 진입 시점 유동성 재검증: watchlist 구축 이후 유동성이 변했을 수 있음
        liq = (self.config.risk_params or {}).get("liquidity_filter") or {}
        if liq.get("enabled", False) and liq.get("check_on_entry", True):
            min_krw = float(liq.get("min_avg_trading_value_20d_krw", 5e9))
            if avg_daily_volume is not None and price > 0:
                est_trading_value = avg_daily_volume * price
                if est_trading_value < min_krw:
                    msg = (
                        f"유동성 부족: 추정 일평균 거래대금 {est_trading_value/1e8:.0f}억 원 "
                        f"< 하한 {min_krw/1e8:.0f}억 원"
                    )
                    logger.warning("종목 {} 매수 스킵 (유동성): {}", symbol, msg)
                    return {"success": False, "reason": msg}

        # 분산 투자 체크 (업종 비중 포함, 상관 체크에서 이미 조회한 positions 재활용)
        sector_map = self._get_sector_map_cached()
        div_check = self.risk_manager.check_diversification(
            len(positions),
            price * quantity,
            capital,
            available_cash=available_cash,
            current_invested=current_invested or 0,
            symbol=symbol,
            sector_map=sector_map,
            positions=positions,
        )
        if not div_check["can_buy"]:
            logger.warning("종목 {} 매수 불가: {}", symbol, div_check["reason"])
            return {"success": False, "reason": div_check["reason"]}

        # 전략 성과 열화 감지 (최근 N건 승률 하한)
        from database.repositories import get_recent_sell_trades
        recent_sells = get_recent_sell_trades(
            limit=self.risk_manager.risk_params.get("performance_degradation", {}).get("recent_trades", 20),
            mode=self.mode,
            account_key=self.account_key if self.account_key else None,
        )
        perf_check = self.risk_manager.check_recent_performance(recent_sells)
        if not perf_check.get("allowed", True):
            logger.warning("종목 {} 매수 불가 (성과 열화): {}", symbol, perf_check.get("reason", ""))
            return {"success": False, "reason": perf_check.get("reason", "성과 열화로 매수 중단")}

        # 거래 비용 계산 (avg_daily_volume 있으면 거래량 기반 동적 슬리피지 적용)
        costs = self.risk_manager.calculate_transaction_costs(price, quantity, "BUY", avg_daily_volume=avg_daily_volume)
        available_cash = capital if available_cash is None else available_cash

        # 음수 캐시 방지: 가용 현금이 0 이하이면 즉시 거부
        if available_cash <= 0:
            logger.warning(
                "종목 {} 매수 불가: 가용 현금 {:,.0f}원 (0 이하)",
                symbol, available_cash,
            )
            return {"success": False, "reason": "가용 현금 부족 (0 이하)"}

        total_required = (price * quantity) + costs["total_cost"]
        if total_required > available_cash:
            logger.warning(
                "종목 {} 매수 불가: 필요 현금 {:,.0f}원 > 사용 가능 현금 {:,.0f}원",
                symbol, total_required, available_cash,
            )
            return {"success": False, "reason": "사용 가능 현금 부족"}

        # 익절가 계산 (국면 배수 적용)
        tp_info = self.risk_manager.calculate_take_profit(price, regime_multiplier=regime_tp_mult)

        # 트레일링 스탑 계산
        trailing_stop = self.risk_manager.calculate_trailing_stop(price, atr)

        # 주문 전 안전 체크
        pre_check = self._pre_order_check()
        if not pre_check["allowed"]:
            return {"success": False, "reason": pre_check["reason"]}

        # 주문 실행 (재시도 포함)
        order_result = None
        if self.mode == "live":
            # 중복 주문 방지: ① 앱 레벨 TTL 가드 ② 해당 종목 미체결 주문 존재 여부(KIS 조회)
            ttl_seconds = int(self.config.trading.get("pending_order_ttl_seconds", 600))
            if OrderGuard.has_pending(symbol):
                reason_text = f"{symbol} 종목에 미체결/최근 주문이 남아 있어 중복 주문을 차단했습니다."
                logger.warning(reason_text)
                return {"success": False, "reason": reason_text}
            if self.kis_api and self.kis_api.has_unfilled_orders(symbol):
                reason_text = "해당 종목 미체결 주문이 있어 중복 주문을 보류했습니다."
                return {"success": False, "reason": reason_text}
            order_result = self._execute_with_retry(
                self.kis_api.buy_order, symbol, quantity, int(price),
                symbol=symbol, action="BUY", price=price, quantity=quantity,
                strategy=strategy, signal_score=signal_score, reason=reason,
            )
            if order_result is None:
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후, dead-letter 저장됨)"}
            OrderGuard.mark_pending(symbol, ttl_seconds=ttl_seconds)

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
            account_key=self.account_key,
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
            account_key=self.account_key,
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
        avg_daily_volume: float = None,
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
            avg_daily_volume: 일평균 거래량 (제공 시 거래량 기반 동적 슬리피지 적용)

        Returns:
            주문 결과 딕셔너리
        """
        with PositionLock():
            return self._execute_sell_impl(symbol, price, quantity, signal_score, reason, strategy, avg_daily_volume)

    def _execute_sell_impl(
        self,
        symbol: str,
        price: float,
        quantity: int = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
    ) -> dict:
        """매도 주문 실제 로직 (Lock 내부에서 호출)."""
        position = get_position(symbol, account_key=self.account_key)
        if not position:
            logger.warning("종목 {} 보유 포지션 없음 — 매도 스킵", symbol)
            return {"success": False, "reason": "보유 포지션 없음"}

        # 최소 보유 기간 검사 (손절/블랙스완은 예외)
        is_emergency = reason in ("STOP_LOSS", "블랙스완 긴급 매도", "TRAILING_STOP")
        if not is_emergency:
            min_hold = self._get_min_holding_days()
            if min_hold > 0 and getattr(position, "bought_at", None):
                bought_date = position.bought_at.date() if hasattr(position.bought_at, "date") else position.bought_at
                holding_days = (datetime.now().date() - bought_date).days
                if holding_days < min_hold:
                    msg = f"최소 보유 기간 미달 ({holding_days}/{min_hold}일)"
                    logger.info("종목 {} 매도 스킵: {}", symbol, msg)
                    return {"success": False, "reason": msg}

        sell_qty = quantity or position.quantity

        # 거래 비용 계산 (증권거래세+농특세 0.20% + 설정 시 양도소득세; avg_price로 양도소득세 계산)
        costs = self.risk_manager.calculate_transaction_costs(
            price, sell_qty, "SELL",
            avg_daily_volume=avg_daily_volume,
            avg_price=float(position.avg_price),
        )
        total_tax = costs["tax"] + costs.get("capital_gains_tax", 0)

        # 수익률 계산 (세금·수수료 반영 후 순손익)
        pnl = (price - position.avg_price) * sell_qty - costs["commission"] - total_tax
        pnl_rate = ((price / position.avg_price) - 1) * 100

        # 주문 전 안전 체크 (매도: 쿨다운 중에도 허용)
        pre_check = self._pre_order_check(action="SELL")
        if not pre_check["allowed"]:
            return {"success": False, "reason": pre_check["reason"]}

        # 주문 실행 (재시도 포함)
        if self.mode == "live":
            # 중복 주문 방지: ① 앱 레벨 TTL 가드 ② 해당 종목 미체결 주문 존재 여부(KIS 조회)
            ttl_seconds = int(self.config.trading.get("pending_order_ttl_seconds", 600))
            if OrderGuard.has_pending(symbol):
                reason_text = f"{symbol} 종목에 미체결/최근 주문이 남아 있어 중복 주문을 차단했습니다."
                logger.warning(reason_text)
                return {"success": False, "reason": reason_text}
            if self.kis_api and self.kis_api.has_unfilled_orders(symbol):
                reason_text = "해당 종목 미체결 주문이 있어 중복 주문을 보류했습니다."
                return {"success": False, "reason": reason_text}
            order_result = self._execute_with_retry(
                self.kis_api.sell_order, symbol, sell_qty, int(price),
                symbol=symbol, action="SELL", price=price, quantity=sell_qty,
                strategy=strategy, signal_score=signal_score, reason=reason,
            )
            if order_result is None:
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후, dead-letter 저장됨)"}
            OrderGuard.mark_pending(symbol, ttl_seconds=ttl_seconds)

        # DB에 매매 기록 저장 (tax = 증권거래세 + 양도소득세)
        save_trade(
            symbol=symbol,
            action="SELL",
            price=price,
            quantity=sell_qty,
            commission=costs["commission"],
            tax=total_tax,
            slippage=costs["slippage"],
            strategy=strategy,
            signal_score=signal_score,
            reason=f"{reason} | PnL: {pnl:,.0f}원 ({pnl_rate:.2f}%)",
            mode=self.mode,
            account_key=self.account_key,
        )

        # 전량 매도 시 포지션 삭제, 부분 매도 시 reduce_position + 손절/익절 재조정
        if sell_qty >= position.quantity:
            delete_position(symbol, account_key=self.account_key)
        else:
            remaining_pos = reduce_position(symbol, sell_qty, account_key=self.account_key)
            # 부분 익절 후 잔여 포지션의 익절가를 최종 목표가로 승격
            if remaining_pos and reason == "TAKE_PROFIT_PARTIAL":
                from database.repositories import update_position_targets
                tp_config = self.risk_manager.risk_params.get("take_profit", {})
                final_target = position.avg_price * (1 + tp_config.get("fixed_rate", 0.08))
                update_position_targets(
                    symbol,
                    take_profit_price=round(final_target, 0),
                    account_key=self.account_key,
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
        보유 종목의 손절/익절/트레일링 스탑 체크.
        PositionLock 내부에서 실행하여 execute_sell과의 이중 매도 경합을 방지합니다.

        체크 순서: 익절(TP) → 부분 익절(TP1) → 트레일링 스탑(TS) → 손절(SL)
        이익 실현을 우선하여 수익 보호를 극대화한다.

        Args:
            symbol: 종목 코드
            current_price: 현재가

        Returns:
            {"action": "STOP_LOSS" / "TAKE_PROFIT" / "TAKE_PROFIT_PARTIAL" / "TRAILING_STOP" / None,
             "price": 현재가, "partial_ratio": 부분 매도 비율 (부분 익절 시)}
        """
        with PositionLock():
            return self._check_stop_loss_take_profit_impl(symbol, current_price)

    def _check_stop_loss_take_profit_impl(self, symbol: str, current_price: float) -> dict:
        """SL/TP 실제 로직 (Lock 내부에서 호출)."""
        position = get_position(symbol, account_key=self.account_key)
        if not position:
            return {"action": None}

        # 트레일링 스탑 업데이트 (고점 경신 시)
        trailing_rate = self.risk_manager.risk_params.get(
            "trailing_stop", {}
        ).get("fixed_rate", 0.03)

        from database.repositories import update_trailing_stop
        update_trailing_stop(symbol, current_price, trailing_rate, account_key=self.account_key)

        # 1. 익절 체크 (최종 목표가 도달 → 전량 매도)
        if position.take_profit_price and current_price >= position.take_profit_price:
            logger.info(
                "🎯 익절 도달: {} 현재가={:,.0f} ≥ 익절가={:,.0f}",
                symbol, current_price, position.take_profit_price,
            )
            return {"action": "TAKE_PROFIT", "price": current_price}

        # 2. 부분 익절 체크 (1차 목표 도달 → partial_ratio만큼 매도)
        tp_config = self.risk_manager.risk_params.get("take_profit", {})
        if tp_config.get("partial_exit", False):
            partial_target_rate = tp_config.get("partial_target", 0.04)
            partial_ratio = tp_config.get("partial_ratio", 0.5)
            partial_target_price = position.avg_price * (1 + partial_target_rate)
            if (
                current_price >= partial_target_price
                and position.quantity >= 2  # 1주면 부분 매도 불가
                and not getattr(position, "_partial_tp_done", False)
            ):
                partial_qty = max(1, int(position.quantity * partial_ratio))
                if partial_qty < position.quantity:
                    logger.info(
                        "🎯 부분 익절 도달: {} 현재가={:,.0f} ≥ 1차목표={:,.0f} ({}주 중 {}주 매도)",
                        symbol, current_price, partial_target_price, position.quantity, partial_qty,
                    )
                    return {
                        "action": "TAKE_PROFIT_PARTIAL",
                        "price": current_price,
                        "partial_qty": partial_qty,
                    }

        # 3. 트레일링 스탑 체크 (이익 보호)
        # position 재조회 (trailing_stop_price가 업데이트되었을 수 있음)
        position = get_position(symbol, account_key=self.account_key)
        if position and position.trailing_stop_price and current_price <= position.trailing_stop_price:
            logger.warning(
                "📉 트레일링 스탑 발동: {} 현재가={:,.0f} ≤ 스탑가={:,.0f}",
                symbol, current_price, position.trailing_stop_price,
            )
            return {"action": "TRAILING_STOP", "price": current_price}

        # 4. 손절 체크 (최후의 손실 제한)
        if position and position.stop_loss_price and current_price <= position.stop_loss_price:
            logger.warning(
                "🚨 손절 발동: {} 현재가={:,.0f} ≤ 손절가={:,.0f}",
                symbol, current_price, position.stop_loss_price,
            )
            return {"action": "STOP_LOSS", "price": current_price}

        return {"action": None}

    def _get_min_holding_days(self) -> int:
        """risk_params.yaml의 최소 보유 기간(일) 반환. 미설정 시 0."""
        return int(
            (self.config.risk_params.get("position_limits") or {})
            .get("min_holding_days", 0)
        )

    def _should_block_new_buy_volatility_window(self) -> bool:
        """
        장 시작 직후·종료 직전 구간 신규 매수 차단 (settings.trading).
        거래일이 아니면 적용하지 않음. 매도(손절·익절·트레일링 등)는 이 경로를 쓰지 않음.
        """
        tcfg = self.config.trading or {}
        block_open = bool(tcfg.get("block_open_30min", True))
        block_close = bool(tcfg.get("block_close_30min", True))
        if not block_open and not block_close:
            return False
        if not self.trading_hours.is_trading_day():
            return False

        now = datetime.now()
        ct = now.time()
        mo = self.trading_hours.market_open
        mc = self.trading_hours.market_close

        if block_open:
            open_end = (datetime.combine(now.date(), mo) + timedelta(minutes=30)).time()
            if mo <= ct < open_end:
                return True

        if block_close:
            close_start = (datetime.combine(now.date(), mc) - timedelta(minutes=30)).time()
            if close_start <= ct <= mc:
                return True

        return False

    # =============================================================
    # 안전 체크 및 재시도
    # =============================================================

    def _pre_order_check(self, action: str = "BUY") -> dict:
        """
        주문 전 안전 체크 (거래 시간 + 블랙스완)

        Args:
            action: "BUY" 또는 "SELL". 쿨다운 중에도 매도는 허용.

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

        # 블랙스완 쿨다운 체크 (매도는 쿨다운 중에도 허용)
        bs_check = self.blackswan.can_trade(action=action)
        if not bs_check["allowed"]:
            logger.warning("🚨 주문 차단: {}", bs_check["reason"])
            return bs_check

        return {"allowed": True, "reason": ""}

    def _execute_with_retry(
        self,
        order_func,
        *args,
        symbol: str = "",
        action: str = "",
        price: float = 0,
        quantity: int = 0,
        strategy: str = "",
        signal_score: float = 0,
        reason: str = "",
    ) -> dict:
        """
        주문 함수를 최대 3회 재시도 (지수 백오프: 1초 → 2초 → 4초).
        모든 재시도 실패 시 dead-letter 테이블에 저장하여 주문 누락을 방지합니다.
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            result = order_func(*args)
            if result is not None:
                if attempt > 1:
                    logger.info("주문 성공 ({}회째 시도)", attempt)
                return result

            if attempt < self.MAX_RETRIES:
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "주문 실패 — {}초 후 재시도 ({}/{})",
                    wait, attempt, self.MAX_RETRIES,
                )
                time_mod.sleep(wait)

        logger.error("주문 최종 실패 ({}회 재시도 모두 실패)", self.MAX_RETRIES)

        if symbol and action:
            save_failed_order(
                symbol=symbol,
                action=action,
                price=price,
                quantity=quantity,
                reason=reason or "KIS API 주문 실패",
                strategy=strategy,
                signal_score=signal_score,
                retry_count=self.MAX_RETRIES,
                mode=self.mode,
                error_detail=f"{self.MAX_RETRIES}회 재시도 모두 실패",
                account_key=self.account_key,
            )

        return None

    def reconcile_open_orders_after_crash(self) -> list[dict]:
        """
        프로세스 재시작 직후: KIS 당일 미체결 주문을 조회해 건별 로깅한다.
        체결 완료분은 증권사 잔고에 반영되므로, 이후 `PortfolioManager.sync_with_broker(auto_correct=True)`로
        DB 포지션을 잔고에 맞추면 정합성이 맞춰진다(미체결 행 자체는 trades 테이블에 자동 삽입하지 않음).
        """
        if self.mode != "live":
            return []
        try:
            if self.kis_api and not getattr(self.kis_api, "_access_token", None):
                self.kis_api.authenticate()
        except Exception as e:
            logger.warning("[복구] KIS 인증 실패 — 미체결 조회 생략: {}", e)
            return []
        try:
            open_orders = self.kis_api.get_open_orders()
        except Exception as e:
            logger.warning("[복구] get_open_orders 실패: {}", e)
            return []
        for o in open_orders:
            logger.info(
                "[복구] 미체결 유지: {} {}주 매매구분={} 주문가={} 주문번호={}",
                o.get("symbol"),
                o.get("remaining_qty"),
                o.get("buy_sell"),
                o.get("order_price"),
                o.get("order_no"),
            )
        return open_orders

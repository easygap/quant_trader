"""
블랙스완 감지기
- 급락 감지 및 긴급 전량 매도
- 시장 전체 이상 탐지
- 쿨다운 매매 중단
"""

from datetime import datetime, timedelta
from loguru import logger

from config.config_loader import Config


class BlackSwanDetector:
    """
    블랙스완(급락) 감지기

    감지 조건:
    - 개별 종목: 전일 대비 -5% 이상 급락
    - 포트폴리오: 일일 손실 -3% 이상
    - 연속 급락: 3일 연속 -2% 이상

    발동 시:
    - 전 종목 긴급 매도 명령
    - 디스코드 경고 알림
    - 쿨다운 매매 중단 (기본 1시간)

    사용법:
        detector = BlackSwanDetector()
        result = detector.check(current_prices, prev_prices)
        if result["triggered"]:
            # 긴급 매도 실행
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()

        # 블랙스완 감지 임계값
        self.single_stock_threshold = -0.05    # 개별 종목 -5%
        self.portfolio_threshold = -0.03       # 포트폴리오 -3%
        self.consecutive_days = 3              # 연속 하락 일수
        self.consecutive_threshold = -0.02     # 연속 하락 기준 -2%

        # 쿨다운 관리
        self.cooldown_minutes = 60             # 기본 1시간 매매 중단
        self._cooldown_until = None            # 매매 재개 시각
        self._triggered_count = 0              # 발동 횟수

        # 연속 하락 추적
        self._daily_returns: list[float] = []

        logger.info("BlackSwanDetector 초기화 완료")

    def check_stock(self, symbol: str, current_price: float, prev_close: float) -> dict:
        """
        개별 종목 급락 감지

        Args:
            symbol: 종목 코드
            current_price: 현재가
            prev_close: 전일 종가

        Returns:
            {"triggered": bool, "change_rate": 등락률, "reason": 사유}
        """
        if prev_close <= 0:
            return {"triggered": False, "change_rate": 0, "reason": ""}

        change_rate = (current_price - prev_close) / prev_close

        if change_rate <= self.single_stock_threshold:
            reason = (
                f"🚨 [{symbol}] 급락 감지! "
                f"{change_rate*100:.2f}% (임계: {self.single_stock_threshold*100:.0f}%)"
            )
            logger.warning(reason)
            self._activate_cooldown()
            return {"triggered": True, "change_rate": change_rate, "reason": reason}

        return {"triggered": False, "change_rate": change_rate, "reason": ""}

    def check_portfolio(self, current_value: float, prev_value: float) -> dict:
        """
        포트폴리오 전체 급락 감지

        Args:
            current_value: 현재 포트폴리오 평가금
            prev_value: 전일 포트폴리오 평가금

        Returns:
            {"triggered": bool, "change_rate": 등락률, "reason": 사유}
        """
        if prev_value <= 0:
            return {"triggered": False, "change_rate": 0, "reason": ""}

        change_rate = (current_value - prev_value) / prev_value

        # 연속 하락 추적
        self._daily_returns.append(change_rate)
        if len(self._daily_returns) > self.consecutive_days:
            self._daily_returns = self._daily_returns[-self.consecutive_days:]

        # 포트폴리오 급락 체크
        if change_rate <= self.portfolio_threshold:
            reason = (
                f"🚨 포트폴리오 급락! "
                f"{change_rate*100:.2f}% (임계: {self.portfolio_threshold*100:.0f}%)"
            )
            logger.warning(reason)
            self._activate_cooldown()
            return {"triggered": True, "change_rate": change_rate, "reason": reason}

        # 연속 하락 체크
        if len(self._daily_returns) >= self.consecutive_days:
            all_down = all(
                r <= self.consecutive_threshold
                for r in self._daily_returns[-self.consecutive_days:]
            )
            if all_down:
                reason = (
                    f"🚨 {self.consecutive_days}일 연속 하락! "
                    f"최근 수익률: {[f'{r*100:.1f}%' for r in self._daily_returns[-self.consecutive_days:]]}"
                )
                logger.warning(reason)
                self._activate_cooldown()
                return {"triggered": True, "change_rate": change_rate, "reason": reason}

        return {"triggered": False, "change_rate": change_rate, "reason": ""}

    def is_on_cooldown(self) -> bool:
        """현재 쿨다운(매매 중단) 상태인지 확인"""
        if self._cooldown_until is None:
            return False

        if datetime.now() < self._cooldown_until:
            remaining = self._cooldown_until - datetime.now()
            logger.debug("쿨다운 중 — 남은 시간: {}", remaining)
            return True

        # 쿨다운 해제
        logger.info("블랙스완 쿨다운 해제 — 매매 재개")
        self._cooldown_until = None
        return False

    def can_trade(self) -> dict:
        """
        매매 가능 여부 (주문 실행 전 호출)

        Returns:
            {"allowed": True/False, "reason": 사유}
        """
        if self.is_on_cooldown():
            remaining = self._cooldown_until - datetime.now()
            return {
                "allowed": False,
                "reason": f"블랙스완 쿨다운 중 (남은 시간: {remaining})",
            }
        return {"allowed": True, "reason": ""}

    def _activate_cooldown(self):
        """쿨다운 활성화"""
        self._triggered_count += 1
        # 반복 발동 시 쿨다운 시간 증가 (최대 4시간)
        cooldown = min(self.cooldown_minutes * self._triggered_count, 240)
        self._cooldown_until = datetime.now() + timedelta(minutes=cooldown)
        logger.warning(
            "⏸️ 매매 중단 — {}분 쿨다운 (발동 횟수: {})",
            cooldown, self._triggered_count,
        )

    def get_emergency_sell_list(self, positions: list) -> list:
        """
        긴급 전량 매도 대상 종목 반환

        Args:
            positions: 현재 보유 포지션 리스트

        Returns:
            매도할 종목 리스트 [{"symbol": 코드, "quantity": 수량}]
        """
        sell_list = []
        for pos in positions:
            sell_list.append({
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "reason": "블랙스완 긴급 전량 매도",
            })
        logger.warning("🚨 긴급 매도 대상: {}개 종목", len(sell_list))
        return sell_list

    def reset(self):
        """감지기 초기화 (수동)"""
        self._cooldown_until = None
        self._triggered_count = 0
        self._daily_returns.clear()
        logger.info("BlackSwanDetector 초기화 완료")

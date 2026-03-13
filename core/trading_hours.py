"""
거래 시간 관리 모듈
- 장 운영 시간 확인
- 공휴일/주말 판별
- 주문 실행 전 시간 검증
"""

from datetime import datetime, time, timedelta
from loguru import logger

from config.config_loader import Config

# 한국 증시 공휴일 (임시 — 실제 운영 시 매년 업데이트 필요)
# 2026년 한국 공휴일 (주식시장 휴장일)
KR_HOLIDAYS_2026 = {
    "2026-01-01",  # 신정
    "2026-01-27",  # 설날 연휴
    "2026-01-28",  # 설날
    "2026-01-29",  # 설날 연휴
    "2026-03-01",  # 삼일절
    "2026-05-05",  # 어린이날
    "2026-05-24",  # 석가탄신일
    "2026-06-06",  # 현충일
    "2026-08-15",  # 광복절
    "2026-09-24",  # 추석 연휴
    "2026-09-25",  # 추석
    "2026-09-26",  # 추석 연휴
    "2026-10-03",  # 개천절
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
}


class TradingHours:
    """
    거래 시간 관리

    사용법:
        th = TradingHours()
        if th.is_market_open():
            # 주문 실행
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        trading = self.config.trading

        # 장 운영 시간 파싱
        open_str = trading.get("market_open", "09:00")
        close_str = trading.get("market_close", "15:30")
        prep_str = trading.get("pre_market_prep", "08:50")

        self.market_open = time(*map(int, open_str.split(":")))
        self.market_close = time(*map(int, close_str.split(":")))
        self.pre_market = time(*map(int, prep_str.split(":")))

        # 공휴일 세트
        self.holidays = KR_HOLIDAYS_2026

        logger.info(
            "TradingHours 초기화 (장: {} ~ {}, 준비: {})",
            open_str, close_str, prep_str,
        )

    def is_trading_day(self, date: datetime = None) -> bool:
        """
        거래일인지 확인 (주말, 공휴일 제외)

        Args:
            date: 확인할 날짜 (None이면 오늘)

        Returns:
            거래일이면 True
        """
        dt = date or datetime.now()

        # 주말 체크 (토=5, 일=6)
        if dt.weekday() >= 5:
            return False

        # 공휴일 체크
        date_str = dt.strftime("%Y-%m-%d")
        if date_str in self.holidays:
            return False

        return True

    def is_market_open(self, dt: datetime = None) -> bool:
        """
        현재 장이 열려있는지 확인

        Returns:
            True: 현재 거래 가능
        """
        dt = dt or datetime.now()

        if not self.is_trading_day(dt):
            return False

        current_time = dt.time()
        return self.market_open <= current_time <= self.market_close

    def is_pre_market(self, dt: datetime = None) -> bool:
        """장전 준비 시간인지 확인"""
        dt = dt or datetime.now()

        if not self.is_trading_day(dt):
            return False

        current_time = dt.time()
        return self.pre_market <= current_time < self.market_open

    def can_place_order(self, dt: datetime = None) -> dict:
        """
        주문 가능 여부 확인 (주문 실행 전 호출)

        Returns:
            {"allowed": True/False, "reason": 사유}
        """
        dt = dt or datetime.now()

        if not self.is_trading_day(dt):
            weekday = dt.strftime("%A")
            return {"allowed": False, "reason": f"거래일 아님 ({weekday})"}

        if not self.is_market_open(dt):
            current = dt.strftime("%H:%M")
            return {
                "allowed": False,
                "reason": f"장 운영 시간 외 (현재: {current}, "
                          f"장: {self.market_open}~{self.market_close})",
            }

        return {"allowed": True, "reason": ""}

    def time_until_market_open(self) -> timedelta:
        """장 시작까지 남은 시간"""
        now = datetime.now()
        today_open = datetime.combine(now.date(), self.market_open)

        if now < today_open:
            return today_open - now

        # 이미 지났으면 다음 거래일 계산
        next_day = now + timedelta(days=1)
        while not self.is_trading_day(next_day):
            next_day += timedelta(days=1)

        next_open = datetime.combine(next_day.date(), self.market_open)
        return next_open - now

    def time_until_market_close(self) -> timedelta:
        """장 종료까지 남은 시간"""
        now = datetime.now()
        today_close = datetime.combine(now.date(), self.market_close)

        if now < today_close:
            return today_close - now

        return timedelta(0)

"""
거래 시간 관리 모듈
- 장 운영 시간 확인
- 공휴일/주말 판별 (pykrx 또는 holidays.yaml 동적 로드, 최종 fallback 하드코딩)
- 주문 실행 전 시간 검증
"""

from datetime import datetime, time, timedelta
from pathlib import Path
from loguru import logger

from config.config_loader import Config

# 프로젝트 루트 (config 상위)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 한국 증시 공휴일 — 최종 fallback (네트워크/파일 불가 시)
KR_HOLIDAYS_FALLBACK = {
    "2026-01-01", "2026-01-27", "2026-01-28", "2026-01-29",
    "2026-03-01", "2026-05-05", "2026-05-24", "2026-06-06",
    "2026-08-15", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-03", "2026-10-09", "2026-12-25",
}


def _load_holidays() -> set:
    """
    공휴일 세트 로드: 1) config/holidays.yaml 2) pykrx 3) 하드코딩 fallback.
    holidays.yaml이 없으면 자동 생성 시도(pykrx+fallback) 후 재로드.
    """
    import yaml
    holidays_path = _PROJECT_ROOT / "config" / "holidays.yaml"

    # 파일 없으면 자동 갱신 시도 (매년 수동 관리 부담 감소)
    if not holidays_path.exists():
        try:
            from core.holidays_updater import update_holidays_yaml
            update_holidays_yaml(path=holidays_path)
        except Exception as e:
            logger.debug("휴장일 파일 자동 생성 스킵: {}", e)

    if holidays_path.exists():
        try:
            with open(holidays_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            lst = data.get("holidays", data.get("dates", []))
            if isinstance(lst, list):
                out = {str(d) for d in lst}
                logger.info("공휴일 로드: config/holidays.yaml ({}일)", len(out))
                return out
        except Exception as e:
            logger.warning("holidays.yaml 로드 실패 — fallback: {}", e)

    # 2) pykrx (거래일 제외 = 휴장일)
    try:
        from pykrx import stock
        now = datetime.now()
        start = f"{now.year}0101"
        end = f"{now.year + 1}1231"
        trading = stock.get_market_trading_date_by_date(start, end)
        if trading is not None and not trading.empty:
            from datetime import date
            all_days = set()
            d = date(now.year, 1, 1)
            end_d = date(now.year + 1, 12, 31)
            while d <= end_d:
                all_days.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            trading_dates = set(trading.index.strftime("%Y-%m-%d").tolist())
            weekends = set()
            d = date(now.year, 1, 1)
            while d <= end_d:
                if d.weekday() >= 5:
                    weekends.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            out = all_days - trading_dates - weekends
            logger.info("공휴일 로드: pykrx ({}일)", len(out))
            return out
    except ImportError:
        logger.debug("pykrx 미설치 — 공휴일 하드코딩 fallback 사용")
    except Exception as e:
        logger.warning("pykrx 공휴일 조회 실패 — fallback: {}", e)

    # 3) 하드코딩
    logger.info("공휴일 로드: 하드코딩 fallback ({}일)", len(KR_HOLIDAYS_FALLBACK))
    return KR_HOLIDAYS_FALLBACK.copy()


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

        # 공휴일 세트 (동적 로드)
        self.holidays = _load_holidays()

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

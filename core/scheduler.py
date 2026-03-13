"""
자동 스케줄러 모듈
- 장전 준비 → 장중 모니터링 → 장마감 리포트 사이클 자동 실행
- 무한 루프 기반 (Ctrl+C로 종료)
"""

import time as time_mod
from datetime import datetime, timedelta
from loguru import logger

from config.config_loader import Config
from core.trading_hours import TradingHours
from core.blackswan_detector import BlackSwanDetector
from core.portfolio_manager import PortfolioManager
from monitoring.discord_bot import DiscordBot
from database.repositories import get_all_positions


class Scheduler:
    """
    자동 매매 스케줄러

    3가지 단계를 자동으로 반복합니다:
    1. 장전 준비 (08:50): 데이터 수집, 지표 계산, 관심 종목 분석
    2. 장중 모니터링 (09:00~15:30): 10분 간격 신호 확인, 손절/익절 체크
    3. 장마감 (15:35): 일일 리포트, 포트폴리오 스냅샷 저장

    사용법:
        scheduler = Scheduler(strategy_name="scoring")
        scheduler.run()  # 무한 루프 시작
    """

    def __init__(self, strategy_name: str = "scoring", config: Config = None):
        self.config = config or Config.get()
        self.strategy_name = strategy_name
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)
        self.portfolio = PortfolioManager(self.config)
        self.discord = DiscordBot(self.config)

        # 모니터링 간격 (초)
        self.monitor_interval = 600  # 10분

        # 상태 추적
        self._pre_market_done = False
        self._post_market_done = False
        self._last_monitor_time = None
        self._today = None

        logger.info("Scheduler 초기화 (전략: {}, 모니터링 간격: {}초)", strategy_name, self.monitor_interval)

    def run(self):
        """
        메인 루프 — 무한 반복으로 장 시간에 맞춰 자동 실행

        Ctrl+C로 종료합니다.
        """
        logger.info("🚀 자동 스케줄러 시작 (전략: {})", self.strategy_name)
        self.discord.send_message(f"🚀 퀀트 트레이더 스케줄러 시작\n전략: {self.strategy_name}")

        try:
            while True:
                now = datetime.now()

                # 날짜가 바뀌면 상태 초기화
                today = now.date()
                if self._today != today:
                    self._today = today
                    self._pre_market_done = False
                    self._post_market_done = False
                    self._last_monitor_time = None
                    logger.info("📅 새로운 거래일: {}", today)

                # 비거래일이면 대기
                if not self.trading_hours.is_trading_day():
                    self._sleep_until_next_trading_day()
                    continue

                # 1단계: 장전 준비 (08:50 ~ 09:00)
                if self.trading_hours.is_pre_market() and not self._pre_market_done:
                    self._run_pre_market()
                    self._pre_market_done = True

                # 2단계: 장중 모니터링 (09:00 ~ 15:30)
                elif self.trading_hours.is_market_open():
                    if self._should_monitor():
                        self._run_monitoring()
                        self._last_monitor_time = now

                # 3단계: 장마감 리포트 (15:30 이후)
                elif now.hour == 15 and now.minute >= 35 and not self._post_market_done:
                    self._run_post_market()
                    self._post_market_done = True

                # 대기 (30초 간격)
                time_mod.sleep(30)

        except KeyboardInterrupt:
            logger.info("⏹️ 스케줄러 종료 (Ctrl+C)")
            self.discord.send_message("⏹️ 퀀트 트레이더 스케줄러 종료")

    def _run_pre_market(self):
        """장전 준비: 데이터 수집 + 전략 분석"""
        logger.info("=" * 50)
        logger.info("📋 장전 준비 시작 ({})", datetime.now().strftime("%H:%M:%S"))
        logger.info("=" * 50)

        try:
            from core.data_collector import DataCollector
            collector = DataCollector()
            strategy = self._get_strategy()
            watchlist = self.config.watchlist or ["005930"]

            signals = []
            for symbol in watchlist:
                try:
                    df = collector.fetch_korean_stock(symbol)
                    if df.empty or len(df) < 30:
                        continue

                    signal_info = strategy.generate_signal(df)
                    signal_info["symbol"] = symbol
                    signals.append(signal_info)

                    if signal_info["signal"] != "HOLD":
                        self.discord.send_signal_alert(symbol, signal_info)

                except Exception as e:
                    logger.error("종목 {} 장전 분석 실패: {}", symbol, e)

            logger.info("장전 분석 완료: {}개 종목, 매수 신호 {}건",
                len(signals),
                sum(1 for s in signals if s["signal"] == "BUY"),
            )

        except Exception as e:
            logger.error("장전 준비 실패: {}", e)

    def _run_monitoring(self):
        """장중 모니터링: 손절/익절 체크 + 실시간 신호"""
        logger.debug("🔍 장중 모니터링 ({})", datetime.now().strftime("%H:%M:%S"))

        try:
            # 블랙스완 체크
            if self.blackswan.is_on_cooldown():
                logger.warning("블랙스완 쿨다운 중 — 매매 스킵")
                return

            # 보유 포지션 손절/익절 체크
            from core.order_executor import OrderExecutor
            from api.kis_api import KISApi

            executor = OrderExecutor(self.config)
            kis = KISApi()
            positions = get_all_positions()

            for pos in positions:
                try:
                    # 현재가 조회
                    price_info = kis.get_current_price(pos.symbol)
                    if not price_info:
                        continue

                    current_price = price_info["price"]

                    # 블랙스완 개별 종목 체크
                    prev_close = price_info.get("prev_close", pos.avg_price)
                    bs_result = self.blackswan.check_stock(pos.symbol, current_price, prev_close)

                    if bs_result["triggered"]:
                        # 긴급 매도
                        self.discord.send_message(f"🚨 블랙스완 발동!\n{bs_result['reason']}")
                        executor.execute_sell(
                            pos.symbol, current_price,
                            reason="블랙스완 긴급 매도",
                            strategy=self.strategy_name,
                        )
                        continue

                    # 일반 손절/익절 체크
                    check = executor.check_stop_loss_take_profit(pos.symbol, current_price)
                    if check["action"]:
                        executor.execute_sell(
                            pos.symbol, current_price,
                            reason=check["action"],
                            strategy=self.strategy_name,
                        )
                        self.discord.send_trade_alert({
                            "action": "SELL",
                            "symbol": pos.symbol,
                            "price": current_price,
                            "quantity": pos.quantity,
                            "pnl": (current_price - pos.avg_price) * pos.quantity,
                            "pnl_rate": ((current_price / pos.avg_price) - 1) * 100,
                        })

                except Exception as e:
                    logger.error("종목 {} 모니터링 실패: {}", pos.symbol, e)

        except Exception as e:
            logger.error("장중 모니터링 실패: {}", e)

    def _run_post_market(self):
        """장마감: 일일 리포트 발송"""
        logger.info("=" * 50)
        logger.info("📊 장마감 리포트 ({})", datetime.now().strftime("%H:%M:%S"))
        logger.info("=" * 50)

        try:
            # 포트폴리오 스냅샷 저장
            self.portfolio.save_daily_snapshot()

            # 일일 리포트 발송
            summary = self.portfolio.get_portfolio_summary()
            self.discord.send_daily_report({
                "total_value": summary["total_value"],
                "cash": summary["cash"],
                "daily_return": 0,
                "cumulative_return": summary["total_return"],
                "mdd": summary["mdd"],
                "position_count": summary["position_count"],
            })

            logger.info("장마감 리포트 발송 완료")

        except Exception as e:
            logger.error("장마감 리포트 실패: {}", e)

    def _should_monitor(self) -> bool:
        """모니터링 주기 확인"""
        if self._last_monitor_time is None:
            return True
        elapsed = (datetime.now() - self._last_monitor_time).total_seconds()
        return elapsed >= self.monitor_interval

    def _sleep_until_next_trading_day(self):
        """다음 거래일까지 대기"""
        wait = self.trading_hours.time_until_market_open()
        hours = wait.total_seconds() / 3600
        logger.info("비거래일 — 다음 거래일까지 {:.1f}시간 대기", hours)
        # 비거래일에는 1시간 간격으로 재확인
        time_mod.sleep(min(wait.total_seconds(), 3600))

    def _get_strategy(self):
        """전략 인스턴스 반환"""
        if self.strategy_name == "mean_reversion":
            from strategies.mean_reversion import MeanReversionStrategy
            return MeanReversionStrategy(self.config)
        elif self.strategy_name == "trend_following":
            from strategies.trend_following import TrendFollowingStrategy
            return TrendFollowingStrategy(self.config)
        else:
            from strategies.scoring_strategy import ScoringStrategy
            return ScoringStrategy(self.config)

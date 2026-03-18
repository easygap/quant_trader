"""
자동 스케줄러 모듈
- 장전 준비 → 장중 모니터링 → 장마감 리포트 사이클 자동 실행
- 무한 루프 기반 (Ctrl+C로 종료)
"""

import time as time_mod
from datetime import datetime
from loguru import logger

from config.config_loader import Config
from core.trading_hours import TradingHours
from core.blackswan_detector import BlackSwanDetector
from core.portfolio_manager import PortfolioManager
from monitoring.discord_bot import DiscordBot
from database.repositories import (
    get_all_positions,
    get_daily_trade_summary,
    get_position,
    save_daily_report,
)
from core.position_lock import PositionLock


class Scheduler:
    """
    자동 매매 스케줄러

    3가지 단계를 자동으로 반복합니다:
    1. 장전 준비 (08:50): 데이터 수집, 지표 계산, 관심 종목 분석
    2. 장중 모니터링 (09:00~15:30): 10분 간격 신호 확인, 손절/익절 체크
    3. 장마감 (15:35): 일일 리포트, 포트폴리오 스냅샷 저장
    """

    def __init__(self, strategy_name: str = "scoring", config: Config = None):
        self.config = config or Config.get()
        self.strategy_name = strategy_name
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)
        self.portfolio = PortfolioManager(self.config)
        self.discord = DiscordBot(self.config)
        self.auto_entry = self.config.trading.get("auto_entry", False)

        self.monitor_interval = 600  # 10분

        self._pre_market_done = False
        self._post_market_done = False
        self._last_monitor_time = None
        self._today = None
        self._entry_candidates = []

        logger.info(
            "Scheduler 초기화 (전략: {}, 모니터링 간격: {}초, auto_entry: {})",
            strategy_name, self.monitor_interval, self.auto_entry,
        )

    def run(self):
        """메인 루프 — 무한 반복으로 장 시간에 맞춰 자동 실행."""
        logger.info("🚀 자동 스케줄러 시작 (전략: {})", self.strategy_name)
        self.discord.send_message(f"🚀 퀀트 트레이더 스케줄러 시작\n전략: {self.strategy_name}")

        try:
            while True:
                now = datetime.now()
                today = now.date()

                if self._today != today:
                    self._today = today
                    self._pre_market_done = False
                    self._post_market_done = False
                    self._last_monitor_time = None
                    self._entry_candidates = []
                    logger.info("📅 새로운 거래일: {}", today)

                if not self.trading_hours.is_trading_day():
                    self._sleep_until_next_trading_day()
                    continue

                if self.trading_hours.is_pre_market() and not self._pre_market_done:
                    self._run_pre_market()
                    self._pre_market_done = True
                elif self.trading_hours.is_market_open():
                    if self._should_monitor():
                        self._run_monitoring()
                        self._last_monitor_time = now
                elif now.hour == 15 and now.minute >= 35 and not self._post_market_done:
                    self._run_post_market()
                    self._post_market_done = True

                time_mod.sleep(30)

        except KeyboardInterrupt:
            logger.info("⏹️ 스케줄러 종료 (Ctrl+C)")
            self.discord.send_message("⏹️ 퀀트 트레이더 스케줄러 종료")

    def _run_pre_market(self):
        """장전 준비: 데이터 수집 + 전략 분석."""
        logger.info("=" * 50)
        logger.info("📋 장전 준비 시작 ({})", datetime.now().strftime("%H:%M:%S"))
        logger.info("=" * 50)

        try:
            from core.data_collector import DataCollector

            collector = DataCollector()
            strategy = self._get_strategy()
            watchlist = self.config.watchlist or ["005930"]
            self._entry_candidates = []

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

                    if (
                        self.auto_entry
                        and signal_info["signal"] == "BUY"
                        and not get_position(symbol)
                    ):
                        self._entry_candidates.append({
                            "symbol": symbol,
                            "price": signal_info.get("close", 0),
                            "atr": signal_info.get("atr"),
                            "score": signal_info.get("score", 0),
                            "reason": "live pre-market candidate",
                        })

                except Exception as e:
                    logger.error("종목 {} 장전 분석 실패: {}", symbol, e)

            logger.info(
                "장전 분석 완료: {}개 종목, 매수 신호 {}건, 진입 후보 {}건",
                len(signals),
                sum(1 for s in signals if s["signal"] == "BUY"),
                len(self._entry_candidates),
            )

        except Exception as e:
            logger.error("장전 준비 실패: {}", e)

    def _run_monitoring(self):
        """장중 모니터링: 진입 후보 실행 + 손절/익절 체크."""
        logger.debug("🔍 장중 모니터링 ({})", datetime.now().strftime("%H:%M:%S"))

        try:
            if self.auto_entry and self._entry_candidates and not self.blackswan.is_on_cooldown():
                with PositionLock():
                    self._execute_entry_candidates()

            with PositionLock():
                self._check_exit_signals()

            if self.blackswan.is_on_cooldown():
                logger.warning("블랙스완 쿨다운 중 — 신규 매수만 스킵 (손절/익절은 정상 동작)")
        except Exception as e:
            logger.error("장중 모니터링 실패: {}", e)

    def _execute_entry_candidates(self):
        """장전 분석에서 쌓인 BUY 후보를 장중 첫 사이클에 실행."""
        if not self._entry_candidates:
            return

        from core.order_executor import OrderExecutor

        executor = OrderExecutor(self.config)
        remaining = []

        for candidate in self._entry_candidates:
            symbol = candidate["symbol"]
            if get_position(symbol):
                continue

            summary = self.portfolio.get_portfolio_summary()
            result = executor.execute_buy(
                symbol=symbol,
                price=candidate["price"],
                capital=summary["total_value"],
                available_cash=summary["cash"],
                atr=candidate.get("atr"),
                signal_score=candidate.get("score", 0),
                reason=candidate.get("reason", "live auto-entry"),
                strategy=self.strategy_name,
            )
            if result.get("success"):
                self.discord.send_trade_alert(result)
            else:
                remaining.append(candidate)

        self._entry_candidates = remaining

    def _check_exit_signals(self):
        """포지션 순회 및 손절/익절/트레일링 스탑 실행."""
        from core.order_executor import OrderExecutor
        from api.kis_api import KISApi

        executor = OrderExecutor(self.config)
        kis = KISApi()
        positions = get_all_positions()

        for pos in positions:
            try:
                price_info = kis.get_current_price(pos.symbol)
                if not price_info:
                    continue

                current_price = price_info["price"]
                prev_close = price_info.get("prev_close", pos.avg_price)
                bs_result = self.blackswan.check_stock(pos.symbol, current_price, prev_close)

                if bs_result["triggered"]:
                    self.discord.send_message(f"🚨 블랙스완 발동!\n{bs_result['reason']}")
                    executor.execute_sell(
                        pos.symbol, current_price,
                        reason="블랙스완 긴급 매도",
                        strategy=self.strategy_name,
                    )
                    continue

                check = executor.check_stop_loss_take_profit(pos.symbol, current_price)
                if check["action"]:
                    result = executor.execute_sell(
                        pos.symbol, current_price,
                        reason=check["action"],
                        strategy=self.strategy_name,
                    )
                    if result.get("success"):
                        self.discord.send_trade_alert(result)

            except Exception as e:
                logger.error("종목 {} 모니터링 실패: {}", pos.symbol, e)

    def _run_post_market(self):
        """장마감: 일일 리포트 저장 및 발송."""
        logger.info("=" * 50)
        logger.info("📊 장마감 리포트 ({})", datetime.now().strftime("%H:%M:%S"))
        logger.info("=" * 50)

        try:
            self.portfolio.save_daily_snapshot()
            summary = self.portfolio.get_portfolio_summary()
            trade_summary = get_daily_trade_summary(mode=self.config.trading.get("mode", "paper"))

            report_text = (
                f"총 평가금 {summary['total_value']:,.0f}원 | "
                f"현금 {summary['cash']:,.0f}원 | "
                f"실현손익 {summary['realized_pnl']:,.0f}원 | "
                f"미실현손익 {summary['unrealized_pnl']:,.0f}원 | "
                f"당일 매매 {trade_summary['total_trades']}건"
            )
            save_daily_report(
                date=datetime.now(),
                total_trades=trade_summary["total_trades"],
                buy_count=trade_summary["buy_count"],
                sell_count=trade_summary["sell_count"],
                realized_pnl=trade_summary["realized_pnl"],
                unrealized_pnl=summary["unrealized_pnl"],
                total_commission=trade_summary["total_commission"],
                total_tax=trade_summary["total_tax"],
                winning_trades=trade_summary["winning_trades"],
                losing_trades=trade_summary["losing_trades"],
                report_text=report_text,
            )

            self.discord.send_daily_report({
                "total_value": summary["total_value"],
                "cash": summary["cash"],
                "daily_return": 0,
                "cumulative_return": summary["total_return"],
                "mdd": summary["mdd"],
                "position_count": summary["position_count"],
                "total_trades": trade_summary["total_trades"],
            })

            logger.info("장마감 리포트 저장 및 발송 완료")

        except Exception as e:
            logger.error("장마감 리포트 실패: {}", e)

    def _should_monitor(self) -> bool:
        """모니터링 주기 확인."""
        if self._last_monitor_time is None:
            return True
        elapsed = (datetime.now() - self._last_monitor_time).total_seconds()
        return elapsed >= self.monitor_interval

    def _sleep_until_next_trading_day(self):
        """다음 거래일까지 대기."""
        wait = self.trading_hours.time_until_market_open()
        hours = wait.total_seconds() / 3600
        logger.info("비거래일 — 다음 거래일까지 {:.1f}시간 대기", hours)
        time_mod.sleep(min(wait.total_seconds(), 3600))

    def _get_strategy(self):
        """전략 인스턴스 반환."""
        if self.strategy_name == "mean_reversion":
            from strategies.mean_reversion import MeanReversionStrategy
            return MeanReversionStrategy(self.config)
        if self.strategy_name == "trend_following":
            from strategies.trend_following import TrendFollowingStrategy
            return TrendFollowingStrategy(self.config)
        if self.strategy_name == "ensemble":
            from core.strategy_ensemble import StrategyEnsemble
            return StrategyEnsemble(self.config)
        from strategies.scoring_strategy import ScoringStrategy
        return ScoringStrategy(self.config)

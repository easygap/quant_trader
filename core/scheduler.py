"""
자동 스케줄러 모듈
- 장전 준비 → 장중 모니터링 → 장마감 리포트 사이클 자동 실행
- 무한 루프 기반 (Ctrl+C로 종료)
"""

import time as time_mod
from datetime import datetime
from loguru import logger

from config.config_loader import Config
from core.watchlist_manager import WatchlistManager
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

    거래 빈도·수수료: 10분마다 신호를 보므로 신호가 자주 바뀌는 전략은 과매매(수수료만 나감) 위험.
    왕복 비용 약 0.23%를 상회하는 기대 수익이 나오도록 전략·임계값 설계 권장. quant_trader_design.md §8.3.
    """

    def __init__(self, strategy_name: str = "scoring", config: Config = None):
        self.config = config or Config.get()
        self.strategy_name = strategy_name
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)
        self.portfolio = PortfolioManager(self.config, account_key=self.strategy_name)
        self.discord = DiscordBot(self.config)
        self.auto_entry = self.config.trading.get("auto_entry", False)

        self.monitor_interval = 600  # 10분

        self._pre_market_done = False
        self._post_market_done = False
        self._last_monitor_time = None
        self._today = None
        self._entry_candidates = []
        self._skip_next_monitor_cycle = False
        self._last_sync_broker_time = None  # KIS 잔고 크로스체크 주기용

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
                    self._last_sync_broker_time = None
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
            watchlist = WatchlistManager(self.config).resolve()
            self._entry_candidates = []

            from core.market_regime import allow_new_buys_by_market_regime
            allow_buys = allow_new_buys_by_market_regime(self.config, collector)

            signals = []
            for symbol in watchlist:
                try:
                    df = collector.fetch_korean_stock(symbol)
                    if df.empty or len(df) < 30:
                        continue

                    signal_info = strategy.generate_signal(df, symbol=symbol)
                    signal_info["symbol"] = symbol
                    signals.append(signal_info)

                    if signal_info["signal"] != "HOLD":
                        self.discord.send_signal_alert(symbol, signal_info)

                    if (
                        self.auto_entry
                        and allow_buys
                        and signal_info["signal"] == "BUY"
                        and not get_position(symbol)
                    ):
                        # 일평균 거래량(20일): 거래량 기반 동적 슬리피지용 (소형주 1~3% 슬리피지 반영)
                        avg_vol = None
                        if "volume" in df.columns and not df["volume"].empty:
                            avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1])
                        self._entry_candidates.append({
                            "symbol": symbol,
                            "price": signal_info.get("close", 0),
                            "atr": signal_info.get("atr"),
                            "score": signal_info.get("score", 0),
                            "reason": "live pre-market candidate",
                            "avg_daily_volume": avg_vol,
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
        started_at = datetime.now()

        try:
            if self.auto_entry and self._entry_candidates and not self.blackswan.is_on_cooldown():
                with PositionLock():
                    self._execute_entry_candidates()

            with PositionLock():
                self._check_exit_signals()

            # live 모드: KIS 잔고와 DB 포지션 상시 크로스체크 (주기적)
            if self.config.trading.get("mode") == "live":
                self._maybe_sync_with_broker()

            if self.blackswan.is_on_cooldown():
                logger.warning("블랙스완 쿨다운 중 — 신규 매수만 스킵 (손절/익절은 정상 동작)")
        except Exception as e:
            logger.error("장중 모니터링 실패: {}", e)
        finally:
            # 타이밍 리스크 안전장치: 루프 실행 시간이 10분을 초과하면 다음 루프 스킵 (API 지연·Rate Limit 시 꼬임 방지)
            elapsed = (datetime.now() - started_at).total_seconds()
            if elapsed > self.monitor_interval:
                self._skip_next_monitor_cycle = True
                logger.warning(
                    "⚠️ 장중 루프가 {}초 소요되어 다음 모니터링 사이클을 1회 스킵합니다. (안전: 10분 초과 시 다음 루프 생략)",
                    int(elapsed),
                )

    def _maybe_sync_with_broker(self):
        """live 모드에서 주기적으로 KIS 잔고와 DB 포지션 크로스체크 (불일치 시 로깅·알림)."""
        interval_min = int(self.config.trading.get("sync_broker_interval_minutes", 30))
        now = datetime.now()
        if self._last_sync_broker_time is not None:
            elapsed = (now - self._last_sync_broker_time).total_seconds()
            if elapsed < interval_min * 60:
                return
        self._last_sync_broker_time = now
        try:
            self.portfolio.sync_with_broker()
        except Exception as e:
            logger.warning("장중 KIS 잔고 크로스체크 실패: {}", e)

    def _execute_entry_candidates(self):
        """장전 분석에서 쌓인 BUY 후보를 장중 첫 사이클에 실행."""
        if not self._entry_candidates:
            return

        from core.order_executor import OrderExecutor
        from core.data_collector import DataCollector
        from core.market_regime import allow_new_buys_by_market_regime

        if not allow_new_buys_by_market_regime(self.config, DataCollector()):
            logger.info("하락장(코스피 200일선 이하)으로 신규 매수 전면 중단 — 진입 후보 실행 생략")
            self._entry_candidates = []
            return

        executor = OrderExecutor(self.config, account_key=self.strategy_name)
        remaining = []

        for candidate in self._entry_candidates:
            symbol = candidate["symbol"]
            if get_position(symbol, account_key=self.strategy_name):
                continue

            summary = self.portfolio.get_portfolio_summary()
            result = executor.execute_buy(
                symbol=symbol,
                price=candidate["price"],
                capital=summary["total_value"],
                available_cash=summary["cash"],
                current_invested=summary["current_value"],
                atr=candidate.get("atr"),
                signal_score=candidate.get("score", 0),
                reason=candidate.get("reason", "live auto-entry"),
                strategy=self.strategy_name,
                avg_daily_volume=candidate.get("avg_daily_volume"),
            )
            if result.get("success"):
                self.discord.send_trade_alert(result)
            else:
                remaining.append(candidate)

        self._entry_candidates = remaining

    def _check_exit_signals(self):
        """포지션 순회: 최대 보유 기간 초과 시 강제 정리, 블랙스완, 손절/익절/트레일링 스탑."""
        from core.order_executor import OrderExecutor
        from api.kis_api import KISApi

        executor = OrderExecutor(self.config, account_key=self.strategy_name)
        account_no = self.config.get_account_no(self.strategy_name)
        kis = KISApi(account_no=account_no)
        positions = get_all_positions(account_key=self.strategy_name)
        today = datetime.now().date()
        max_holding_days = (
            self.config.risk_params.get("position_limits", {}) or {}
        ).get("max_holding_days", 0)

        for pos in positions:
            try:
                price_info = kis.get_current_price(pos.symbol)
                if not price_info:
                    continue

                current_price = price_info["price"]
                prev_close = price_info.get("prev_close", pos.avg_price)

                # 최대 보유 기간 초과 시 신호 없어도 강제 정리 (물리는 상황 방지)
                if max_holding_days > 0 and getattr(pos, "bought_at", None):
                    bought_date = pos.bought_at.date() if hasattr(pos.bought_at, "date") else pos.bought_at
                    holding_days = (today - bought_date).days
                    if holding_days >= max_holding_days:
                        result = executor.execute_sell(
                            pos.symbol, current_price,
                            reason=f"최대 보유 기간({max_holding_days}일) 도달 강제 정리 (보유 {holding_days}일)",
                            strategy=self.strategy_name,
                        )
                        if result.get("success"):
                            self.discord.send_trade_alert(result)
                        continue

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
            trade_summary = get_daily_trade_summary(
                mode=self.config.trading.get("mode", "paper"),
                account_key=self.strategy_name,
            )

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
                account_key=self.strategy_name,
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

            # live 모드: 장마감 시 KIS 잔고와 DB 크로스체크 후 백업 (SQLite 손상 대비)
            if self.config.trading.get("mode") == "live":
                try:
                    self.portfolio.sync_with_broker()
                except Exception as sync_err:
                    logger.warning("장마감 KIS 크로스체크 실패: {}", sync_err)
            try:
                from database.backup import run_daily_backup
                run_daily_backup(self.config)
            except Exception as backup_err:
                logger.warning("DB 백업 스킵/실패: {}", backup_err)

            logger.info("장마감 리포트 저장 및 발송 완료")

        except Exception as e:
            logger.error("장마감 리포트 실패: {}", e)

    def _should_monitor(self) -> bool:
        """모니터링 주기 확인. 이전 루프가 10분 초과 시 다음 사이클 스킵(타이밍 리스크 방지)."""
        if self._skip_next_monitor_cycle:
            self._skip_next_monitor_cycle = False
            logger.warning("이전 장중 루프 지연으로 이번 모니터링 사이클을 스킵합니다. (10분 초과 안전장치)")
            return False
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

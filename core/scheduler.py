"""
자동 스케줄러 모듈
- 장전 준비 → 장중 모니터링 → 장마감 리포트 사이클 자동 실행
- 무한 루프 기반 (Ctrl+C로 종료)
- 시스템 헬스체크 자동화 (DB/API/디스크/메모리)
- 루프 모니터링 지표 (실행 시간, 스킵 횟수)
"""

import os
import time as time_mod
import shutil
from datetime import datetime, timedelta
from loguru import logger

from config.config_loader import Config
from core.watchlist_manager import WatchlistManager
from core.trading_hours import TradingHours
from core.blackswan_detector import BlackSwanDetector
from core.portfolio_manager import PortfolioManager
from core.notifier import Notifier
from database.repositories import (
    get_all_positions,
    get_daily_trade_summary,
    get_pending_failed_orders,
    get_position,
    save_daily_report,
)
from core.position_lock import PositionLock


class LoopMetrics:
    """10분 루프 모니터링 지표 수집기."""

    def __init__(self):
        self.total_loops = 0
        self.total_skips = 0
        self.consecutive_skips = 0
        self.last_success_time: datetime | None = None
        self.last_elapsed_seconds: float = 0.0
        self.max_elapsed_seconds: float = 0.0
        self._elapsed_sum: float = 0.0
        self.loops_over_8min: int = 0
        self.loops_over_10min: int = 0

    def record_success(self, elapsed: float, monitor_interval: float = 600.0):
        """
        monitor_interval: 장중 모니터링 주기(초). 10분=600일 때 80% 경고=480초.
        주기가 매우 짧은 단위테스트(예: 1초)에서는 8분 경고 카운트를 올리지 않음(소프트 임계 < 60이면 비활성).
        """
        self.total_loops += 1
        self.consecutive_skips = 0
        self.last_success_time = datetime.now()
        self.last_elapsed_seconds = elapsed
        self.max_elapsed_seconds = max(self.max_elapsed_seconds, elapsed)
        self._elapsed_sum += elapsed
        soft = int(monitor_interval * 0.8)
        if soft >= 60 and elapsed > soft:
            self.loops_over_8min += 1
        if elapsed > monitor_interval:
            self.loops_over_10min += 1

    def record_skip(self):
        self.total_skips += 1
        self.consecutive_skips += 1

    def summary(self) -> dict:
        avg = self._elapsed_sum / self.total_loops if self.total_loops else 0.0
        return {
            "total_loops": self.total_loops,
            "total_skips": self.total_skips,
            "consecutive_skips": self.consecutive_skips,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
            "last_elapsed_s": round(self.last_elapsed_seconds, 1),
            "max_elapsed_s": round(self.max_elapsed_seconds, 1),
            "avg_elapsed_s": round(avg, 1),
            "loops_over_8min": self.loops_over_8min,
            "loops_over_10min": self.loops_over_10min,
        }


class Scheduler:
    """
    자동 매매 스케줄러

    3가지 단계를 자동으로 반복합니다:
    1. 장전 준비 (08:50): 데이터 수집, 지표 계산, 관심 종목 분석
    2. 장중 모니터링 (09:00~15:30): 10분 간격 신호 확인, 손절/익절 체크
    3. 장마감 (15:35): 일일 리포트, 포트폴리오 스냅샷 저장

    거래 빈도·수수료: 10분마다 신호를 보므로 신호가 자주 바뀌는 전략은 과매매(수수료만 나감) 위험.
    왕복 비용 약 0.23%(수수료+거래세 0.20%, 2026년 기준)를 상회하는 기대 수익이 나오도록 전략·임계값 설계 권장. quant_trader_design.md §8.3.
    """

    MAX_CONSECUTIVE_SKIPS = 3  # 연속 스킵 허용 한도 (초과 시 알림)

    def __init__(self, strategy_name: str = "scoring", config: Config = None):
        self.config = config or Config.get()
        self.strategy_name = strategy_name
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)
        self.portfolio = PortfolioManager(self.config, account_key=self.strategy_name)
        self.discord = Notifier(self.config)
        self.auto_entry = self.config.trading.get("auto_entry", False)

        self.monitor_interval = 600  # 기본 10분 (적응형 주기로 동적 변경됨)

        self._pre_market_done = False
        self._post_market_done = False
        self._last_monitor_time = None
        self._today = None
        self._entry_candidates = []
        self._skip_next_monitor_cycle = False
        self._last_sync_broker_time = None  # KIS 잔고 크로스체크 주기용
        self._last_healthcheck_time: datetime | None = None
        self._last_regime_check_time: datetime | None = None  # 장중 시장 국면 재확인 주기
        self._market_regime_scale: float = 1.0
        self._loop_metrics = LoopMetrics()

        logger.info(
            "Scheduler 초기화 (전략: {}, 모니터링 간격: {}초, auto_entry: {})",
            strategy_name, self.monitor_interval, self.auto_entry,
        )

    def startup_recovery(self):
        """
        비정상 종료 후 재시작 시: Dead-letter 미처리 건 알림, KIS 미체결 확인, 잔고↔DB 동기화.
        장중이면 `_last_monitor_time`을 비워 첫 장중 루프에서 곧바로 모니터링이 돌도록 한다.
        """
        try:
            pending_orders = get_pending_failed_orders()
            if pending_orders:
                logger.warning("[복구] 미처리 실패 주문 {}건 발견", len(pending_orders))
                lines = []
                for o in pending_orders[:15]:
                    ak = getattr(o, "account_key", "") or ""
                    lines.append(
                        f"  #{o.id} [{ak}] {o.symbol} {o.action} {o.quantity}주 @{o.price:,.0f}"
                    )
                if len(pending_orders) > 15:
                    lines.append(f"  … 외 {len(pending_orders) - 15}건")
                self.discord.send_message(
                    "⚠️ **[복구] 미처리 Dead-letter 주문**\n"
                    f"총 {len(pending_orders)}건 (status=pending)\n"
                    + "\n".join(lines),
                    critical=True,
                )

            if self.config.trading.get("mode") == "live":
                from core.order_executor import OrderExecutor

                executor = OrderExecutor(self.config, account_key=self.strategy_name)
                open_orders = executor.reconcile_open_orders_after_crash()
                if open_orders:
                    logger.warning("[복구] KIS 미체결 주문 {}건: {}", len(open_orders), open_orders)
                    parts = [
                        f"{x.get('symbol','?')} {x.get('remaining_qty','?')}주 "
                        f"({x.get('buy_sell','')}) @{x.get('order_price','')}"
                        for x in open_orders[:12]
                    ]
                    tail = f"\n… 외 {len(open_orders) - 12}건" if len(open_orders) > 12 else ""
                    self.discord.send_message(
                        "⚠️ **[복구] KIS 미체결 주문**\n"
                        f"{len(open_orders)}건 — 잔고 동기화로 체결분 반영 예정\n"
                        + "\n".join(parts)
                        + tail,
                        critical=True,
                    )

                try:
                    self.portfolio.sync_with_broker(auto_correct=True)
                except Exception as e:
                    logger.warning("[복구] KIS 잔고↔DB 동기화 실패: {}", e)
            else:
                logger.info("[복구] paper 모드 — KIS 미체결·잔고 동기화 생략")

            if self.trading_hours.is_market_open():
                self._last_monitor_time = None
                logger.info("[복구] 장중 재시작 — 모니터링 주기 타이머 초기화(다음 루프에서 즉시 실행 가능)")
            else:
                logger.info("[복구] 비장중 재시작 — 다음 거래일·장 개시까지 기존 스케줄로 대기")
        except Exception as e:
            logger.error("[복구] startup_recovery 처리 중 오류: {}", e)

    def run(self):
        """메인 루프 — 무한 반복으로 장 시간에 맞춰 자동 실행."""
        logger.info("🚀 자동 스케줄러 시작 (전략: {})", self.strategy_name)
        self.discord.send_message(f"🚀 퀀트 트레이더 스케줄러 시작\n전략: {self.strategy_name}")
        self.startup_recovery()

        try:
            while True:
                try:
                    now = datetime.now()
                    today = now.date()

                    if self._today != today:
                        self._today = today
                        self._pre_market_done = False
                        self._post_market_done = False
                        self._last_monitor_time = None
                        self._last_sync_broker_time = None
                        self._entry_candidates = []
                        self._loop_metrics = LoopMetrics()
                        logger.info("📅 새로운 거래일: {}", today)
                        self._maybe_update_holidays()

                    if not self.trading_hours.is_trading_day():
                        self._sleep_until_next_trading_day()
                        continue

                    # 장전 헬스체크 (10분 주기)
                    self._maybe_run_healthcheck()

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

                except Exception as loop_exc:
                    logger.exception(
                        "[Scheduler] 메인 루프 예외 — 30초 후 계속: {}",
                        loop_exc,
                    )
                    try:
                        self.discord.send_message(
                            f"⚠️ **[Scheduler]** 루프 예외(복구 진행): `{type(loop_exc).__name__}`",
                        )
                    except Exception:
                        pass

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

            from core.market_regime import check_market_regime
            regime_result = check_market_regime(self.config, collector)
            allow_buys = regime_result["allow_buys"]
            self._market_regime_scale = regime_result["position_scale"]

            # API 요청 사전 예측: 종목당 ~2건(데이터 수집 + 포지션 조회) 가정
            n_syms = len(watchlist)
            est_requests = n_syms * 2
            self._log_rate_limit_preflight(est_requests, n_syms, "장전 분석")

            signals = []
            for symbol in watchlist:
                try:
                    df = collector.fetch_stock(symbol)
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
                            "market_regime_scale": self._market_regime_scale,
                            "timestamp": datetime.now(),
                        })

                except Exception as e:
                    logger.error("종목 {} 장전 분석 실패: {}", symbol, e)

            logger.info(
                "장전 분석 완료: {}개 종목, 매수 신호 {}건, 진입 후보 {}건",
                len(signals),
                sum(1 for s in signals if s["signal"] == "BUY"),
                len(self._entry_candidates),
            )
            self._log_rate_limit_stats("장전 분석")

            mismatched = collector.check_source_consistency()
            if mismatched:
                msg = (
                    f"⚠️ 데이터 소스 불일치 감지: {mismatched}. "
                    "백테스트(수정주가)와 다른 소스(비수정주가 가능)를 사용 중. "
                    "지표·신호가 달라질 수 있습니다. pip install FinanceDataReader 권장."
                )
                logger.warning(msg)
                self.discord.send_message(msg, critical=True)

            self._run_basket_rebalance_check()

        except Exception as e:
            logger.error("장전 준비 실패: {}", e)

    def _get_kis_max_calls_per_sec(self) -> float:
        """KIS 설정 초당 호출 한도 (예상 소요 계산용). 실패 시 10."""
        try:
            from api.kis_api import KISApi
            v = float(KISApi().max_calls_per_sec)
            return v if v > 0 else 10.0
        except Exception:
            return 10.0

    def _log_monitoring_watchlist_preflight(self):
        """장중 루프 진입 전 워치리스트 규모·KIS 한도 대비 예상 API 소요(종목×2/초당한도) 로깅."""
        try:
            n = len(WatchlistManager(self.config).resolve())
        except Exception as e:
            logger.debug("워치리스트 조회 실패(프리플라이트 스킵): {}", e)
            return
        per_sec = self._get_kis_max_calls_per_sec()
        est_sec = (n * 2) / per_sec if per_sec > 0 else 0.0
        if n > 50:
            logger.error(
                "[Scheduler] 종목 {}개. 10분 루프 내 처리 불가능할 수 있습니다. "
                "예상 순수 API 소요 약 {:.1f}초 (종목×2요청 / 초당 {:.0f}건)",
                n, est_sec, per_sec,
            )
        elif n > 30:
            logger.warning(
                "[Scheduler] 종목 {}개. KIS API 분당 한도 초과 위험. "
                "예상 순수 API 소요 약 {:.1f}초 (종목×2요청 / 초당 {:.0f}건)",
                n, est_sec, per_sec,
            )

    def _run_monitoring(self):
        """장중 모니터링: 진입 후보 실행 + 손절/익절 체크."""
        logger.debug("🔍 장중 모니터링 ({})", datetime.now().strftime("%H:%M:%S"))
        started_at = datetime.now()

        self._log_monitoring_watchlist_preflight()

        try:
            # 장중 시장 국면 재확인 (2시간마다)
            self._maybe_recheck_market_regime()

            # 쿨다운 해제 직후 → 즉시 신호 재평가 (반등 구간 포착)
            if self.blackswan.consume_cooldown_ended_flag():
                logger.info("블랙스완 쿨다운 해제 — 즉시 신호 재평가 트리거")
                self.discord.send_message("🔄 블랙스완 쿨다운 해제 — 신호 재평가 후 recovery 사이징으로 재진입 검토")
                self._run_post_cooldown_rescan()

            if self.auto_entry and self._entry_candidates and not self.blackswan.is_on_cooldown():
                with PositionLock():
                    self._execute_entry_candidates()

            with PositionLock():
                self._check_exit_signals()

            # 장중 보유 종목 동적 손절가 업데이트 (ATR 변화 반영)
            self._update_dynamic_stop_losses()

            # 장중 신호 재평가: 보유하지 않은 워치리스트 종목에서 새 진입 기회 탐색
            if self.auto_entry and not self.blackswan.is_on_cooldown():
                self._rescan_for_new_entries()

            # live 모드: KIS 잔고와 DB 포지션 상시 크로스체크 (주기적)
            if self.config.trading.get("mode") == "live":
                self._maybe_sync_with_broker()

            if self.blackswan.is_on_cooldown():
                logger.warning("블랙스완 쿨다운 중 — 신규 매수만 스킵 (손절/익절은 정상 동작)")
        except Exception as e:
            logger.error("장중 모니터링 실패: {}", e)
        finally:
            elapsed = (datetime.now() - started_at).total_seconds()

            soft = int(self.monitor_interval * 0.8)
            if soft >= 60 and elapsed > soft:
                logger.warning(
                    "[Scheduler] 루프 실행 시간 {:.1f}초 경고. {}초(모니터 주기의 80%) 초과 — 10분 한도에 근접.",
                    elapsed,
                    soft,
                )
                self.discord.send_message(
                    f"⚠️ **[Scheduler]** 장중 루프 **{elapsed:.1f}초** 소요 "
                    f"({soft}초 = 주기 {self.monitor_interval:.0f}초의 80% 초과). "
                    "다음 루프가 주기 한도에 가까워질 수 있습니다.",
                )

            self._loop_metrics.record_success(elapsed, self.monitor_interval)

            if elapsed > self.monitor_interval:
                self._skip_next_monitor_cycle = True
                self._loop_metrics.record_skip()
                logger.warning(
                    "⚠️ 장중 루프가 {}초 소요되어 다음 모니터링 사이클을 1회 스킵합니다. (안전: 10분 초과 시 다음 루프 생략) "
                    "[LoopMetrics] 10분 초과(timeout) 1회 기록",
                    int(elapsed),
                )

            if self._loop_metrics.consecutive_skips >= self.MAX_CONSECUTIVE_SKIPS:
                msg = (
                    f"🚨 루프 연속 스킵 {self._loop_metrics.consecutive_skips}회 — "
                    f"시스템 지연 의심. 최근 루프 {elapsed:.0f}초 소요."
                )
                logger.error(msg)
                self.discord.send_message(msg, critical=True)

            if self._loop_metrics.total_loops % 6 == 0:
                logger.info("📊 루프 지표: {}", self._loop_metrics.summary())

    def _maybe_recheck_market_regime(self):
        """장중 2시간마다 시장 국면을 재확인하여 기존 포지션 규모 조정 권고."""
        interval_sec = 7200  # 2시간
        now = datetime.now()
        if self._last_regime_check_time is not None:
            elapsed = (now - self._last_regime_check_time).total_seconds()
            if elapsed < interval_sec:
                return
        self._last_regime_check_time = now
        try:
            from core.data_collector import DataCollector
            from core.market_regime import check_market_regime

            regime_result = check_market_regime(self.config, DataCollector())
            new_scale = regime_result["position_scale"]
            if new_scale < self._market_regime_scale:
                logger.warning(
                    "장중 시장 국면 악화 감지: 포지션 스케일 {:.0f}% → {:.0f}%",
                    self._market_regime_scale * 100, new_scale * 100,
                )
                self.discord.send_message(
                    f"⚠️ **장중 시장 국면 악화** 감지\n"
                    f"포지션 스케일: {self._market_regime_scale*100:.0f}% → {new_scale*100:.0f}%\n"
                    f"신규 매수 축소 적용. 기존 포지션 검토 권장.",
                )
            self._market_regime_scale = new_scale
        except Exception as e:
            logger.debug("장중 국면 재확인 실패: {}", e)

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

    def _run_post_cooldown_rescan(self):
        """블랙스완 쿨다운 해제 직후: 워치리스트 전 종목을 재스캔해 진입 후보를 다시 채운다."""
        try:
            from core.data_collector import DataCollector

            collector = DataCollector()
            strategy = self._get_strategy()
            watchlist = WatchlistManager(self.config).resolve()
            new_candidates = []

            for symbol in watchlist:
                try:
                    df = collector.fetch_stock(symbol)
                    if df.empty or len(df) < 30:
                        continue
                    signal_info = strategy.generate_signal(df, symbol=symbol)
                    if signal_info.get("signal") == "BUY" and not get_position(symbol, account_key=self.strategy_name):
                        avg_vol = None
                        if "volume" in df.columns and not df["volume"].empty:
                            avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1])
                        new_candidates.append({
                            "symbol": symbol,
                            "price": signal_info.get("close", 0),
                            "atr": signal_info.get("atr"),
                            "score": signal_info.get("score", 0),
                            "reason": "post-cooldown rescan",
                            "avg_daily_volume": avg_vol,
                        })
                except Exception as e:
                    logger.debug("쿨다운 후 재스캔 {} 실패: {}", symbol, e)

            if new_candidates:
                self._entry_candidates.extend(new_candidates)
                logger.info("쿨다운 해제 재스캔: {}개 매수 후보 추가 (recovery 사이징 적용 예정)", len(new_candidates))
            else:
                logger.info("쿨다운 해제 재스캔: 매수 신호 종목 없음")
        except Exception as e:
            logger.error("쿨다운 해제 재스캔 실패: {}", e)

    def _execute_entry_candidates(self):
        """
        장전 분석에서 쌓인 BUY 후보를 장중 첫 사이클에 실행.
        30분 이상 경과한 후보는 폐기하고, 현재 시그널을 재검증한다.
        """
        if not self._entry_candidates:
            return

        from core.order_executor import OrderExecutor
        from core.data_collector import DataCollector
        from core.market_regime import check_market_regime

        collector = DataCollector()
        regime_result = check_market_regime(self.config, collector)
        if not regime_result["allow_buys"]:
            logger.info(
                "시장 국면 [bearish] — 200일선 이탈 + 단기 모멘텀 하락 → 신규 매수 전면 중단, 진입 후보 실행 생략"
            )
            self._entry_candidates = []
            return
        regime_scale = regime_result["position_scale"]

        executor = OrderExecutor(self.config, account_key=self.strategy_name)
        strategy = self._get_strategy()
        remaining = []
        now = datetime.now()
        stale_minutes = 30  # 30분 이상 경과한 후보는 폐기

        for candidate in self._entry_candidates:
            symbol = candidate["symbol"]
            if get_position(symbol, account_key=self.strategy_name):
                continue

            # 오래된 후보 폐기
            candidate_time = candidate.get("timestamp", now)
            if (now - candidate_time).total_seconds() > stale_minutes * 60:
                logger.info("종목 {} 진입 후보 폐기 ({}분 초과)", symbol, stale_minutes)
                continue

            # 시그널 재검증: 현재 데이터로 BUY 시그널 유효한지 재확인
            try:
                df = collector.fetch_stock(symbol)
                if df.empty or len(df) < 30:
                    logger.info("종목 {} 진입 후보 폐기 (데이터 부족)", symbol)
                    continue
                signal_info = strategy.generate_signal(df, symbol=symbol)
                if signal_info.get("signal") != "BUY":
                    logger.info(
                        "종목 {} 진입 후보 폐기 (시그널 재검증 실패: {} → {})",
                        symbol, "BUY", signal_info.get("signal", "HOLD"),
                    )
                    continue
                # 재검증 통과 — 현재 가격으로 업데이트
                candidate["price"] = signal_info.get("close", candidate["price"])
                candidate["atr"] = signal_info.get("atr", candidate.get("atr"))
                candidate["score"] = signal_info.get("score", candidate.get("score", 0))
            except Exception as e:
                logger.warning("종목 {} 시그널 재검증 실패 — 후보 유지: {}", symbol, e)

            summary = self.portfolio.get_portfolio_summary()
            scale = candidate.get("market_regime_scale", regime_scale)
            # 블랙스완 recovery 기간 중이면 추가 축소
            bs_scale = self.blackswan.get_recovery_scale()
            scale = scale * bs_scale
            adjusted_capital = summary["total_value"] * scale
            adjusted_cash = summary["cash"] * scale
            if scale < 1.0:
                parts = []
                if regime_scale < 1.0:
                    parts.append(f"시장 국면 {regime_scale*100:.0f}%")
                if bs_scale < 1.0:
                    parts.append(f"블랙스완 recovery {bs_scale*100:.0f}%")
                logger.info("{} 포지션 사이징 {:.0f}%로 축소 ({})", symbol, scale * 100, " + ".join(parts))
            result = executor.execute_buy(
                symbol=symbol,
                price=candidate["price"],
                capital=adjusted_capital,
                available_cash=adjusted_cash,
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
        """포지션 순회: 갭다운 즉시 청산, 최대 보유 기간 초과, 블랙스완, 손절/익절/트레일링 스탑."""
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

        gap_cfg = (self.config.risk_params or {}).get("gap_risk", {})
        gap_enabled = gap_cfg.get("enabled", False)
        gap_down_threshold = float(gap_cfg.get("gap_down_threshold", -0.03))

        for pos in positions:
            try:
                price_info = kis.get_current_price(pos.symbol)
                if not price_info:
                    continue

                current_price = price_info["price"]
                prev_close = price_info.get("prev_close", pos.avg_price)

                # 갭다운 즉시 청산: 전일 종가 대비 시가가 크게 갭다운이면 손절 회피 불가
                if gap_enabled and prev_close > 0:
                    gap_pct = (current_price - prev_close) / prev_close
                    if gap_pct <= gap_down_threshold:
                        logger.warning(
                            "갭다운 청산 발동: {} 갭 {:.1f}% (기준 {:.0f}%)",
                            pos.symbol, gap_pct * 100, gap_down_threshold * 100,
                        )
                        result = executor.execute_sell(
                            pos.symbol, current_price,
                            reason=f"갭다운 {gap_pct*100:.1f}% 즉시 청산",
                            strategy=self.strategy_name,
                        )
                        if result.get("success"):
                            self.discord.send_message(
                                f"🚨 갭다운 청산: {pos.symbol} {gap_pct*100:.1f}%",
                                critical=True,
                            )
                            self.discord.send_trade_alert(result)
                        continue

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
                    self.discord.send_message(f"🚨 블랙스완 발동!\n{bs_result['reason']}", critical=True)
                    executor.execute_sell(
                        pos.symbol, current_price,
                        reason="블랙스완 긴급 매도",
                        strategy=self.strategy_name,
                    )
                    continue

                check = executor.check_stop_loss_take_profit(pos.symbol, current_price)
                if check["action"]:
                    sell_qty = check.get("partial_qty")  # 부분 익절 시에만 존재
                    result = executor.execute_sell(
                        pos.symbol, current_price,
                        quantity=sell_qty,
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

            # paper 모드: 장마감 시 실전 전환 준비 자동 평가
            if self.config.trading.get("mode") == "paper":
                self._check_live_readiness()

            # 일일 루프 모니터링 지표 기록
            metrics = self._loop_metrics.summary()
            logger.info("📊 일일 루프 지표: {}", metrics)
            if metrics["total_loops"] > 0 or metrics["total_skips"] > 0:
                self.discord.send_message(
                    "📊 **오늘 루프 지표**\n"
                    f"실행 {metrics['total_loops']}회 | 스킵 {metrics['total_skips']}회\n"
                    f"평균 소요 {metrics['avg_elapsed_s']}초 | 최대 소요 {metrics['max_elapsed_s']}초\n"
                    f"8분 초과(경고) {metrics['loops_over_8min']}회 | "
                    f"10분 초과(timeout) {metrics['loops_over_10min']}회"
                )

            logger.info("장마감 리포트 저장 및 발송 완료")

        except Exception as e:
            logger.error("장마감 리포트 실패: {}", e)

    def _check_live_readiness(self):
        """paper 모드 장마감 시 실전 전환 준비 자동 평가."""
        readiness_cfg = (
            self.config.risk_params.get("paper_backtest_compare", {})
            .get("live_readiness", {})
        )
        if not readiness_cfg.get("auto_check_in_scheduler", True):
            return
        try:
            from datetime import timedelta
            from backtest.paper_compare import check_live_readiness

            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            result = check_live_readiness(
                start_date=start_date,
                end_date=end_date,
                strategy_name=self.strategy_name,
                config=self.config,
            )
            if result["ready"]:
                logger.info("✅ 실전 전환 준비 완료 — {}", result["message"])
                if readiness_cfg.get("notify_on_ready", True):
                    self.discord.send_embed(
                        "✅ 실전 전환 준비 완료",
                        result["message"],
                        color=0x2ECC71,
                        fields=[
                            {"name": "방향성 일치율", "value": f"{result['direction_agreement_pct']:.1f}%", "inline": True},
                            {"name": "수익률 차이", "value": f"{result.get('return_diff_pct', 0):.1f}%", "inline": True},
                            {"name": "거래일수", "value": f"{result['trading_days']}일", "inline": True},
                        ],
                    )
            else:
                logger.info("⏳ 실전 전환 기준 미달 — {}", result["message"])
        except Exception as e:
            logger.warning("실전 전환 준비 평가 실패: {}", e)

    def _get_adaptive_interval(self) -> float:
        """
        적응형 모니터링 주기 (과매매 방지).
        - 장 시작/종료 부근 (09:00~10:30, 14:30~15:30): 10분 (빠른 반응)
        - 장중 안정 시간대 (10:30~14:30): 20분 (과매매 방지)
        """
        now = datetime.now()
        ct = now.time()
        from datetime import time
        # 장 초반·마감 부근: 10분
        if ct < time(10, 30) or ct >= time(14, 30):
            return 600.0
        # 장중 안정 구간: 20분
        return 1200.0

    def _should_monitor(self) -> bool:
        """모니터링 주기 확인. 적응형 주기 사용. 이전 루프 10분 초과 시 다음 사이클 스킵."""
        if self._skip_next_monitor_cycle:
            self._skip_next_monitor_cycle = False
            self._loop_metrics.record_skip()
            logger.warning("이전 장중 루프 지연으로 이번 모니터링 사이클을 스킵합니다. (10분 초과 안전장치)")
            return False
        if self._last_monitor_time is None:
            return True
        self.monitor_interval = self._get_adaptive_interval()
        elapsed = (datetime.now() - self._last_monitor_time).total_seconds()
        return elapsed >= self.monitor_interval

    def _sleep_until_next_trading_day(self):
        """다음 거래일까지 대기."""
        wait = self.trading_hours.time_until_market_open()
        hours = wait.total_seconds() / 3600
        logger.info("비거래일 — 다음 거래일까지 {:.1f}시간 대기", hours)
        time_mod.sleep(min(wait.total_seconds(), 3600))

    # =============================================================
    # 시스템 헬스체크
    # =============================================================

    def _maybe_run_healthcheck(self):
        """10분 주기로 시스템 헬스체크 실행."""
        now = datetime.now()
        if self._last_healthcheck_time is not None:
            elapsed = (now - self._last_healthcheck_time).total_seconds()
            if elapsed < 600:
                return
        self._last_healthcheck_time = now
        issues = self._run_healthcheck()
        if issues:
            msg = "🏥 헬스체크 이상 항목:\n" + "\n".join(f"  - {i}" for i in issues)
            logger.warning(msg)
            self.discord.send_message(msg, critical=True)

    def _run_healthcheck(self) -> list[str]:
        """
        시스템 상태 점검. 이상 항목 문자열 리스트를 반환합니다.
        검사 항목: DB 연결, 디스크 여유, KIS API 인증(live), 메모리 사용량.
        """
        issues = []

        # 1) DB 연결 검사
        try:
            from database.models import get_session
            session = get_session()
            session.execute("SELECT 1")
            session.remove()
        except Exception as e:
            issues.append(f"DB 연결 실패: {e}")

        # 2) 디스크 여유 공간 검사
        try:
            db_path = self.config.database.get("sqlite_path", "data/quant_trader.db")
            disk = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)))
            free_gb = disk.free / (1024 ** 3)
            if free_gb < 1.0:
                issues.append(f"디스크 여유 공간 부족: {free_gb:.1f}GB")
        except Exception:
            pass

        # 3) KIS API 인증 상태 (live 모드)
        if self.config.trading.get("mode") == "live":
            try:
                from api.kis_api import KISApi
                kis = KISApi()
                if not getattr(kis, "_access_token", None):
                    issues.append("KIS API 토큰 없음 — 재인증 필요")
            except Exception as e:
                issues.append(f"KIS API 초기화 실패: {e}")

        # 4) 메모리 사용량 검사
        try:
            import psutil
            mem = psutil.virtual_memory()
            if mem.percent > 90:
                issues.append(f"메모리 사용률 높음: {mem.percent}%")
        except ImportError:
            pass
        except Exception:
            pass

        if not issues:
            logger.debug("헬스체크 정상")
        return issues

    # =============================================================
    # 휴장일 자동 갱신
    # =============================================================

    def _maybe_update_holidays(self):
        """
        새해 또는 holidays.yaml이 오래된 경우 자동 갱신.
        연초(1/1~1/7) 또는 파일 수정일이 90일 이상 지난 경우 트리거.
        """
        from pathlib import Path
        holidays_path = Path("config/holidays.yaml")

        needs_update = False
        now = datetime.now()

        if not holidays_path.exists():
            needs_update = True
        else:
            try:
                mtime = datetime.fromtimestamp(holidays_path.stat().st_mtime)
                age_days = (now - mtime).days
                if age_days > 90:
                    needs_update = True
                    logger.info("holidays.yaml 마지막 수정 {}일 전 → 갱신 시도", age_days)
                elif now.month == 1 and now.day <= 7:
                    if mtime.year < now.year:
                        needs_update = True
                        logger.info("새해 — holidays.yaml 갱신 시도")
            except Exception:
                needs_update = True

        if needs_update:
            try:
                from core.holidays_updater import update_holidays_yaml
                result_path = update_holidays_yaml()
                logger.info("휴장일 자동 갱신 완료: {}", result_path)
                self.trading_hours = TradingHours(self.config)
            except Exception as e:
                logger.warning("휴장일 자동 갱신 실패 (기존 파일 유지): {}", e)

    def _run_basket_rebalance_check(self):
        """장전 단계에서 enabled 바스켓의 리밸런싱 필요 여부를 체크하고 실행."""
        try:
            from core.basket_rebalancer import BasketRebalancer

            enabled = BasketRebalancer.get_enabled_baskets()
            if not enabled:
                return

            logger.info("🔄 바스켓 리밸런싱 체크: {}", enabled)
            for name in enabled:
                try:
                    rebalancer = BasketRebalancer(
                        basket_name=name, config=self.config,
                        account_key=self.strategy_name,
                    )
                    should, reason = rebalancer.should_rebalance()
                    if not should:
                        logger.info("바스켓 '{}' 리밸런싱 불필요: {}", name, reason)
                        continue

                    orders = rebalancer.plan_rebalance()
                    if not orders:
                        continue

                    result = rebalancer.execute(orders)
                    summary = (
                        f"🔄 바스켓 '{name}' 리밸런싱 완료: "
                        f"실행 {result['executed']}건, 실패 {result['failed']}건"
                    )
                    logger.info(summary)
                    self.discord.send_message(summary)

                except Exception as e:
                    logger.error("바스켓 '{}' 리밸런싱 오류: {}", name, e)

        except ImportError:
            pass
        except Exception as e:
            logger.error("바스켓 리밸런싱 체크 실패: {}", e)

    def _log_rate_limit_preflight(self, est_requests: int, n_symbols: int, phase: str):
        """API 요청 사전 예측: 예상 건수와 소요 시간을 로그하고, 분당 한도 초과 시 경고."""
        try:
            from api.kis_api import KISApi
            kis = KISApi()
            per_sec = kis.max_calls_per_sec
            per_min = kis.max_calls_per_min
            est_seconds = est_requests / per_sec if per_sec > 0 else 0
            logger.info(
                "[{}] 종목 {}개, 예상 API 요청 ~{}건 (초당 {:.0f}건 → 약 {:.0f}초 소요)",
                phase, n_symbols, est_requests, per_sec, est_seconds,
            )
            if est_requests > per_min:
                logger.warning(
                    "[{}] 예상 요청 {}건 > 분당 한도 {}건. 분당 슬라이딩 윈도우에 의해 자동 대기 발생 가능.",
                    phase, est_requests, per_min,
                )
        except Exception:
            pass

    def _log_rate_limit_stats(self, phase: str):
        """API 요청 사후 통계를 로그한다."""
        try:
            from api.kis_api import KISApi
            kis = KISApi()
            stats = kis.get_rate_limit_stats()
            logger.info(
                "[{}] API 사용량: 최근 60초 {}/{}건 (활용률 {:.0f}%), 누적 {}건, 429 {}회",
                phase,
                stats["requests_last_60s"], stats["max_per_min"],
                stats["minute_utilization_pct"],
                stats["total_requests"], stats["total_429s"],
            )
        except Exception:
            pass

    def _update_dynamic_stop_losses(self):
        """
        보유 종목의 ATR이 변화하면 손절가를 재계산하여 DB에 반영.
        변동성이 확대된 종목은 손절가를 더 넓게, 축소된 종목은 타이트하게 조정.
        기존 손절가보다 낮아지는(불리해지는) 경우에는 변경하지 않음 (래칫: 항상 유리한 방향만).
        """
        from core.risk_manager import RiskManager
        from database.repositories import update_stop_loss_price

        positions = get_all_positions(account_key=self.strategy_name)
        if not positions:
            return

        try:
            from core.data_collector import DataCollector
            from core.market_regime import get_regime_adjusted_params

            collector = DataCollector()
            rm = RiskManager(self.config)
            regime_adj = get_regime_adjusted_params(self.config, collector)
            regime_mult = regime_adj.get("stop_loss_multiplier", 1.0)

            for pos in positions:
                try:
                    df = collector.fetch_stock(pos.symbol)
                    if df is None or df.empty or "close" not in df.columns:
                        continue
                    if "atr" not in df.columns:
                        from core.indicator_engine import IndicatorEngine
                        ie = IndicatorEngine(self.config)
                        df = ie.calculate_all(df)
                    if "atr" not in df.columns:
                        continue

                    current_atr = float(df["atr"].iloc[-1])
                    if current_atr <= 0:
                        continue

                    new_sl = rm.calculate_stop_loss(
                        pos.avg_price, atr=current_atr, regime_multiplier=regime_mult,
                    )

                    old_sl = getattr(pos, "stop_loss_price", 0) or 0
                    if new_sl > old_sl:
                        update_stop_loss_price(
                            pos.symbol, new_sl, account_key=self.strategy_name,
                        )
                        logger.debug(
                            "동적 손절가 갱신: {} {:,.0f} → {:,.0f} (ATR={:.0f})",
                            pos.symbol, old_sl, new_sl, current_atr,
                        )
                except Exception as e:
                    logger.debug("동적 손절가 업데이트 실패 {}: {}", pos.symbol, e)
        except Exception as e:
            logger.debug("동적 손절가 전체 업데이트 스킵: {}", e)

    def _rescan_for_new_entries(self):
        """장중 신호 재평가: 새로운 매수 기회를 탐색하여 진입 후보에 추가."""
        try:
            from core.data_collector import DataCollector
            from core.market_regime import check_market_regime

            regime = check_market_regime(self.config)
            if not regime["allow_buys"]:
                return

            collector = DataCollector()
            strategy = self._get_strategy()
            watchlist = WatchlistManager(self.config).resolve()

            for symbol in watchlist:
                if get_position(symbol, account_key=self.strategy_name):
                    continue
                if any(c["symbol"] == symbol for c in self._entry_candidates):
                    continue

                try:
                    df = collector.fetch_stock(symbol)
                    if df is None or df.empty or len(df) < 30:
                        continue

                    signal_info = strategy.generate_signal(df, symbol=symbol)
                    if signal_info.get("signal") != "BUY":
                        continue

                    avg_vol = None
                    if "volume" in df.columns and not df["volume"].empty:
                        avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1])

                    self._entry_candidates.append({
                        "symbol": symbol,
                        "price": signal_info.get("close", 0),
                        "atr": signal_info.get("atr"),
                        "score": signal_info.get("score", 0),
                        "reason": "intraday rescan",
                        "avg_daily_volume": avg_vol,
                        "market_regime_scale": regime["position_scale"],
                    })
                    logger.info("장중 재스캔: {} 매수 신호 감지 (score={})", symbol, signal_info.get("score", 0))
                except Exception:
                    continue

        except Exception as e:
            logger.debug("장중 재스캔 실패: {}", e)

    def _get_strategy(self):
        """전략 레지스트리를 통해 인스턴스 반환."""
        from strategies import create_strategy
        return create_strategy(self.strategy_name, self.config)

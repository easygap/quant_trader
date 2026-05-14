"""
주문 실행 모듈
- 실제 주문(KIS API) 또는 페이퍼 트레이딩(시뮬레이션) 실행
- 매수/매도 주문 처리
- 거래시간 체크 / 블랙스완 감지 연동
- 주문 실패 시 재시도 (최대 3회, 지수 백오프)
- 결과를 DB에 기록
- 재시작 복구: `reconcile_open_orders_after_crash()`로 KIS 미체결 목록 확인·로깅 (잔고 정합은 `PortfolioManager.sync_with_broker`)
"""

import math
import time as time_mod
from datetime import datetime, timedelta
from loguru import logger

from config.config_loader import Config
from api.kis_api import KISApi
from core.risk_manager import RiskManager
from database.repositories import (
    save_trade, save_position, delete_position, reduce_position,
    get_position, get_all_positions, save_failed_order, count_monthly_buy_trades,
    save_order_record, get_open_order_records, reconcile_order_record,
)
from monitoring.logger import log_trade
from core.order_guard import OrderGuard
from core.position_lock import PositionLock
from core.order_state import OrderBook, OrderStatus

try:
    from monitoring.paper_monitor import log_event as _log_op_event
except ImportError:
    def _log_op_event(*a, **kw): pass


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
    # risk_params.slippage 기본 0.05% 대비 3배 초과 시 warning, 1% 초과 시 디스코드
    SLIPPAGE_WARN_PCT = 0.15
    SLIPPAGE_DISCORD_PCT = 1.0

    def __init__(
        self,
        config: Config = None,
        account_key: str = "",
        *,
        live_gate_validated: bool = False,
    ):
        self.config = config or Config.get()
        self.account_key = account_key or ""
        self.mode = self.config.trading.get("mode", "paper")
        self.live_gate_validated = bool(live_gate_validated)
        self.risk_manager = RiskManager(self.config)
        account_no = self.config.get_account_no(self.account_key) if self.account_key else None
        self.kis_api = KISApi(account_no=account_no)

        # 거래 시간 / 블랙스완 체크 모듈
        from core.trading_hours import TradingHours
        from core.blackswan_detector import BlackSwanDetector
        self.trading_hours = TradingHours(self.config)
        self.blackswan = BlackSwanDetector(self.config)

        self._sector_map: dict | None = None
        self.order_book = OrderBook()
        self.last_open_order_reconcile_status: dict = {
            "checked": None,
            "reason": "not_run",
            "orders": [],
        }
        self._restore_persistent_open_orders()

        if self.mode == "live":
            self.kis_api.authenticate()

        logger.info("OrderExecutor 초기화 완료 (모드: {})", self.mode)

    def _live_buy_gate_check(self, action: str = "BUY") -> dict:
        """live 신규 BUY는 canonical live gate 통과 경로에서만 허용한다."""
        if self.mode != "live" or str(action).upper() != "BUY":
            return {"allowed": True, "reason": ""}
        if self.live_gate_validated:
            return {"allowed": True, "reason": ""}

        reason = (
            "live BUY는 run_live_trading/live rebalance의 readiness gate를 "
            "통과한 OrderExecutor에서만 실행할 수 있습니다."
        )
        logger.error("실전 신규 매수 차단: {}", reason)
        return {
            "allowed": False,
            "reason": reason,
            "live_gate_blocked": True,
            "mode": self.mode,
        }

    def _restore_persistent_open_orders(self) -> None:
        if self.mode != "live":
            return
        try:
            records = get_open_order_records(account_key=self.account_key, mode=self.mode)
            if records:
                self.order_book.restore_from_records(records)
                logger.warning(
                    "DB 미완료 주문 상태 복구: {}건 — 신규 주문 전 브로커 재조정 필요",
                    len(records),
                )
        except Exception as e:
            logger.error("DB 미완료 주문 상태 복구 실패: {}", e)

    def _persist_order_record(self, order) -> None:
        if self.mode != "live":
            return
        save_order_record(order)

    def _persistent_live_order_block(self, symbol: str, order) -> dict | None:
        if self.mode != "live":
            return None
        try:
            open_records = get_open_order_records(
                symbol=symbol,
                account_key=self.account_key,
                mode=self.mode,
            )
        except Exception as e:
            order.transition(OrderStatus.REJECTED, reason="DB 미완료 주문 조회 실패")
            logger.error("DB 미완료 주문 조회 실패 — 실전 주문 차단: {}", e)
            return {
                "success": False,
                "reason": "DB 미완료 주문 상태를 확인하지 못해 실전 중복 주문 방지를 위해 주문을 보류했습니다.",
                "symbol": symbol,
                "mode": self.mode,
                "requires_reconcile": True,
                "persistent_order_block": True,
                "open_order_records": [],
            }
        if not open_records:
            return None
        order.transition(OrderStatus.REJECTED, reason="DB 미완료 주문 상태 존재")
        self._persist_order_record(order)
        return {
            "success": False,
            "reason": f"{symbol} 종목에 재조정되지 않은 실전 주문 상태가 남아 있어 중복 주문을 차단했습니다.",
            "symbol": symbol,
            "mode": self.mode,
            "requires_reconcile": True,
            "persistent_order_block": True,
            "open_order_records": open_records,
        }

    def _get_sector_map_cached(self) -> dict:
        """업종 매핑을 한 번만 조회하고 캐시한다. 실패 시 빈 dict."""
        if self._sector_map is None:
            try:
                from core.data_collector import DataCollector
                self._sector_map = DataCollector.get_sector_map()
            except Exception:
                self._sector_map = {}
        return self._sector_map

    def _is_paper_like_mode(self) -> bool:
        return self.mode in ("paper", "schedule")

    def _resolve_guard_strategy(self, strategy: str = "") -> str:
        """Paper runtime/preflight 조회에 사용할 전략명을 결정한다."""
        return str(strategy or self.account_key or "").strip()

    def _runtime_block_detail(self, strategy: str, rt_state) -> str:
        metrics = getattr(rt_state, "metrics", {}) or {}
        allowed_actions = getattr(rt_state, "allowed_actions", []) or []
        reasons = getattr(rt_state, "reasons", []) or []
        parts = [
            f"state={getattr(rt_state, 'state', 'unknown')}",
            f"strategy={strategy}",
            f"evidence_date={getattr(rt_state, 'evidence_date', None) or 'N/A'}",
            f"allowed_actions={','.join(allowed_actions) or 'none'}",
        ]
        if "recent_final_ratio" in metrics:
            parts.append(f"benchmark_final_ratio={metrics.get('recent_final_ratio')}")
        if "recent_anomaly_count" in metrics:
            parts.append(f"anomaly_count={metrics.get('recent_anomaly_count')}")
        if reasons:
            parts.append("reasons=" + "; ".join(str(r) for r in reasons))
        return " | ".join(parts)

    def _block_paper_entry(
        self,
        reason: str,
        strategy: str = "",
        event_type: str = "RUNTIME_BLOCK",
        detail: dict | None = None,
    ) -> dict:
        logger.warning("Paper 신규 진입 차단: {}", reason)
        try:
            _log_op_event(
                event_type,
                f"entry blocked: {reason}",
                severity="warning",
                strategy=strategy or None,
                mode=self.mode,
                detail=detail or {},
            )
        except Exception as exc:
            logger.debug("Paper entry block 이벤트 기록 실패: {}", exc)
        return {"allowed": False, "reason": reason, "paper_entry_blocked": True}

    def _paper_entry_pre_order_check(
        self,
        action: str = "BUY",
        strategy: str = "",
        candidate_notional: float | None = None,
    ) -> dict:
        """Paper/schedule 신규 진입은 runtime/preflight가 확인될 때만 허용한다."""
        if not self._is_paper_like_mode() or str(action).upper() != "BUY":
            return {"allowed": True, "reason": ""}

        strategy_name = self._resolve_guard_strategy(strategy)
        if not strategy_name:
            return self._block_paper_entry(
                "paper entry guard requires strategy",
                strategy=strategy_name,
                event_type="CONFIG_ERROR",
            )

        try:
            from core.paper_preflight import load_preflight_status
            preflight = load_preflight_status(strategy_name, strict=True)
        except Exception as exc:
            return self._block_paper_entry(
                f"paper preflight status unavailable: {exc}",
                strategy=strategy_name,
                event_type="PREFLIGHT_BLOCK",
                detail={"strategy": strategy_name, "error": str(exc)},
            )

        if preflight is None:
            return self._block_paper_entry(
                "paper preflight status missing",
                strategy=strategy_name,
                event_type="PREFLIGHT_BLOCK",
                detail={"strategy": strategy_name},
            )

        if getattr(preflight, "overall", None) == "fail":
            block_reasons = getattr(preflight, "block_reasons", []) or []
            reason = "; ".join(str(r) for r in block_reasons) or getattr(preflight, "overall", "fail")
            return self._block_paper_entry(
                f"paper preflight blocked entry: {reason}",
                strategy=strategy_name,
                event_type="PREFLIGHT_BLOCK",
                detail={
                    "strategy": strategy_name,
                    "overall": getattr(preflight, "overall", None),
                    "runtime_state": getattr(preflight, "runtime_state", None),
                    "block_reasons": block_reasons,
                },
            )

        try:
            from core.paper_runtime import get_paper_runtime_state
            rt_state = get_paper_runtime_state(strategy_name)
        except Exception as exc:
            return self._block_paper_entry(
                f"paper runtime state unavailable: {exc}",
                strategy=strategy_name,
                event_type="RUNTIME_BLOCK",
                detail={"strategy": strategy_name, "error": str(exc)},
            )

        allowed_actions = getattr(rt_state, "allowed_actions", []) or []
        if "entry" in allowed_actions:
            return {"allowed": True, "reason": ""}

        pilot_check = None
        try:
            from core.paper_pilot import check_pilot_entry
            pilot_check = check_pilot_entry(
                strategy_name,
                candidate_notional=float(candidate_notional or 0),
            )
        except Exception as exc:
            detail = {
                "strategy": strategy_name,
                "runtime_state": getattr(rt_state, "state", None),
                "pilot_error": str(exc),
            }
            return self._block_paper_entry(
                f"paper runtime blocked entry and pilot check failed: {exc}",
                strategy=strategy_name,
                detail=detail,
            )

        if getattr(pilot_check, "allowed", False):
            try:
                _log_op_event(
                    "PILOT_ENTRY_ALLOWED",
                    f"pilot override: {getattr(pilot_check, 'reason', '')}",
                    severity="info",
                    strategy=strategy_name,
                    mode=self.mode,
                    detail=getattr(pilot_check, "caps_snapshot", None) or {},
                )
            except Exception as exc:
                logger.debug("Pilot 허용 이벤트 기록 실패: {}", exc)
            return {"allowed": True, "reason": "paper pilot allowed entry"}

        block_detail = self._runtime_block_detail(strategy_name, rt_state)
        if pilot_check is not None:
            block_detail = f"{block_detail} | pilot={getattr(pilot_check, 'reason', '')}"
        return self._block_paper_entry(
            f"paper runtime blocked entry: {block_detail}",
            strategy=strategy_name,
            detail={
                "strategy": strategy_name,
                "state": getattr(rt_state, "state", None),
                "allowed_actions": allowed_actions,
                "reasons": getattr(rt_state, "reasons", []) or [],
            },
        )

    def _monthly_buy_cap_check(self, symbol: str, action: str = "BUY") -> dict:
        """종목별 월간 BUY 체결 횟수 제한을 운영 주문에도 적용한다."""
        if str(action).upper() != "BUY":
            return {"allowed": True, "reason": ""}

        try:
            limit = int(
                ((self.config.risk_params or {}).get("position_limits") or {})
                .get("max_monthly_roundtrips", 0)
                or 0
            )
        except Exception:
            limit = 0
        if limit <= 0:
            return {"allowed": True, "reason": ""}

        try:
            current_count = count_monthly_buy_trades(
                symbol,
                mode=self.mode,
                account_key=self.account_key if self.account_key else "",
            )
        except Exception as exc:
            reason = f"월간 거래 횟수 확인 실패: {exc}"
            logger.warning("종목 {} 매수 차단: {}", symbol, reason)
            return {"allowed": False, "reason": reason}

        if current_count >= limit:
            reason = (
                f"월간 거래 횟수 제한 초과: {symbol} "
                f"{current_count}/{limit}회 BUY 체결"
            )
            logger.warning("종목 {} 매수 차단: {}", symbol, reason)
            return {
                "allowed": False,
                "reason": reason,
                "monthly_buy_cap_blocked": True,
                "monthly_buy_count": current_count,
                "monthly_buy_limit": limit,
            }
        return {"allowed": True, "reason": ""}

    def _daily_loss_baseline_value(self) -> float | None:
        """최근 포트폴리오 스냅샷에서 당일 손실 비교 기준값을 가져온다."""
        from database.repositories import get_portfolio_snapshots

        snapshots = get_portfolio_snapshots(
            days=10,
            account_key=self.account_key if self.account_key else None,
        )
        if snapshots.empty:
            return None

        baseline_rows = snapshots
        if "date" in snapshots.columns:
            try:
                today = datetime.now().date()
                dated = snapshots.copy()
                dated["_snapshot_date"] = dated["date"].apply(
                    lambda value: value.date() if hasattr(value, "date") else value
                )
                previous_rows = dated[dated["_snapshot_date"] < today]
                if not previous_rows.empty:
                    baseline_rows = previous_rows
            except Exception as exc:
                logger.debug("일일 손실 기준 스냅샷 날짜 해석 실패: {}", exc)

        latest = baseline_rows.iloc[-1]
        baseline_value = float(latest.get("total_value") or 0)
        return baseline_value if baseline_value > 0 else None

    def _drawdown_pre_order_check(self, action: str = "BUY") -> dict:
        """MDD/일일 손실 한도 도달 시 신규 BUY만 차단한다."""
        if str(action).upper() != "BUY":
            return {"allowed": True, "reason": ""}

        drawdown_cfg = (self.config.risk_params or {}).get("drawdown") or {}
        max_mdd = float(drawdown_cfg.get("max_portfolio_mdd") or 0)
        max_daily_loss = float(drawdown_cfg.get("max_daily_loss") or 0)
        if max_mdd <= 0 and max_daily_loss <= 0:
            return {"allowed": True, "reason": ""}

        try:
            from core.portfolio_manager import PortfolioManager

            summary = PortfolioManager(
                self.config,
                account_key=self.account_key,
            ).get_portfolio_summary()
            if self.mode == "live" and summary.get("broker_balance_ok") is False:
                reason = (
                    "손실 한도 확인 실패: KIS 잔고 조회가 확인되지 않아 "
                    "DB 기준 평가금액으로 신규 매수를 판단하지 않습니다."
                )
                logger.warning("신규 매수 차단: {}", reason)
                return {
                    "allowed": False,
                    "reason": reason,
                    "drawdown_guard_blocked": True,
                    "drawdown_guard_type": "broker_balance_unavailable",
                    "broker_balance_source": summary.get("broker_balance_source"),
                    "broker_balance_error": summary.get("broker_balance_error"),
                    "mode": self.mode,
                }
            total_value = float(summary.get("total_value") or 0)
            if total_value <= 0:
                reason = "손실 한도 확인 실패: 포트폴리오 평가금액 없음"
                logger.warning("신규 매수 차단: {}", reason)
                return {
                    "allowed": False,
                    "reason": reason,
                    "drawdown_guard_blocked": True,
                    "drawdown_guard_type": "unavailable",
                    "mode": self.mode,
                }

            mdd_pct = abs(float(summary.get("mdd") or 0))
            mdd_limit_pct = max_mdd * 100
            if max_mdd > 0 and mdd_pct >= mdd_limit_pct:
                reason = f"MDD 한도 도달: {mdd_pct:.2f}% >= {mdd_limit_pct:.2f}%"
                logger.warning("신규 매수 차단: {}", reason)
                return {
                    "allowed": False,
                    "reason": reason,
                    "drawdown_guard_blocked": True,
                    "drawdown_guard_type": "mdd",
                    "mdd": round(mdd_pct, 2),
                    "mdd_limit": round(mdd_limit_pct, 2),
                    "mode": self.mode,
                }

            baseline_value = self._daily_loss_baseline_value()
            if max_daily_loss > 0 and baseline_value:
                daily_pnl = total_value - baseline_value
                daily_allowed = self.risk_manager.check_daily_loss(
                    daily_pnl,
                    baseline_value,
                )
                if not daily_allowed:
                    daily_loss_pct = abs(daily_pnl) / baseline_value * 100
                    daily_limit_pct = max_daily_loss * 100
                    reason = (
                        f"일일 손실 한도 도달: "
                        f"{daily_loss_pct:.2f}% >= {daily_limit_pct:.2f}%"
                    )
                    logger.warning("신규 매수 차단: {}", reason)
                    return {
                        "allowed": False,
                        "reason": reason,
                        "drawdown_guard_blocked": True,
                        "drawdown_guard_type": "daily_loss",
                        "daily_loss": round(daily_loss_pct, 2),
                        "daily_loss_limit": round(daily_limit_pct, 2),
                        "daily_loss_baseline": round(baseline_value, 0),
                        "mode": self.mode,
                    }
        except Exception as exc:
            reason = f"손실 한도 확인 실패: {exc}"
            logger.exception("신규 매수 차단: {}", reason)
            return {
                "allowed": False,
                "reason": reason,
                "drawdown_guard_blocked": True,
                "drawdown_guard_type": "unavailable",
                "mode": self.mode,
            }

        return {"allowed": True, "reason": ""}

    def _entry_liquidity_check(
        self,
        symbol: str,
        price: float,
        avg_daily_volume: float | None,
        action: str = "BUY",
    ) -> dict:
        """주문 직전 유동성 데이터가 없거나 하한 미달이면 신규 BUY를 차단한다."""
        if str(action).upper() != "BUY":
            return {"allowed": True, "reason": ""}

        liq = (self.config.risk_params or {}).get("liquidity_filter") or {}
        if not (liq.get("enabled", False) and liq.get("check_on_entry", True)):
            return {"allowed": True, "reason": ""}

        try:
            current_price = float(price or 0)
        except (TypeError, ValueError):
            current_price = 0.0
        if current_price <= 0:
            reason = f"유동성 확인 실패: {symbol} 현재가 없음"
            logger.warning("종목 {} 매수 스킵 (유동성): {}", symbol, reason)
            return {"allowed": False, "reason": reason, "liquidity_blocked": True}

        try:
            avg_volume = float(avg_daily_volume)
        except (TypeError, ValueError):
            avg_volume = 0.0
        if avg_volume <= 0:
            reason = f"유동성 확인 실패: {symbol} 20일 평균 거래량 데이터 없음"
            logger.warning("종목 {} 매수 스킵 (유동성): {}", symbol, reason)
            return {"allowed": False, "reason": reason, "liquidity_blocked": True}

        min_krw = float(liq.get("min_avg_trading_value_20d_krw", 5e9))
        est_trading_value = avg_volume * current_price
        if est_trading_value < min_krw:
            reason = (
                f"유동성 부족: 추정 일평균 거래대금 {est_trading_value/1e8:.0f}억 원 "
                f"< 하한 {min_krw/1e8:.0f}억 원"
            )
            logger.warning("종목 {} 매수 스킵 (유동성): {}", symbol, reason)
            return {
                "allowed": False,
                "reason": reason,
                "liquidity_blocked": True,
                "avg_daily_volume": avg_volume,
                "estimated_avg_trading_value_20d_krw": est_trading_value,
                "min_avg_trading_value_20d_krw": min_krw,
            }

        return {"allowed": True, "reason": ""}

    def _gap_up_entry_check(self, symbol: str, price: float) -> dict:
        """갭업 추격매수 방지용 최근 가격 조회는 실패 시 신규 BUY를 차단한다."""
        gap_cfg = (self.config.risk_params or {}).get("gap_risk", {})
        if not (gap_cfg.get("enabled", False) and gap_cfg.get("gap_up_entry_block", 0) > 0):
            return {"allowed": True, "reason": ""}

        try:
            from core.data_collector import DataCollector

            df_recent = DataCollector().fetch_stock(symbol)
        except Exception as exc:
            reason = f"갭 리스크 확인 실패: {symbol} 최근 가격 조회 실패 ({exc})"
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason)
            return {"allowed": False, "reason": reason, "gap_risk_blocked": True}

        if df_recent is None or len(df_recent) < 2 or "close" not in df_recent.columns:
            reason = f"갭 리스크 확인 실패: {symbol} 최근 가격 데이터 부족"
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason)
            return {"allowed": False, "reason": reason, "gap_risk_blocked": True}

        try:
            prev_close = float(df_recent["close"].iloc[-2])
            today_open = (
                float(df_recent["open"].iloc[-1])
                if "open" in df_recent.columns
                else float(price or 0)
            )
        except (TypeError, ValueError) as exc:
            reason = f"갭 리스크 확인 실패: {symbol} 가격 데이터 해석 실패 ({exc})"
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason)
            return {"allowed": False, "reason": reason, "gap_risk_blocked": True}

        if (
            not math.isfinite(prev_close)
            or not math.isfinite(today_open)
            or prev_close <= 0
            or today_open <= 0
        ):
            reason = f"갭 리스크 확인 실패: {symbol} 기준 가격 없음"
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason)
            return {"allowed": False, "reason": reason, "gap_risk_blocked": True}

        gap_pct = (today_open - prev_close) / prev_close
        if gap_pct >= gap_cfg["gap_up_entry_block"]:
            reason = (
                f"갭업 +{gap_pct*100:.1f}% "
                f"(기준 +{gap_cfg['gap_up_entry_block']*100:.0f}%) — 추격매수 차단"
            )
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason)
            return {"allowed": False, "reason": reason, "gap_risk_blocked": True}

        return {"allowed": True, "reason": ""}

    @staticmethod
    def _is_emergency_sell_reason(reason: str) -> bool:
        """손실 방어용 청산은 최소 보유 기간보다 우선한다."""
        text = str(reason or "").strip()
        normalized = text.upper()
        if normalized in {"STOP_LOSS", "TRAILING_STOP", "GAP_DOWN", "BLACKSWAN"}:
            return True
        emergency_keywords = (
            "블랙스완",
            "갭다운",
            "긴급 전량 청산",
            "--mode liquidate",
            "강제 청산",
        )
        return any(keyword in text for keyword in emergency_keywords)

    @staticmethod
    def _positive_order_price(price) -> float | None:
        """주문·손절 판단에 쓸 수 있는 양수 가격만 통과시킨다."""
        try:
            order_price = float(price)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(order_price) or order_price <= 0:
            return None
        return order_price

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
        signal_at: "datetime | None" = None,
        execution_session_id: str = "",
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
                symbol, price, capital, available_cash, current_invested, atr, signal_score, reason, strategy, avg_daily_volume, signal_at=signal_at, execution_session_id=execution_session_id
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
        signal_at: "datetime | None" = None,
        execution_session_id: str = "",
    ) -> dict:
        """매수 주문 실제 로직 (Lock 내부에서 호출)."""
        live_gate_check = self._live_buy_gate_check("BUY")
        if not live_gate_check["allowed"]:
            return {"success": False, **live_gate_check}

        order_price = self._positive_order_price(price)
        if order_price is None:
            reason_text = f"매수 가격 확인 실패: {symbol} 현재가 없음"
            logger.warning("종목 {} 매수 스킵: {}", symbol, reason_text)
            return {"success": False, "reason": reason_text, "price_invalid": True}
        price = order_price

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

        sizing_costs = self.risk_manager.calculate_transaction_costs(
            price,
            1,
            "BUY",
            avg_daily_volume=avg_daily_volume,
        )
        sizing_entry_price = float(sizing_costs.get("execution_price", price) or price)

        # 손절가 계산 (국면 배수 적용). 수량 산정은 예상 체결가 기준으로 보수화한다.
        stop_loss = self.risk_manager.calculate_stop_loss(
            sizing_entry_price,
            atr,
            regime_multiplier=regime_sl_mult,
        )

        # 포지션 크기 계산 (1% 룰 + 신호 강도 스케일링)
        quantity = self.risk_manager.calculate_position_size(
            capital,
            sizing_entry_price,
            stop_loss,
            signal_score=signal_score,
        )

        if quantity <= 0:
            logger.warning("종목 {} 매수 수량 0 — 주문 스킵", symbol)
            return {"success": False, "reason": "계산된 수량이 0"}

        # 상관관계 기반 포지션 축소
        positions = get_all_positions(account_key=self.account_key if self.account_key else None)
        existing_symbols = [p.symbol for p in positions]
        corr_result = self.risk_manager.check_correlation_risk(symbol, existing_symbols)
        if corr_result.get("blocked"):
            logger.warning("종목 {} 매수 스킵: {}", symbol, corr_result["reason"])
            return {
                "success": False,
                "reason": corr_result["reason"],
                "correlation_risk_blocked": True,
            }
        if corr_result["scale"] < 1.0:
            scaled_qty = max(1, int(quantity * corr_result["scale"]))
            logger.info(
                "종목 {} 상관관계 축소: {}주 → {}주 ({})",
                symbol, quantity, scaled_qty, corr_result["reason"],
            )
            quantity = scaled_qty

        # 갭 리스크 체크: 당일 시가가 전일 종가 대비 큰 폭 갭업이면 매수 차단
        gap_check = self._gap_up_entry_check(symbol, price)
        if not gap_check["allowed"]:
            return {"success": False, "reason": gap_check["reason"]}

        # 실적 발표일 필터: 전후 N일 이내이면 신규 매수 금지
        skip_earnings_days = int(self.config.trading.get("skip_earnings_days", 0))
        if skip_earnings_days > 0:
            from core.earnings_filter import is_near_earnings
            near, reason_earn = is_near_earnings(symbol, skip_days=skip_earnings_days)
            if near:
                logger.warning("종목 {} 매수 스킵 (실적 필터): {}", symbol, reason_earn)
                return {"success": False, "reason": reason_earn}

        # 분산 투자 체크 (업종 비중 포함, 상관 체크에서 이미 조회한 positions 재활용)
        sector_map = self._get_sector_map_cached()
        div_check = self.risk_manager.check_diversification(
            len(positions),
            sizing_entry_price * quantity,
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
        estimated_fill_price = float(costs.get("execution_price", price) or price)

        # 음수 캐시 방지: 가용 현금이 0 이하이면 즉시 거부
        if available_cash <= 0:
            logger.warning(
                "종목 {} 매수 불가: 가용 현금 {:,.0f}원 (0 이하)",
                symbol, available_cash,
            )
            return {"success": False, "reason": "가용 현금 부족 (0 이하)"}

        total_required = (estimated_fill_price * quantity) + float(costs.get("commission", 0) or 0)
        if total_required > available_cash:
            logger.warning(
                "종목 {} 매수 불가: 필요 현금 {:,.0f}원 > 사용 가능 현금 {:,.0f}원",
                symbol, total_required, available_cash,
            )
            return {"success": False, "reason": "사용 가능 현금 부족"}

        # 주문 전 안전 체크
        pre_check = self._pre_order_check(
            symbol=symbol,
            action="BUY",
            strategy=strategy,
            candidate_notional=estimated_fill_price * quantity,
            price=price,
            avg_daily_volume=avg_daily_volume,
        )
        if not pre_check["allowed"]:
            result = {"success": False, "reason": pre_check["reason"]}
            if pre_check.get("paper_entry_blocked"):
                result["paper_entry_blocked"] = True
            return result

        # ── 상태기계 기반 주문 처리 ──
        expected_price = float(price)

        # 1) OrderRecord 생성 (NEW)
        order = self.order_book.create_order(
            symbol=symbol, action="BUY", requested_qty=quantity,
            requested_price=expected_price, strategy=strategy,
            account_key=self.account_key, mode=self.mode,
        )

        # 2) 중복 주문 확인 (OrderBook 기반 + 기존 guard)
        existing_open = [o for o in self.order_book.get_open_orders(symbol)
                         if o.order_id != order.order_id]
        if existing_open:
            order.transition(OrderStatus.REJECTED, reason="중복 주문: 미완료 주문 존재")
            self._persist_order_record(order)
            result = {"success": False, "reason": f"{symbol} 미완료 주문 존재"}
            if self.mode == "live":
                result.update({"requires_reconcile": True, "persistent_order_block": True})
            return result

        # 3) Submit → broker/simulated event → FILLED
        fill_price = expected_price
        actual_slippage_pct = None

        if self.mode == "live":
            ttl_seconds = int(self.config.trading.get("pending_order_ttl_seconds", 600))
            persistent_block = self._persistent_live_order_block(symbol, order)
            if persistent_block:
                return persistent_block
            if OrderGuard.has_pending(symbol):
                order.transition(OrderStatus.REJECTED, reason="OrderGuard pending")
                self._persist_order_record(order)
                return {"success": False, "reason": f"{symbol} 종목에 미체결/최근 주문이 남아 있어 중복 주문을 차단했습니다."}
            live_unfilled_block = self._live_unfilled_order_block(symbol, order)
            if live_unfilled_block:
                self._persist_order_record(order)
                return live_unfilled_block

            OrderGuard.mark_pending(symbol, ttl_seconds=ttl_seconds)
            order.transition(OrderStatus.SUBMITTED)
            self._persist_order_record(order)

            order_result = self._execute_with_retry(
                self.kis_api.buy_order, symbol, quantity, int(price),
                symbol=symbol, action="BUY", price=price, quantity=quantity,
                strategy=strategy, signal_score=signal_score, reason=reason,
            )
            if order_result is None:
                order.transition(OrderStatus.REJECTED, reason="KIS API 3회 재시도 실패")
                OrderGuard.clear(symbol)
                self._persist_order_record(order)
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후, dead-letter 저장됨)"}

            order.transition(OrderStatus.ACKED, broker_order_id=str(order_result.get("odno", "")))
            self._persist_order_record(order)
            execution = self._resolve_live_execution(
                symbol, expected_price, order_result if isinstance(order_result, dict) else None,
                requested_qty=quantity,
            )
            if not execution["confirmed"]:
                self._mark_partial_live_execution(order, execution)
                return self._pending_live_execution_result(
                    order=order,
                    action="BUY",
                    execution=execution,
                )
            fill_price = float(execution["fill_price"])
            actual_slippage_pct = execution["actual_slippage_pct"]
            self._report_execution_slippage(symbol, "BUY", expected_price, fill_price, actual_slippage_pct)
            # FILLED 전이 — 이 시점에서만 position/trade 반영
            order.transition(OrderStatus.FILLED, fill_qty=quantity, fill_price=fill_price)
            self._persist_order_record(order)
            OrderGuard.clear(symbol)
        else:
            # Paper mode: simulated broker event
            order.transition(OrderStatus.SUBMITTED)
            order.transition(OrderStatus.ACKED)
            # Paper에서는 슬리피지 적용 후 즉시 FILLED (simulated fill)
            costs = self.risk_manager.calculate_transaction_costs(
                expected_price, quantity, "BUY", avg_daily_volume=avg_daily_volume,
            )
            fill_price = costs["execution_price"]
            order.transition(OrderStatus.FILLED, fill_qty=quantity, fill_price=fill_price)

        # 4) FILLED 상태에서만 DB 반영 (invariant: fill 전 position 없음)
        assert order.status == OrderStatus.FILLED, f"DB 반영 시점에 FILLED가 아님: {order.status}"

        if self.mode == "live":
            costs = self.risk_manager.calculate_transaction_costs(
                fill_price, quantity, "BUY", avg_daily_volume=avg_daily_volume,
            )

        stop_loss = self.risk_manager.calculate_stop_loss(
            fill_price,
            atr,
            regime_multiplier=regime_sl_mult,
        )
        tp_info = self.risk_manager.calculate_take_profit(
            fill_price,
            regime_multiplier=regime_tp_mult,
        )
        trailing_stop = self.risk_manager.calculate_trailing_stop(fill_price, atr)

        _order_at = datetime.now()
        save_trade(
            symbol=symbol, action="BUY", price=fill_price, quantity=quantity,
            commission=costs["commission"], tax=0, slippage=costs["slippage"],
            strategy=strategy, signal_score=signal_score, reason=reason,
            mode=self.mode, account_key=self.account_key,
            signal_at=signal_at or _order_at, order_at=_order_at,
            expected_price=expected_price,
            actual_slippage_pct=actual_slippage_pct if self.mode == "live" else None,
            execution_session_id=execution_session_id,
            order_id=order.order_id,
        )
        _log_op_event("SIGNAL", f"BUY {symbol} {quantity}주 @ {price:,.0f}원",
                       symbol=symbol, strategy=strategy, mode=self.mode)

        save_position(
            symbol=symbol, avg_price=fill_price, quantity=quantity,
            stop_loss_price=stop_loss, take_profit_price=tp_info["target_final"],
            trailing_stop_price=trailing_stop, strategy=strategy,
            account_key=self.account_key,
        )

        # 매매 로그
        log_trade("BUY", symbol, fill_price, quantity, reason)

        result = {
            "success": True,
            "symbol": symbol,
            "action": "BUY",
            "price": fill_price,
            "quantity": quantity,
            "total_amount": fill_price * quantity,
            "stop_loss": stop_loss,
            "take_profit": tp_info["target_final"],
            "trailing_stop": trailing_stop,
            "costs": costs,
            "mode": self.mode,
            "execution_session_id": execution_session_id,
            "order_id": order.order_id,
        }

        logger.info(
            "✅ 매수 완료: {} {}주 @ {:,.0f}원 | 손절={:,.0f} | 익절={:,.0f}",
            symbol, quantity, fill_price, stop_loss, tp_info["target_final"],
        )

        return result

    def execute_buy_quantity(
        self,
        symbol: str,
        price: float,
        quantity: int,
        capital: float,
        available_cash: float,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
        atr: float = None,
        execution_session_id: str = "",
    ) -> dict:
        """Execute a fixed-quantity paper buy.

        Portfolio target-weight adapters already decide quantities at the book
        level, so the normal risk-ratio position sizer must not override them.
        This path is deliberately paper-only.
        """
        with PositionLock():
            return self._execute_buy_quantity_impl(
                symbol=symbol,
                price=price,
                quantity=quantity,
                capital=capital,
                available_cash=available_cash,
                signal_score=signal_score,
                reason=reason,
                strategy=strategy,
                avg_daily_volume=avg_daily_volume,
                atr=atr,
                execution_session_id=execution_session_id,
            )

    def _execute_buy_quantity_impl(
        self,
        symbol: str,
        price: float,
        quantity: int,
        capital: float,
        available_cash: float,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
        atr: float = None,
        execution_session_id: str = "",
    ) -> dict:
        if self.mode == "live":
            return {"success": False, "reason": "fixed-quantity buy is paper-only"}

        quantity = int(quantity or 0)
        if quantity <= 0:
            return {"success": False, "reason": "quantity must be positive"}
        order_price = self._positive_order_price(price)
        if order_price is None:
            return {
                "success": False,
                "reason": f"매수 가격 확인 실패: {symbol} 현재가 없음",
                "price_invalid": True,
            }
        price = order_price

        if self._should_block_new_buy_volatility_window():
            return {"success": False, "reason": "장 초반/마감 진입 차단 시간대"}

        costs = self.risk_manager.calculate_transaction_costs(
            price, quantity, "BUY", avg_daily_volume=avg_daily_volume,
        )
        expected_price = float(price)
        fill_price = float(costs["execution_price"])
        total_required = fill_price * quantity + float(costs.get("commission", 0) or 0)
        if total_required > float(available_cash):
            return {
                "success": False,
                "reason": "사용 가능 현금 부족",
                "required": total_required,
                "available_cash": available_cash,
            }

        pre_check = self._pre_order_check(
            symbol=symbol,
            action="BUY",
            strategy=strategy,
            candidate_notional=fill_price * quantity,
            price=price,
            avg_daily_volume=avg_daily_volume,
        )
        if not pre_check["allowed"]:
            result = {"success": False, "reason": pre_check["reason"]}
            if pre_check.get("paper_entry_blocked"):
                result["paper_entry_blocked"] = True
            return result

        stop_loss = self.risk_manager.calculate_stop_loss(price, atr)
        tp_info = self.risk_manager.calculate_take_profit(price)
        trailing_stop = self.risk_manager.calculate_trailing_stop(price, atr)

        order = self.order_book.create_order(
            symbol=symbol,
            action="BUY",
            requested_qty=quantity,
            requested_price=expected_price,
            strategy=strategy,
            account_key=self.account_key,
            mode=self.mode,
        )
        existing_open = [
            o for o in self.order_book.get_open_orders(symbol)
            if o.order_id != order.order_id
        ]
        if existing_open:
            order.transition(OrderStatus.REJECTED, reason="중복 주문: 미완료 주문 존재")
            self._persist_order_record(order)
            result = {"success": False, "reason": f"{symbol} 미완료 주문 존재"}
            if self.mode == "live":
                result.update({"requires_reconcile": True, "persistent_order_block": True})
            return result

        order.transition(OrderStatus.SUBMITTED)
        order.transition(OrderStatus.ACKED)
        order.transition(OrderStatus.FILLED, fill_qty=quantity, fill_price=fill_price)
        assert order.status == OrderStatus.FILLED, f"DB 반영 시점에 FILLED가 아님: {order.status}"

        stop_loss = self.risk_manager.calculate_stop_loss(fill_price, atr)
        tp_info = self.risk_manager.calculate_take_profit(fill_price)
        trailing_stop = self.risk_manager.calculate_trailing_stop(fill_price, atr)

        _order_at = datetime.now()
        save_trade(
            symbol=symbol,
            action="BUY",
            price=fill_price,
            quantity=quantity,
            commission=costs["commission"],
            tax=0,
            slippage=costs["slippage"],
            strategy=strategy,
            signal_score=signal_score,
            reason=reason,
            mode=self.mode,
            account_key=self.account_key,
            signal_at=_order_at,
            order_at=_order_at,
            expected_price=expected_price,
            actual_slippage_pct=None,
            execution_session_id=execution_session_id,
            order_id=order.order_id,
        )
        _log_op_event(
            "SIGNAL",
            f"BUY {symbol} {quantity}주 @ {price:,.0f}원",
            symbol=symbol,
            strategy=strategy,
            mode=self.mode,
        )
        save_position(
            symbol=symbol,
            avg_price=fill_price,
            quantity=quantity,
            stop_loss_price=stop_loss,
            take_profit_price=tp_info["target_final"],
            trailing_stop_price=trailing_stop,
            strategy=strategy,
            account_key=self.account_key,
        )
        log_trade("BUY", symbol, fill_price, quantity, reason)

        result = {
            "success": True,
            "symbol": symbol,
            "action": "BUY",
            "price": fill_price,
            "quantity": quantity,
            "total_amount": fill_price * quantity,
            "stop_loss": stop_loss,
            "take_profit": tp_info["target_final"],
            "trailing_stop": trailing_stop,
            "costs": costs,
            "mode": self.mode,
            "paper_fixed_quantity": True,
            "execution_session_id": execution_session_id,
            "order_id": order.order_id,
        }
        logger.info(
            "✅ 고정수량 paper 매수 완료: {} {}주 @ {:,.0f}원",
            symbol, quantity, fill_price,
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
        execution_session_id: str = "",
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
            return self._execute_sell_impl(symbol, price, quantity, signal_score, reason, strategy, avg_daily_volume, execution_session_id)

    def _execute_sell_impl(
        self,
        symbol: str,
        price: float,
        quantity: int = None,
        signal_score: float = 0,
        reason: str = "",
        strategy: str = "",
        avg_daily_volume: float = None,
        execution_session_id: str = "",
    ) -> dict:
        """매도 주문 실제 로직 (Lock 내부에서 호출)."""
        position = get_position(symbol, account_key=self.account_key)
        if not position:
            logger.warning("종목 {} 보유 포지션 없음 — 매도 스킵", symbol)
            return {"success": False, "reason": "보유 포지션 없음"}

        order_price = self._positive_order_price(price)
        if order_price is None:
            reason_text = f"매도 가격 확인 실패: {symbol} 현재가 없음"
            logger.warning("종목 {} 매도 스킵: {}", symbol, reason_text)
            return {"success": False, "reason": reason_text, "price_invalid": True}
        price = order_price

        sell_qty = position.quantity if quantity is None else int(quantity)
        if sell_qty <= 0:
            logger.warning("종목 {} 매도 수량 오류: {}", symbol, sell_qty)
            return {"success": False, "reason": "매도 수량은 1주 이상이어야 합니다"}
        if sell_qty > position.quantity:
            logger.warning(
                "종목 {} 매도 수량 초과: 요청 {}주 > 보유 {}주",
                symbol,
                sell_qty,
                position.quantity,
            )
            return {
                "success": False,
                "reason": "보유 수량 초과 매도 요청",
                "requested_quantity": sell_qty,
                "available_quantity": position.quantity,
            }

        # 최소 보유 기간 검사 (손실 방어용 긴급 청산은 예외)
        is_emergency = self._is_emergency_sell_reason(reason)
        if not is_emergency:
            min_hold = self._get_min_holding_days()
            if min_hold > 0 and getattr(position, "bought_at", None):
                bought_date = position.bought_at.date() if hasattr(position.bought_at, "date") else position.bought_at
                holding_days = (datetime.now().date() - bought_date).days
                if holding_days < min_hold:
                    msg = f"최소 보유 기간 미달 ({holding_days}/{min_hold}일)"
                    logger.info("종목 {} 매도 스킵: {}", symbol, msg)
                    return {"success": False, "reason": msg}

        expected_price = float(price)
        fill_price = expected_price
        actual_slippage_pct = None
        costs = None

        # 주문 전 안전 체크 (매도: 쿨다운 중에도 허용)
        pre_check = self._pre_order_check(symbol=symbol, action="SELL", strategy=strategy)
        if not pre_check["allowed"]:
            return {"success": False, "reason": pre_check["reason"]}

        # ── 상태기계 기반 매도 처리 ──
        order = self.order_book.create_order(
            symbol=symbol, action="SELL", requested_qty=sell_qty,
            requested_price=expected_price, strategy=strategy,
            account_key=self.account_key, mode=self.mode,
        )

        if self.mode == "live":
            ttl_seconds = int(self.config.trading.get("pending_order_ttl_seconds", 600))
            persistent_block = self._persistent_live_order_block(symbol, order)
            if persistent_block:
                return persistent_block
            if OrderGuard.has_pending(symbol):
                order.transition(OrderStatus.REJECTED, reason="OrderGuard pending")
                self._persist_order_record(order)
                return {"success": False, "reason": f"{symbol} 종목에 미체결/최근 주문이 남아 있어 중복 주문을 차단했습니다."}
            live_unfilled_block = self._live_unfilled_order_block(symbol, order)
            if live_unfilled_block:
                self._persist_order_record(order)
                return live_unfilled_block

            OrderGuard.mark_pending(symbol, ttl_seconds=ttl_seconds)
            order.transition(OrderStatus.SUBMITTED)
            self._persist_order_record(order)

            order_result = self._execute_with_retry(
                self.kis_api.sell_order, symbol, sell_qty, int(price),
                symbol=symbol, action="SELL", price=price, quantity=sell_qty,
                strategy=strategy, signal_score=signal_score, reason=reason,
            )
            if order_result is None:
                order.transition(OrderStatus.REJECTED, reason="KIS API 3회 재시도 실패")
                OrderGuard.clear(symbol)
                self._persist_order_record(order)
                return {"success": False, "reason": "KIS API 주문 실패 (3회 재시도 후, dead-letter 저장됨)"}

            order.transition(OrderStatus.ACKED, broker_order_id=str(order_result.get("odno", "")))
            self._persist_order_record(order)
            execution = self._resolve_live_execution(
                symbol, expected_price, order_result if isinstance(order_result, dict) else None,
                requested_qty=sell_qty,
            )
            if not execution["confirmed"]:
                self._mark_partial_live_execution(order, execution)
                return self._pending_live_execution_result(
                    order=order,
                    action="SELL",
                    execution=execution,
                )
            fill_price = float(execution["fill_price"])
            actual_slippage_pct = execution["actual_slippage_pct"]
            self._report_execution_slippage(symbol, "SELL", expected_price, fill_price, actual_slippage_pct)
            order.transition(OrderStatus.FILLED, fill_qty=sell_qty, fill_price=fill_price)
            self._persist_order_record(order)
            OrderGuard.clear(symbol)
        else:
            # Paper mode: simulated broker event
            costs = self.risk_manager.calculate_transaction_costs(
                expected_price,
                sell_qty,
                "SELL",
                avg_daily_volume=avg_daily_volume,
                avg_price=float(position.avg_price),
            )
            fill_price = float(costs["execution_price"])
            order.transition(OrderStatus.SUBMITTED)
            order.transition(OrderStatus.ACKED)
            order.transition(OrderStatus.FILLED, fill_qty=sell_qty, fill_price=fill_price)

        # FILLED 상태에서만 DB 반영 (invariant)
        assert order.status == OrderStatus.FILLED, f"SELL DB 반영 시점에 FILLED가 아님: {order.status}"

        if self.mode == "live":
            costs = self.risk_manager.calculate_transaction_costs(
                fill_price,
                sell_qty,
                "SELL",
                avg_daily_volume=avg_daily_volume,
                avg_price=float(position.avg_price),
            )
        total_tax = costs["tax"] + costs.get("capital_gains_tax", 0)
        pnl = (fill_price - position.avg_price) * sell_qty - costs["commission"] - total_tax
        pnl_rate = ((fill_price / position.avg_price) - 1) * 100

        save_trade(
            symbol=symbol, action="SELL", price=fill_price, quantity=sell_qty,
            commission=costs["commission"], tax=total_tax, slippage=costs["slippage"],
            strategy=strategy, signal_score=signal_score,
            reason=f"{reason} | PnL: {pnl:,.0f}원 ({pnl_rate:.2f}%)",
            mode=self.mode, account_key=self.account_key,
            expected_price=expected_price,
            actual_slippage_pct=actual_slippage_pct if self.mode == "live" else None,
            execution_session_id=execution_session_id,
            order_id=order.order_id,
        )

        if sell_qty >= position.quantity:
            delete_position(symbol, account_key=self.account_key)
        else:
            remaining_pos = reduce_position(symbol, sell_qty, account_key=self.account_key)
            if remaining_pos and reason == "TAKE_PROFIT_PARTIAL":
                from database.repositories import update_position_targets
                tp_config = self.risk_manager.risk_params.get("take_profit", {})
                final_target = position.avg_price * (1 + tp_config.get("fixed_rate", 0.08))
                update_position_targets(
                    symbol, take_profit_price=round(final_target, 0),
                    account_key=self.account_key,
                )

        # 매매 로그
        log_trade("SELL", symbol, fill_price, sell_qty, f"{reason} (수익: {pnl_rate:.2f}%)")

        result = {
            "success": True,
            "symbol": symbol,
            "action": "SELL",
            "price": fill_price,
            "quantity": sell_qty,
            "total_amount": fill_price * sell_qty,
            "pnl": round(pnl, 0),
            "pnl_rate": round(pnl_rate, 2),
            "costs": costs,
            "mode": self.mode,
            "execution_session_id": execution_session_id,
            "order_id": order.order_id,
        }

        emoji = "📈" if pnl >= 0 else "📉"
        logger.info(
            "{} 매도 완료: {} {}주 @ {:,.0f}원 | 수익: {:,.0f}원 ({:.2f}%)",
            emoji, symbol, sell_qty, fill_price, pnl, pnl_rate,
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

        checked_price = self._positive_order_price(current_price)
        if checked_price is None:
            reason_text = f"현재가 확인 실패: {symbol} 손절/익절 판단 보류"
            logger.warning(reason_text)
            return {"action": None, "price_invalid": True, "reason": reason_text}
        current_price = checked_price

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
    # 실전 체결가·슬리피지
    # =============================================================

    def _live_unfilled_order_block(self, symbol: str, order) -> dict | None:
        if not self.kis_api:
            order.transition(OrderStatus.REJECTED, reason="KIS API 미설정")
            return {
                "success": False,
                "reason": "KIS API가 준비되지 않아 실전 주문 전 미체결 확인을 할 수 없습니다.",
                "symbol": symbol,
                "mode": self.mode,
                "live_unfilled_check": {
                    "checked": False,
                    "reason": "kis_api_missing",
                    "orders": [],
                },
            }
        try:
            status_getter = getattr(self.kis_api, "get_unfilled_order_status", None)
            if callable(status_getter):
                status = status_getter(symbol)
                if not status.get("checked"):
                    order.transition(OrderStatus.REJECTED, reason="KIS 미체결 조회 실패")
                    return {
                        "success": False,
                        "reason": "실전 주문 전 KIS 미체결 조회가 실패해 중복 주문 방지를 위해 주문을 보류했습니다.",
                        "symbol": symbol,
                        "mode": self.mode,
                        "live_unfilled_check": status,
                    }
                if status.get("has_unfilled"):
                    order.transition(OrderStatus.REJECTED, reason="KIS 미체결 존재")
                    return {
                        "success": False,
                        "reason": "해당 종목 미체결 주문이 있어 중복 주문을 보류했습니다.",
                        "symbol": symbol,
                        "mode": self.mode,
                        "live_unfilled_check": status,
                    }
                return None
            if self.kis_api.has_unfilled_orders(symbol):
                order.transition(OrderStatus.REJECTED, reason="KIS 미체결 존재")
                return {
                    "success": False,
                    "reason": "해당 종목 미체결 주문이 있어 중복 주문을 보류했습니다.",
                    "symbol": symbol,
                    "mode": self.mode,
                    "live_unfilled_check": {
                        "checked": True,
                        "has_unfilled": True,
                        "reason": "legacy_bool_check",
                        "orders": [],
                    },
                }
        except Exception as exc:
            logger.warning("실전 주문 전 미체결 조회 예외 — 주문 보류: {} — {}", symbol, exc)
            order.transition(OrderStatus.REJECTED, reason="KIS 미체결 조회 예외")
            return {
                "success": False,
                "reason": "실전 주문 전 KIS 미체결 조회 중 예외가 발생해 주문을 보류했습니다.",
                "symbol": symbol,
                "mode": self.mode,
                "live_unfilled_check": {
                    "checked": False,
                    "reason": "kis_unfilled_query_exception",
                    "error": str(exc),
                    "orders": [],
                },
            }
        return None

    def _resolve_live_execution(
        self,
        symbol: str,
        expected_price: float,
        order_output: dict | None,
        requested_qty: int | None = None,
    ) -> dict:
        """
        체결가 조회 후 confirmed/fill_price/actual_slippage_pct를 반환한다.
        조회 실패 시 live 장부 반영을 보류하기 위해 confirmed=False로 둔다.
        """
        if not order_output:
            return {
                "confirmed": False,
                "fill_price": None,
                "actual_slippage_pct": None,
                "reason": "live_order_output_missing",
            }
        try:
            detail_getter = getattr(self.kis_api, "get_order_execution_after_order", None)
            if callable(detail_getter):
                execution = detail_getter(symbol, order_output)
                expected_order_no = self._normalize_broker_order_id(
                    self._broker_order_id_from_order_output(order_output)
                )
                execution_order_no = self._normalize_broker_order_id(
                    (execution or {}).get("order_no")
                )
                if expected_order_no and execution_order_no and execution_order_no != expected_order_no:
                    logger.warning(
                        "실전 체결 조회 주문번호 불일치 — DB 반영 보류: {} expected={} actual={}",
                        symbol,
                        expected_order_no,
                        execution_order_no,
                    )
                    return {
                        "confirmed": False,
                        "fill_price": None,
                        "actual_slippage_pct": None,
                        "reason": "live_execution_order_mismatch",
                        "expected_order_no": expected_order_no,
                        "execution_order_no": execution_order_no,
                    }
                fill = (execution or {}).get("fill_price")
                filled_qty = (execution or {}).get("filled_qty")
                remaining_qty = (execution or {}).get("remaining_qty")
                qty_contract_checked = True
            else:
                fill = self.kis_api.get_filled_avg_price_after_order(symbol, order_output)
                filled_qty = None
                remaining_qty = None
                qty_contract_checked = False
        except Exception as exc:
            logger.warning("실전 체결가 조회 예외 — DB 반영 보류: {} — {}", symbol, exc)
            return {
                "confirmed": False,
                "fill_price": None,
                "actual_slippage_pct": None,
                "reason": "live_fill_lookup_failed",
                "error": str(exc),
            }
        try:
            fill = float(fill) if fill is not None else None
        except (TypeError, ValueError):
            fill = None
        if fill is None or fill <= 0:
            logger.warning(
                "실전 체결가 조회 실패 — DB 반영 보류: {}", symbol,
            )
            return {
                "confirmed": False,
                "fill_price": None,
                "actual_slippage_pct": None,
                "reason": "live_fill_unconfirmed",
            }
        try:
            filled_qty = int(float(filled_qty)) if filled_qty is not None else None
        except (TypeError, ValueError):
            filled_qty = None
        try:
            remaining_qty = int(float(remaining_qty)) if remaining_qty is not None else None
        except (TypeError, ValueError):
            remaining_qty = None
        if expected_price <= 0:
            actual_slippage_pct = None
        else:
            actual_slippage_pct = (fill - expected_price) / expected_price * 100.0
        if requested_qty and qty_contract_checked and filled_qty is None:
            logger.warning(
                "실전 체결 수량 확인 실패 — DB 반영 보류: {} requested_qty={}",
                symbol,
                requested_qty,
            )
            return {
                "confirmed": False,
                "fill_price": fill,
                "actual_slippage_pct": actual_slippage_pct,
                "reason": "live_filled_qty_unconfirmed",
                "filled_qty": None,
                "requested_qty": requested_qty,
                "remaining_qty": remaining_qty,
            }
        if requested_qty and filled_qty is not None and filled_qty <= 0:
            logger.warning(
                "실전 체결 수량 0 확인 — DB 반영 보류: {} requested_qty={}",
                symbol,
                requested_qty,
            )
            return {
                "confirmed": False,
                "fill_price": fill,
                "actual_slippage_pct": actual_slippage_pct,
                "reason": "live_fill_unconfirmed",
                "filled_qty": filled_qty,
                "requested_qty": requested_qty,
                "remaining_qty": remaining_qty,
            }
        if requested_qty and filled_qty is not None and filled_qty < requested_qty:
            logger.warning(
                "실전 부분체결 확인 — 장부 반영 보류: {} filled_qty={} requested_qty={}",
                symbol,
                filled_qty,
                requested_qty,
            )
            return {
                "confirmed": False,
                "fill_price": fill,
                "actual_slippage_pct": actual_slippage_pct,
                "reason": "live_partial_fill_unreconciled",
                "filled_qty": filled_qty,
                "requested_qty": requested_qty,
                "remaining_qty": remaining_qty,
            }
        return {
            "confirmed": True,
            "fill_price": fill,
            "actual_slippage_pct": actual_slippage_pct,
            "reason": "",
            "filled_qty": filled_qty,
            "requested_qty": requested_qty,
            "remaining_qty": remaining_qty,
        }

    def _mark_partial_live_execution(self, order, execution: dict) -> None:
        try:
            filled_qty = int(execution.get("filled_qty") or 0)
            fill_price = float(execution.get("fill_price") or 0)
        except (TypeError, ValueError):
            return
        if 0 < filled_qty < order.requested_qty and fill_price > 0:
            order.transition(
                OrderStatus.PARTIAL_FILLED,
                fill_qty=filled_qty,
                fill_price=fill_price,
            )

    def _pending_live_execution_result(
        self,
        *,
        order,
        action: str,
        execution: dict,
    ) -> dict:
        self._persist_order_record(order)
        logger.warning(
            "실전 주문 체결 미확인 — 장부 반영 보류: {} {} order_id={} broker_order_id={} reason={}",
            action,
            order.symbol,
            order.order_id,
            order.broker_order_id,
            execution.get("reason", "unknown"),
        )
        return {
            "success": False,
            "reason": "실전 주문은 접수됐지만 체결 확인 전이라 DB 반영을 보류했습니다.",
            "symbol": order.symbol,
            "action": action,
            "mode": self.mode,
            "order_pending": True,
            "requires_reconcile": True,
            "order_id": order.order_id,
            "broker_order_id": order.broker_order_id,
            "order_status": order.status.value,
            "execution_check": execution,
        }

    @staticmethod
    def _broker_order_id_from_order_output(order_output) -> str:
        if not order_output or not isinstance(order_output, dict):
            return ""
        for key in ("ODNO", "odno", "ORD_NO", "ord_no"):
            value = order_output.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _normalize_broker_order_id(value) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return raw.lstrip("0") or raw

    def _broker_open_order_keys(self, open_orders: list[dict]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for item in open_orders or []:
            symbol = str(item.get("symbol") or "").strip()
            order_no = self._normalize_broker_order_id(item.get("order_no"))
            if symbol and order_no:
                keys.add((symbol, order_no))
        return keys

    def _lookup_persistent_order_execution(self, record: dict) -> dict | None:
        detail_getter = getattr(self.kis_api, "get_order_execution_after_order", None)
        broker_order_id = record.get("broker_order_id")
        if not callable(detail_getter) or not broker_order_id:
            return None
        try:
            return detail_getter(
                record.get("symbol", ""),
                {"odno": broker_order_id},
                max_attempts=1,
                delay_seconds=0,
            )
        except TypeError:
            return detail_getter(record.get("symbol", ""), {"odno": broker_order_id})
        except Exception as exc:
            logger.warning(
                "[복구] DB 미완료 주문 체결 조회 실패: {} {} — {}",
                record.get("symbol"),
                broker_order_id,
                exc,
            )
            return None

    def _reconcile_persistent_open_order_records(self, open_orders: list[dict]) -> list[dict]:
        """KIS 미체결 목록에서 사라진 DB open order record를 대조 완료로 닫는다."""
        if self.mode != "live":
            return []
        try:
            records = get_open_order_records(account_key=self.account_key, mode=self.mode)
        except Exception as exc:
            logger.warning("[복구] DB 미완료 주문 조회 실패 — 대조 완료 처리 생략: {}", exc)
            return []

        broker_open_keys = self._broker_open_order_keys(open_orders)
        reconciled: list[dict] = []
        for record in records:
            broker_order_id = self._normalize_broker_order_id(record.get("broker_order_id"))
            if not broker_order_id:
                continue
            symbol = str(record.get("symbol") or "").strip()
            if (symbol, broker_order_id) in broker_open_keys:
                continue

            execution = self._lookup_persistent_order_execution(record)
            if not execution:
                logger.warning(
                    "[복구] DB 미완료 주문 체결 상태 불명확 — 열린 상태 유지: {} {}",
                    symbol,
                    record.get("broker_order_id"),
                )
                continue
            filled_qty = execution.get("filled_qty")
            filled_price = execution.get("fill_price")
            remaining_qty = execution.get("remaining_qty")

            try:
                resolved_filled_qty = int(float(filled_qty)) if filled_qty is not None else int(record.get("filled_qty") or 0)
            except (TypeError, ValueError):
                resolved_filled_qty = int(record.get("filled_qty") or 0)
            try:
                resolved_filled_price = (
                    float(filled_price)
                    if filled_price is not None
                    else float(record.get("filled_price") or 0)
                )
            except (TypeError, ValueError):
                resolved_filled_price = float(record.get("filled_price") or 0)
            try:
                resolved_remaining_qty = int(float(remaining_qty)) if remaining_qty is not None else 0
            except (TypeError, ValueError):
                resolved_remaining_qty = 0

            updated = reconcile_order_record(
                record["order_id"],
                status=OrderStatus.RECONCILED.value,
                filled_qty=resolved_filled_qty,
                filled_price=resolved_filled_price,
                remaining_qty=max(resolved_remaining_qty, 0),
                reason="broker_execution_confirmed_after_recovery_check",
            )
            if updated:
                OrderGuard.clear(symbol)
                reconciled.append({
                    **updated,
                    "execution_checked": bool(execution),
                })
                logger.info(
                    "[복구] DB 미완료 주문 대조 완료: {} {} status={} filled={}/{}",
                    symbol,
                    record.get("broker_order_id"),
                    updated["status"],
                    updated["filled_qty"],
                    record.get("requested_qty"),
                )
        return reconciled

    def _report_execution_slippage(
        self,
        symbol: str,
        action: str,
        expected_price: float,
        fill_price: float,
        actual_slippage_pct: float | None,
    ) -> None:
        if actual_slippage_pct is None:
            return
        a = abs(actual_slippage_pct)
        if a > self.SLIPPAGE_DISCORD_PCT:
            logger.warning(
                "슬리피지 {:.3f}% (1% 초과) — {} {} | 예상 {:,.0f}원 → 체결 {:,.0f}원",
                actual_slippage_pct, symbol, action, expected_price, fill_price,
            )
            try:
                from core.notifier import Notifier
                Notifier().send_message(
                    "[슬리피지 경고] "
                    f"{symbol} {action} | 실제 슬리피지 {actual_slippage_pct:+.3f}% "
                    f"(예상 {expected_price:,.0f}원 → 체결 {fill_price:,.0f}원)",
                )
            except Exception as e:
                logger.debug("슬리피지 디스코드 알림 실패: {}", e)
        elif a > self.SLIPPAGE_WARN_PCT:
            logger.warning(
                "슬리피지 {:.3f}% (백테스트 가정 0.05%의 3배 초과) — {} {} | "
                "예상 {:,.0f}원 → 체결 {:,.0f}원",
                actual_slippage_pct, symbol, action, expected_price, fill_price,
            )

    # =============================================================
    # 안전 체크 및 재시도
    # =============================================================

    def _pre_order_check(
        self,
        symbol: str = "",
        action: str = "BUY",
        strategy: str = "",
        candidate_notional: float | None = None,
        price: float | None = None,
        avg_daily_volume: float | None = None,
    ) -> dict:
        """
        주문 전 안전 체크 (거래 시간 + 블랙스완)

        Args:
            action: "BUY" 또는 "SELL". 쿨다운 중에도 매도는 허용.

        Returns:
            {"allowed": True/False, "reason": 사유}
        """
        monthly_cap = self._monthly_buy_cap_check(symbol, action)
        if not monthly_cap["allowed"]:
            return monthly_cap

        drawdown_check = self._drawdown_pre_order_check(action)
        if not drawdown_check["allowed"]:
            return drawdown_check

        # 페이퍼/스케줄 신규 진입은 runtime/preflight 확인에 실패하면 차단한다.
        if self.mode != "live":
            paper_check = self._paper_entry_pre_order_check(
                action=action,
                strategy=strategy,
                candidate_notional=candidate_notional,
            )
            if not paper_check["allowed"]:
                return paper_check
            liquidity_check = self._entry_liquidity_check(
                symbol=symbol,
                price=price if price is not None else 0,
                avg_daily_volume=avg_daily_volume,
                action=action,
            )
            if not liquidity_check["allowed"]:
                return liquidity_check
            return paper_check

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

        liquidity_check = self._entry_liquidity_check(
            symbol=symbol,
            price=price if price is not None else 0,
            avg_daily_volume=avg_daily_volume,
            action=action,
        )
        if not liquidity_check["allowed"]:
            return liquidity_check

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
            self.last_open_order_reconcile_status = {
                "checked": True,
                "reason": "paper_mode_skipped",
                "orders": [],
            }
            return []
        try:
            if self.kis_api and not getattr(self.kis_api, "_access_token", None):
                self.kis_api.authenticate()
        except Exception as e:
            logger.warning("[복구] KIS 인증 실패 — 미체결 조회 생략: {}", e)
            self.last_open_order_reconcile_status = {
                "checked": False,
                "reason": "kis_auth_failed",
                "error": str(e),
                "orders": [],
            }
            return []
        try:
            status_getter = getattr(self.kis_api, "get_open_orders_status", None)
            if callable(status_getter):
                status = status_getter()
                self.last_open_order_reconcile_status = status
                if not status.get("checked"):
                    logger.warning("[복구] KIS 미체결 조회 실패 상태: {}", status.get("reason"))
                    return []
                open_orders = status.get("orders", [])
            else:
                open_orders = self.kis_api.get_open_orders()
                self.last_open_order_reconcile_status = {
                    "checked": True,
                    "reason": "legacy_list_check",
                    "orders": open_orders,
                }
            reconciled_records = self._reconcile_persistent_open_order_records(open_orders)
            self.last_open_order_reconcile_status["persistent_order_reconciliations"] = reconciled_records
        except Exception as e:
            logger.warning("[복구] get_open_orders 실패: {}", e)
            self.last_open_order_reconcile_status = {
                "checked": False,
                "reason": "kis_open_orders_query_exception",
                "error": str(e),
                "orders": [],
            }
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

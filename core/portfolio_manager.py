"""
포트폴리오 관리 모듈
- 보유 포지션 관리, 잔고/수익률 추적
- KIS 잔고와 DB 포지션 동기화 (sync_with_broker)
"""

from loguru import logger

from config.config_loader import Config
from core.position_lock import PositionLock
from database.repositories import (
    get_all_positions,
    get_trade_cash_summary,
    save_portfolio_snapshot,
    get_latest_peak_value,
    get_strategy_performance_summary,
    get_cash_flow_total,
    get_cash_flow_total_between,
    get_max_cumulative_return,
    get_latest_snapshot_summary,
    has_cash_flows,
)


def twr_period_return(v_prev: float, v_now: float, flow: float = 0.0) -> float:
    """구간 시간가중수익률(소수). 외부 현금 흐름(flow, +입금)은 구간 시작에 유입으로 간주.

    r = v_now / (v_prev + flow) - 1  — 입금은 수익이 아니므로 분모에 더해 중화한다.
    분모가 0 이하이면 판정 불가로 0을 반환한다(신규 계정 초기 상태 등).
    """
    base = float(v_prev) + float(flow)
    if base <= 0:
        return 0.0
    return float(v_now) / base - 1.0


class LiveBrokerBalanceUnavailable(RuntimeError):
    """live 주문 판단에 필요한 KIS 잔고를 확인하지 못한 상태."""


class PortfolioManager:
    """
    포트폴리오 관리자
    - account_key: 전략별 계좌 구분 (다중 계좌 시)
    """

    def __init__(self, config: Config = None, account_key: str = "", initial_capital: float = None):
        self.config = config or Config.get()
        self.account_key = account_key or ""
        # initial_capital 명시 시 전역(risk_params.position_sizing.initial_capital) 대신
        # 사용한다 — 계정 키별 paper 계정은 독립 자본 풀이므로, 바스켓처럼 자기 자본
        # 규모가 필요한 계정(예: 고가 종목 슬롯을 채우려면 전역 1,000만으로 부족)이
        # 전역값을 건드리지 않고 자본을 지정할 수 있어야 한다.
        if initial_capital is not None:
            self.initial_capital = float(initial_capital)
        else:
            self.initial_capital = self.config.risk_params.get(
                "position_sizing", {}
            ).get("initial_capital", 10000000)
        self._is_live = self.config.trading.get("mode", "paper") == "live"

        # Peak value 복구: DB 스냅샷에서 이전 세션의 peak을 가져와 MDD 연속성 유지
        restored_peak = get_latest_peak_value(account_key=self.account_key)
        if restored_peak is not None and restored_peak > self.initial_capital:
            self._peak_value = restored_peak
            logger.info("Peak value DB에서 복구: {:,.0f}원", restored_peak)
        else:
            self._peak_value = self.initial_capital

        logger.info(
            "PortfolioManager 초기화 (초기 자본: {:,.0f}원, peak: {:,.0f}원, 모드: {}, 계좌: {})",
            self.initial_capital,
            self._peak_value,
            "live" if self._is_live else "paper",
            self.account_key or "default",
        )

    def _build_position_state(self, current_prices: dict = None) -> dict:
        """보유 포지션과 평가손익 상태 계산."""
        positions = get_all_positions(account_key=self.account_key if self.account_key else None)
        current_prices = current_prices or {}

        invested = 0.0
        current_value = 0.0
        position_details = []

        for pos in positions:
            price = current_prices.get(pos.symbol, pos.avg_price)
            pos_value = price * pos.quantity
            pos_invested = pos.avg_price * pos.quantity
            pnl = pos_value - pos_invested
            pnl_rate = ((price / pos.avg_price) - 1) * 100 if pos.avg_price > 0 else 0

            invested += pos_invested
            current_value += pos_value

            position_details.append({
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "invested": pos_invested,
                "current_value": pos_value,
                "pnl": pnl,
                "pnl_rate": pnl_rate,
            })

        return {
            "positions": positions,
            "position_details": position_details,
            "invested": invested,
            "current_value": current_value,
            "position_count": len(positions),
        }

    def _get_db_financials(self, invested: float, current_value: float, mode: str) -> dict:
        """trade_history 기준 현금/실현손익/총 평가금 계산."""
        cash_summary = get_trade_cash_summary(
            mode=mode,
            account_key=self.account_key if self.account_key else None,
        )
        # 외부 현금 흐름(입금/출금)은 현금에 더하되 손익에서는 제외한다 —
        # 입금은 수익이 아니다(적립식 지원, docs/POCKET_TRACK_PLAN.md §4).
        deposits = get_cash_flow_total(account_key=self.account_key)
        cash = self.initial_capital + deposits + cash_summary["cash_delta"]
        total_value = cash + current_value
        realized_pnl = cash + invested - self.initial_capital - deposits
        unrealized_pnl = current_value - invested
        return {
            "cash": cash,
            "total_value": total_value,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
        }

    def get_portfolio_summary(self, current_prices: dict = None) -> dict:
        """
        포트폴리오 현황 요약

        Args:
            current_prices: {종목코드: 현재가} 딕셔너리

        Returns:
            포트폴리오 요약 딕셔너리
        """
        state = self._build_position_state(current_prices)
        invested = state["invested"]
        current_value = state["current_value"]
        position_details = state["position_details"]
        positions = state["positions"]

        cash = None
        total_value = None
        broker_balance_ok = None
        broker_balance_source = "db"
        broker_balance_error = ""

        if self._is_live:
            broker_balance_ok = False
            broker_balance_source = "db_fallback"
            try:
                from api.kis_api import KISApi
                account_no = self.config.get_account_no(self.account_key)
                balance = KISApi(account_no=account_no).get_balance()
                if balance and "total_value" in balance:
                    total_value = float(balance["total_value"])
                    cash = float(balance.get("cash", total_value - current_value))
                    broker_balance_ok = True
                    broker_balance_source = "kis"
                else:
                    broker_balance_error = "KIS 잔고 응답 없음"
                    logger.warning("KIS 잔고 조회 실패 — DB 기준으로 대체: {}", broker_balance_error)
            except Exception as e:
                broker_balance_error = str(e)
                logger.warning("KIS 잔고 조회 실패 — DB 기준으로 대체: {}", e)

        deposits_total = get_cash_flow_total(account_key=self.account_key)

        if cash is None or total_value is None:
            financials = self._get_db_financials(
                invested,
                current_value,
                "live" if self._is_live else "paper",
            )
            cash = financials["cash"]
            total_value = financials["total_value"]
            realized_pnl = financials["realized_pnl"]
            unrealized_pnl = financials["unrealized_pnl"]
        else:
            unrealized_pnl = current_value - invested
            realized_pnl = total_value - self.initial_capital - deposits_total - unrealized_pnl

        # 분기는 순합이 아니라 '흐름 존재 여부'로 — 순합 0(+100/-100)이어도 구간
        # 수익률은 이미 흐름의 영향을 받았으므로 TWR 경로를 유지해야 한다.
        account_has_flows = deposits_total != 0 or has_cash_flows(self.account_key)

        if not account_has_flows:
            # 무입금 계정: 기존 산식 그대로 (하위 호환 — 결과 불변)
            total_return = ((total_value / self.initial_capital) - 1) * 100 if self.initial_capital > 0 else 0

            if total_value > self._peak_value:
                self._peak_value = total_value
            mdd = ((self._peak_value - total_value) / self._peak_value) * 100 if self._peak_value > 0 else 0
        else:
            # 적립식 계정: 시간가중수익률(TWR) — 입금은 수익이 아니다.
            # 직전 스냅샷과 이번 측정 사이 유입(flow)을 분모에 더해 중화하고,
            # 누적은 직전 스냅샷의 누적수익률에 구간 수익률을 연결한다.
            from datetime import datetime as _dt
            prev = get_latest_snapshot_summary(account_key=self.account_key)
            if prev is None:
                # 첫 측정: 초기자본이 첫 유입, 그간의 입금 전액이 구간 유입
                r = twr_period_return(self.initial_capital, total_value, deposits_total)
                total_return = r * 100
            else:
                # 경계는 실제 측정 시각(created_at) — date(자정 귀속)를 쓰면 스냅샷
                # 이전의 같은 날 입금이 이중 산입된다.
                boundary = prev.get("created_at") or prev.get("date")
                flow_since = get_cash_flow_total_between(
                    self.account_key, boundary, _dt.now(),
                )
                r = twr_period_return(prev["total_value"], total_value, flow_since)
                total_return = ((1 + prev["cumulative_return"] / 100) * (1 + r) - 1) * 100

            # MDD도 TWR 지수 기준 — 원화 피크로 재면 입금이 낙폭을 가짜 회복시킨다.
            index_now = 1 + total_return / 100
            hist_max = get_max_cumulative_return(account_key=self.account_key)
            peak_index = max(1.0, index_now, 1 + (hist_max or 0.0) / 100)
            mdd = ((peak_index - index_now) / peak_index) * 100 if peak_index > 0 else 0
            # 원화 피크(peak_value 컬럼)는 스냅샷 연속성 위해 기존대로 계속 기록
            if total_value > self._peak_value:
                self._peak_value = total_value

        return {
            "total_value": round(total_value, 0),
            "cash": round(cash, 0),
            "invested": round(invested, 0),
            "current_value": round(current_value, 0),
            "total_return": round(total_return, 2),
            "mdd": round(mdd, 2),
            "position_count": len(positions),
            "realized_pnl": round(realized_pnl, 0),
            "unrealized_pnl": round(unrealized_pnl, 0),
            "deposits_total": round(deposits_total, 0),
            "principal": round(self.initial_capital + deposits_total, 0),
            "positions": position_details,
            "broker_balance_ok": broker_balance_ok,
            "broker_balance_source": broker_balance_source,
            "broker_balance_error": broker_balance_error,
        }

    def save_daily_snapshot(self, current_prices: dict = None, snapshot_date=None) -> bool:
        """일일 포트폴리오 스냅샷 저장. 성공 여부를 반환한다.

        snapshot_date: 귀속 날짜 지정(미지정 시 오늘). 비거래일 보충 실행에서
        NAV의 가격 기준일(직전 거래일)로 귀속할 때 사용.
        """
        summary = self.get_portfolio_summary(current_prices)

        ok = save_portfolio_snapshot(
            total_value=summary["total_value"],
            cash=summary["cash"],
            invested=summary["invested"],
            cumulative_return=summary["total_return"],
            mdd=summary["mdd"],
            position_count=summary["position_count"],
            account_key=self.account_key,
            peak_value=self._peak_value,
            snapshot_date=snapshot_date,
        )

        if ok:
            logger.info(
                "포트폴리오 스냅샷 저장: 총={:,.0f}원 | 수익={:.2f}% | MDD={:.2f}%",
                summary["total_value"], summary["total_return"], summary["mdd"],
            )
        else:
            logger.warning(
                "포트폴리오 스냅샷 저장 실패 (계좌: {}) — 커버리지에 빠진다",
                self.account_key or "default",
            )
        return bool(ok)

    def get_paper_performance_report(self, days: int = 30) -> dict:
        """
        Paper 모드 성과 요약 리포트.
        전략별 성과, 포트폴리오 수익률, 거래 비용 비율을 포함합니다.

        Returns:
            {"portfolio": {...}, "by_strategy": {...}, "cost_analysis": {...}}
        """
        summary = self.get_portfolio_summary()
        mode = "paper" if not self._is_live else "live"
        strategy_perf = get_strategy_performance_summary(
            mode=mode,
            account_key=self.account_key if self.account_key else None,
            days=days,
        )
        from database.repositories import get_trade_cash_summary
        cash_summary = get_trade_cash_summary(
            mode=mode,
            account_key=self.account_key if self.account_key else None,
        )
        total_cost = cash_summary["commission"] + cash_summary["tax"] + cash_summary["slippage"]
        cost_to_capital = (total_cost / self.initial_capital * 100) if self.initial_capital > 0 else 0

        report = {
            "portfolio": {
                "total_value": summary["total_value"],
                "total_return_pct": summary["total_return"],
                "mdd_pct": summary["mdd"],
                "position_count": summary["position_count"],
                "mode": mode,
                "days": days,
            },
            "by_strategy": strategy_perf,
            "cost_analysis": {
                "total_commission": cash_summary["commission"],
                "total_tax": cash_summary["tax"],
                "total_slippage": cash_summary["slippage"],
                "total_cost": total_cost,
                "cost_to_capital_pct": round(cost_to_capital, 2),
                "total_trades": cash_summary["total_trades"],
            },
        }

        # 요약 로그
        logger.info(
            "[Paper 성과] 수익={:.2f}% | MDD={:.2f}% | 총비용={:,.0f}원 ({:.2f}% of 자본) | 거래 {}건",
            summary["total_return"], summary["mdd"],
            total_cost, cost_to_capital, cash_summary["total_trades"],
        )
        for strat, perf in strategy_perf.items():
            logger.info(
                "  [{}] 거래 {}건 | 승률 {:.1f}% | PnL {:,.0f}원 | 비용 {:,.0f}원",
                strat, perf["trades"], perf["win_rate"], perf["total_pnl"], perf["total_cost"],
            )

        return report

    def get_current_capital(self) -> float:
        """
        포지션 사이징 등에 쓸 현재 자본.
        Live 모드: KIS 잔고 API 총 평가금액. 실패 시 예외로 fail-closed.
        Paper 모드: DB 기준 총 평가금액.
        """
        summary = self.get_portfolio_summary()
        if self._is_live and summary.get("broker_balance_ok") is False:
            raise LiveBrokerBalanceUnavailable(
                summary.get("broker_balance_error") or "KIS 잔고 조회 실패"
            )
        return float(summary["total_value"])

    def get_available_cash(self) -> float:
        """현재 사용 가능한 현금."""
        summary = self.get_portfolio_summary()
        if self._is_live and summary.get("broker_balance_ok") is False:
            raise LiveBrokerBalanceUnavailable(
                summary.get("broker_balance_error") or "KIS 잔고 조회 실패"
            )
        return float(summary["cash"])

    def sync_with_broker(self, auto_correct: bool = None) -> dict:
        """
        KIS 실제 잔고와 DB 포지션을 대조하여 불일치 시 로깅 및 알림.
        auto_correct=True 시 DB를 증권사 기준으로 자동 보정합니다.
        PositionLock으로 보호하여 주문 실행과 동시 접근을 방지합니다. (감사 H-2 대응)

        Args:
            auto_correct: True면 DB를 증권사 기준으로 보정.
                          None이면 settings.yaml의 position_mismatch_auto_correct 참조.

        Returns:
            {"ok": bool, "mismatches": [...], "corrected": [...], "message": str}
        """
        if auto_correct is None:
            auto_correct = self.config.trading.get("position_mismatch_auto_correct", False)

        with PositionLock():
            return self._sync_with_broker_impl(auto_correct)

    def _sync_with_broker_impl(self, auto_correct: bool) -> dict:
        """sync_with_broker의 실제 구현 (PositionLock 내부에서 실행)."""
        try:
            from api.kis_api import KISApi
            account_no = self.config.get_account_no(self.account_key)
            kis = KISApi(account_no=account_no)
            balance = kis.get_balance()
        except Exception as e:
            logger.error("sync_with_broker: KIS 잔고 조회 실패 — {}", e)
            return {"ok": False, "mismatches": [], "corrected": [], "message": f"잔고 조회 실패: {e}"}

        if not balance or "positions" not in balance:
            logger.warning("sync_with_broker: KIS 잔고 응답 없음")
            return {"ok": False, "mismatches": [], "corrected": [], "message": "잔고 응답 없음"}

        kis_positions = {p["symbol"]: p for p in balance["positions"] if p.get("symbol")}
        db_positions = {p.symbol: p for p in get_all_positions(account_key=self.account_key if self.account_key else None)}
        empty_broker_auto_correct_skipped = False

        mismatches = []
        for symbol, kp in kis_positions.items():
            db_pos = db_positions.get(symbol)
            if not db_pos:
                mismatches.append({
                    "symbol": symbol,
                    "type": "kis_only",
                    "reason": "KIS에는 있으나 DB에 없음",
                    "kis_qty": kp["quantity"],
                    "kis_avg_price": kp.get("avg_price", 0),
                    "kis_current_price": kp.get("current_price", 0),
                    "db_qty": None,
                })
            elif db_pos.quantity != kp["quantity"]:
                mismatches.append({
                    "symbol": symbol,
                    "type": "qty_mismatch",
                    "reason": "수량 불일치",
                    "kis_qty": kp["quantity"],
                    "kis_avg_price": kp.get("avg_price", db_pos.avg_price),
                    "kis_current_price": kp.get("current_price", 0),
                    "db_qty": db_pos.quantity,
                })
        for symbol in db_positions:
            if symbol not in kis_positions:
                mismatches.append({
                    "symbol": symbol,
                    "type": "db_only",
                    "reason": "DB에는 있으나 KIS에 없음",
                    "kis_qty": None,
                    "kis_avg_price": None,
                    "db_qty": db_positions[symbol].quantity,
                })

        corrected = []
        if mismatches:
            msg = f"포지션 불일치 {len(mismatches)}건: " + "; ".join(
                f"{m['symbol']}({m['reason']})" for m in mismatches
            )
            logger.warning("sync_with_broker: {}", msg)

            if auto_correct:
                allow_empty_delete = self.config.trading.get(
                    "position_mismatch_allow_empty_broker_delete",
                    False,
                )
                if not kis_positions and db_positions and not allow_empty_delete:
                    auto_correct = False
                    empty_broker_auto_correct_skipped = True
                    msg += " → 자동 보정 보류(브로커 보유 목록 빈 응답)"
                    logger.warning(
                        "sync_with_broker: KIS 보유 목록이 비어 있어 DB 포지션 자동 삭제를 보류합니다. "
                        "확실한 무보유 계좌라면 position_mismatch_allow_empty_broker_delete=true 설정 후 재시도하세요."
                    )
                else:
                    corrected = self._auto_correct_positions(mismatches)
                    msg += f" → 자동 보정 {len(corrected)}건"
                    logger.info("sync_with_broker: 자동 보정 완료 ({}건)", len(corrected))

            try:
                from core.notifier import Notifier
                notifier = Notifier()
                action_msg = "자동 보정 완료" if auto_correct and corrected else "수동 확인 후 DB 보정하세요"
                notifier.send_message(
                    f"⚠️ **포지션 동기화 불일치**\n{msg}\n{action_msg}",
                    critical=True,
                )
            except Exception as e:
                logger.error("sync_with_broker: 알림 발송 실패 — {}", e)
            result = {"ok": False, "mismatches": mismatches, "corrected": corrected, "message": msg}
            if empty_broker_auto_correct_skipped:
                result["auto_correct_skipped_reason"] = "empty_broker_positions"
            return result
        logger.info("sync_with_broker: DB와 KIS 잔고 일치")
        return {"ok": True, "mismatches": [], "corrected": [], "message": "일치"}

    def _broker_reference_price(self, mismatch: dict) -> float:
        for key in ("kis_avg_price", "kis_current_price"):
            try:
                price = float(mismatch.get(key) or 0)
            except (TypeError, ValueError):
                price = 0
            if price > 0:
                return price
        return 0.0

    def _broker_recovered_targets(self, reference_price: float) -> dict:
        if reference_price <= 0:
            return {}
        from core.risk_manager import RiskManager

        risk_manager = RiskManager(self.config)
        take_profit = risk_manager.calculate_take_profit(reference_price)
        return {
            "stop_loss_price": risk_manager.calculate_stop_loss(reference_price),
            "take_profit_price": take_profit.get("target_final"),
            "trailing_stop_price": risk_manager.calculate_trailing_stop(reference_price),
        }

    def _auto_correct_positions(self, mismatches: list) -> list:
        """
        불일치 목록을 기반으로 DB 포지션을 KIS 잔고에 맞춰 보정합니다.

        - kis_only: KIS에만 있는 포지션 → DB에 추가
        - db_only: DB에만 있는 포지션 → DB에서 삭제
        - qty_mismatch: 수량 불일치 → DB 수량을 KIS 기준으로 수정

        Returns:
            보정된 항목 리스트
        """
        from database.repositories import delete_position, replace_position_from_broker

        corrected = []
        ak = self.account_key or ""

        for m in mismatches:
            symbol = m["symbol"]
            try:
                if m["type"] == "kis_only":
                    reference_price = self._broker_reference_price(m)
                    if reference_price <= 0:
                        raise ValueError("KIS-only 포지션 기준 가격이 없어 자동 추가 보정 불가")
                    targets = self._broker_recovered_targets(reference_price)
                    replace_position_from_broker(
                        symbol=symbol,
                        avg_price=reference_price,
                        quantity=int(m["kis_qty"]),
                        strategy="broker_sync_recovered",
                        account_key=ak,
                        **targets,
                    )
                    corrected.append({
                        "symbol": symbol,
                        "action": "added",
                        "qty": m["kis_qty"],
                        "risk_targets_recovered": True,
                    })
                    logger.info("자동 보정: {} DB에 추가 ({}주)", symbol, m["kis_qty"])

                elif m["type"] == "db_only":
                    delete_position(symbol, account_key=ak)
                    corrected.append({"symbol": symbol, "action": "deleted", "qty": m["db_qty"]})
                    logger.info("자동 보정: {} DB에서 삭제 (KIS에 없음)", symbol)

                elif m["type"] == "qty_mismatch":
                    kis_qty = int(m["kis_qty"])
                    if kis_qty == 0:
                        delete_position(symbol, account_key=ak)
                        corrected.append({"symbol": symbol, "action": "deleted", "qty": 0})
                    else:
                        reference_price = self._broker_reference_price(m)
                        if reference_price <= 0:
                            raise ValueError("수량 불일치 포지션 기준 가격이 없어 자동 수량 보정 불가")
                        targets = self._broker_recovered_targets(reference_price)
                        replace_position_from_broker(
                            symbol=symbol,
                            avg_price=reference_price,
                            quantity=kis_qty,
                            strategy="broker_sync_recovered",
                            account_key=ak,
                            **targets,
                        )
                        corrected.append({
                            "symbol": symbol,
                            "action": "qty_updated",
                            "old_qty": m["db_qty"],
                            "new_qty": kis_qty,
                            "risk_targets_recovered": True,
                        })
                    logger.info("자동 보정: {} 수량 {}→{}", symbol, m["db_qty"], kis_qty)

            except Exception as e:
                logger.error("자동 보정 실패 ({}): {}", symbol, e)

        return corrected

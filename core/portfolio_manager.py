"""
포트폴리오 관리 모듈
- 보유 포지션 관리, 잔고/수익률 추적
- KIS 잔고와 DB 포지션 동기화 (sync_with_broker)
"""

from loguru import logger

from config.config_loader import Config
from database.repositories import (
    get_all_positions,
    get_trade_cash_summary,
    save_portfolio_snapshot,
)


class PortfolioManager:
    """
    포트폴리오 관리자
    - account_key: 전략별 계좌 구분 (다중 계좌 시)
    """

    def __init__(self, config: Config = None, account_key: str = ""):
        self.config = config or Config.get()
        self.account_key = account_key or ""
        self.initial_capital = self.config.risk_params.get(
            "position_sizing", {}
        ).get("initial_capital", 10000000)
        self._peak_value = self.initial_capital
        self._is_live = self.config.trading.get("mode", "paper") == "live"
        logger.info(
            "PortfolioManager 초기화 (초기 자본: {:,.0f}원, 모드: {}, 계좌: {})",
            self.initial_capital,
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
        cash = self.initial_capital + cash_summary["cash_delta"]
        total_value = cash + current_value
        realized_pnl = cash + invested - self.initial_capital
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

        if self._is_live:
            try:
                from api.kis_api import KISApi
                account_no = self.config.get_account_no(self.account_key)
                balance = KISApi(account_no=account_no).get_balance()
                if balance and "total_value" in balance:
                    total_value = float(balance["total_value"])
                    cash = float(balance.get("cash", total_value - current_value))
            except Exception as e:
                logger.warning("KIS 잔고 조회 실패 — DB 기준으로 대체: {}", e)

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
            realized_pnl = total_value - self.initial_capital - unrealized_pnl

        total_return = ((total_value / self.initial_capital) - 1) * 100 if self.initial_capital > 0 else 0

        if total_value > self._peak_value:
            self._peak_value = total_value
        mdd = ((self._peak_value - total_value) / self._peak_value) * 100 if self._peak_value > 0 else 0

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
            "positions": position_details,
        }

    def save_daily_snapshot(self, current_prices: dict = None):
        """일일 포트폴리오 스냅샷 저장"""
        summary = self.get_portfolio_summary(current_prices)

        save_portfolio_snapshot(
            total_value=summary["total_value"],
            cash=summary["cash"],
            invested=summary["invested"],
            cumulative_return=summary["total_return"],
            mdd=summary["mdd"],
            position_count=summary["position_count"],
            account_key=self.account_key,
        )

        logger.info(
            "포트폴리오 스냅샷 저장: 총={:,.0f}원 | 수익={:.2f}% | MDD={:.2f}%",
            summary["total_value"], summary["total_return"], summary["mdd"],
        )

    def get_current_capital(self) -> float:
        """
        포지션 사이징 등에 쓸 현재 자본.
        Live 모드: KIS 잔고 API 총 평가금액. 실패 시 DB 기준.
        Paper 모드: DB 기준 총 평가금액.
        """
        summary = self.get_portfolio_summary()
        return float(summary["total_value"])

    def get_available_cash(self) -> float:
        """현재 사용 가능한 현금."""
        summary = self.get_portfolio_summary()
        return float(summary["cash"])

    def sync_with_broker(self, auto_correct: bool = False) -> dict:
        """
        KIS 실제 잔고와 DB 포지션을 대조하여 불일치 시 로깅 및 알림.

        Args:
            auto_correct: True면 DB를 증권사 기준으로 보정 (미구현 시 무시)

        Returns:
            {"ok": bool, "mismatches": [...], "message": str}
        """
        try:
            from api.kis_api import KISApi
            account_no = self.config.get_account_no(self.account_key)
            kis = KISApi(account_no=account_no)
            balance = kis.get_balance()
        except Exception as e:
            logger.error("sync_with_broker: KIS 잔고 조회 실패 — {}", e)
            return {"ok": False, "mismatches": [], "message": f"잔고 조회 실패: {e}"}

        if not balance or "positions" not in balance:
            logger.warning("sync_with_broker: KIS 잔고 응답 없음")
            return {"ok": False, "mismatches": [], "message": "잔고 응답 없음"}

        kis_positions = {p["symbol"]: p for p in balance["positions"] if p.get("symbol")}
        db_positions = {p.symbol: p for p in get_all_positions(account_key=self.account_key if self.account_key else None)}

        mismatches = []
        for symbol, kp in kis_positions.items():
            db_pos = db_positions.get(symbol)
            if not db_pos:
                mismatches.append({
                    "symbol": symbol,
                    "reason": "KIS에는 있으나 DB에 없음",
                    "kis_qty": kp["quantity"],
                    "db_qty": None,
                })
            elif db_pos.quantity != kp["quantity"]:
                mismatches.append({
                    "symbol": symbol,
                    "reason": "수량 불일치",
                    "kis_qty": kp["quantity"],
                    "db_qty": db_pos.quantity,
                })
        for symbol in db_positions:
            if symbol not in kis_positions:
                mismatches.append({
                    "symbol": symbol,
                    "reason": "DB에는 있으나 KIS에 없음",
                    "kis_qty": None,
                    "db_qty": db_positions[symbol].quantity,
                })

        if mismatches:
            msg = f"포지션 불일치 {len(mismatches)}건: " + "; ".join(
                f"{m['symbol']}({m['reason']})" for m in mismatches
            )
            logger.warning("sync_with_broker: {}", msg)
            try:
                from core.notifier import Notifier
                notifier = Notifier()
                notifier.send_message(
                    f"⚠️ **포지션 동기화 불일치**\n{msg}\n수동 확인 후 DB 보정하세요.",
                    critical=True,
                )
            except Exception as e:
                logger.error("sync_with_broker: 알림 발송 실패 — {}", e)
            return {"ok": False, "mismatches": mismatches, "message": msg}
        logger.info("sync_with_broker: DB와 KIS 잔고 일치")
        return {"ok": True, "mismatches": [], "message": "일치"}

"""
바스켓 포트폴리오 리밸런싱 모듈

fintics의 BasketRebalanceTask 개념을 차용하여 Python으로 구현.
- 종목별 목표 비중과 실제 비중의 드리프트를 계산
- 드리프트 기반(threshold) 또는 주기 기반(weekly/monthly) 리밸런싱 트리거
- 신호 가중 모드: 전략 점수에 따라 목표 비중을 동적 조정
- 리밸런싱 주문 생성 (매수/매도 목록)
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import yaml
from loguru import logger

from config.config_loader import Config
from core.portfolio_manager import PortfolioManager
from core.data_collector import DataCollector
from database.repositories import get_all_positions


class RebalanceOrder:
    """리밸런싱 개별 주문 정보."""

    def __init__(self, symbol: str, action: str, quantity: int, price: float, reason: str = ""):
        self.symbol = symbol
        self.action = action        # BUY / SELL
        self.quantity = quantity
        self.price = price
        self.reason = reason

    def __repr__(self):
        return f"<RebalanceOrder({self.symbol} {self.action} {self.quantity}주 @{self.price:,.0f} | {self.reason})>"


class BasketRebalancer:
    """
    바스켓 포트폴리오 리밸런서.

    사용법:
        rebalancer = BasketRebalancer(basket_name="kr_blue_chip")
        orders = rebalancer.plan_rebalance()
        result = rebalancer.execute(orders)
    """

    def __init__(
        self,
        basket_name: str,
        config: Config = None,
        account_key: str = "",
    ):
        self.config = config or Config.get()
        self.account_key = account_key
        self.basket_name = basket_name

        baskets_cfg = self._load_baskets_config()
        if basket_name not in baskets_cfg:
            available = ", ".join(baskets_cfg.keys())
            raise ValueError(f"바스켓 '{basket_name}' 없음. 사용 가능: {available}")

        self.basket = baskets_cfg[basket_name]
        self.holdings: dict[str, float] = self.basket.get("holdings", {})
        self.rebalance_cfg = self.basket.get("rebalance", {})

        self.portfolio_mgr = PortfolioManager(self.config, account_key=account_key)
        self.data_collector = DataCollector(self.config)

        self._risk_params = self.config.risk_params
        self._min_cash_ratio = (
            self._risk_params.get("diversification", {}).get("min_cash_ratio", 0.20)
        )

        logger.info(
            "BasketRebalancer 초기화: {} ({}종목, trigger={})",
            basket_name, len(self.holdings), self.rebalance_cfg.get("trigger", "drift"),
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @staticmethod
    def _load_baskets_config() -> dict:
        """config/baskets.yaml 로드."""
        path = os.path.join(os.path.dirname(__file__), "..", "config", "baskets.yaml")
        path = os.path.normpath(path)
        if not os.path.exists(path):
            logger.warning("baskets.yaml 없음: {}", path)
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("baskets", {})

    @staticmethod
    def get_enabled_baskets() -> list[str]:
        """enabled: true인 바스켓 이름 목록."""
        baskets = BasketRebalancer._load_baskets_config()
        return [name for name, cfg in baskets.items() if cfg.get("enabled", False)]

    # ------------------------------------------------------------------
    # 비중 계산
    # ------------------------------------------------------------------

    def get_target_weights(self) -> dict[str, float]:
        """
        목표 비중 반환. signal_weighted 모드 시 전략 점수로 조정.
        합이 1.0을 초과하면 정규화합니다.
        """
        base = dict(self.holdings)

        if self.basket.get("signal_weighted", False):
            base = self._apply_signal_weights(base)

        total = sum(base.values())
        if total > 0 and abs(total - 1.0) > 0.001:
            base = {s: w / total for s, w in base.items()}

        return base

    def get_current_weights(self, prices: dict[str, float] = None) -> dict[str, float]:
        """
        현재 포트폴리오 내 바스켓 종목의 실제 비중 계산.
        바스켓 외 종목은 무시합니다.
        """
        prices = prices or self._fetch_current_prices()
        summary = self.portfolio_mgr.get_portfolio_summary(current_prices=prices)
        total_value = summary.get("total_value", 0)
        if total_value <= 0:
            return {s: 0.0 for s in self.holdings}

        investable = total_value * (1.0 - self._min_cash_ratio)

        positions = get_all_positions(
            account_key=self.account_key if self.account_key else None,
        )
        pos_map = {p.symbol: p for p in positions}

        weights = {}
        for symbol in self.holdings:
            pos = pos_map.get(symbol)
            if pos:
                price = prices.get(symbol, pos.avg_price)
                value = price * pos.quantity
                weights[symbol] = value / investable if investable > 0 else 0.0
            else:
                weights[symbol] = 0.0

        return weights

    def calculate_drift(self, prices: dict[str, float] = None) -> dict[str, dict]:
        """
        종목별 드리프트(목표 비중 - 실제 비중) 계산.

        Returns:
            {symbol: {"target": 0.25, "actual": 0.20, "drift": 0.05, "drift_pct": 20.0}}
        """
        targets = self.get_target_weights()
        actuals = self.get_current_weights(prices)

        result = {}
        for symbol in targets:
            t = targets[symbol]
            a = actuals.get(symbol, 0.0)
            drift = t - a
            drift_pct = (drift / t * 100) if t > 0 else 0
            result[symbol] = {
                "target": round(t, 4),
                "actual": round(a, 4),
                "drift": round(drift, 4),
                "drift_pct": round(drift_pct, 1),
            }

        return result

    # ------------------------------------------------------------------
    # 트리거 판단
    # ------------------------------------------------------------------

    def should_rebalance(self, prices: dict[str, float] = None) -> tuple[bool, str]:
        """리밸런싱 실행 여부 판단. (실행 여부, 사유) 반환."""
        trigger = self.rebalance_cfg.get("trigger", "drift")

        if trigger == "drift":
            threshold = self.rebalance_cfg.get("drift_threshold", 0.05)
            drifts = self.calculate_drift(prices)
            max_drift = max(abs(d["drift"]) for d in drifts.values()) if drifts else 0
            if max_drift >= threshold:
                return True, f"최대 드리프트 {max_drift:.1%} >= 임계값 {threshold:.1%}"
            return False, f"드리프트 {max_drift:.1%} < 임계값 {threshold:.1%}"

        elif trigger == "weekly":
            weekday = self.rebalance_cfg.get("weekday", 0)
            today = datetime.now().weekday()
            if today == weekday:
                return True, f"주간 리밸런싱 (요일: {today})"
            return False, f"리밸런싱 요일 아님 (오늘: {today}, 대상: {weekday})"

        elif trigger == "monthly":
            day = self.rebalance_cfg.get("day", 1)
            today = datetime.now().day
            if today == day:
                return True, f"월간 리밸런싱 (일: {day})"
            return False, f"리밸런싱 일 아님 (오늘: {today}, 대상: {day})"

        return False, f"알 수 없는 트리거: {trigger}"

    # ------------------------------------------------------------------
    # 리밸런싱 주문 계획
    # ------------------------------------------------------------------

    def plan_rebalance(self, prices: dict[str, float] = None) -> list[RebalanceOrder]:
        """
        리밸런싱 주문 목록 생성 (실행 전 미리보기).

        SELL 주문을 먼저 배치하여 현금 확보 후 BUY 주문을 실행할 수 있도록 합니다.
        max_turnover_ratio를 초과하는 리밸런싱은 부분 실행합니다.
        """
        prices = prices or self._fetch_current_prices()
        summary = self.portfolio_mgr.get_portfolio_summary(current_prices=prices)
        total_value = summary.get("total_value", 0)
        if total_value <= 0:
            logger.warning("총 자산이 0 — 리밸런싱 불가")
            return []

        investable = total_value * (1.0 - self._min_cash_ratio)
        targets = self.get_target_weights()
        actuals = self.get_current_weights(prices)

        min_trade = self.rebalance_cfg.get("min_trade_amount", 100000)
        max_turnover = self.rebalance_cfg.get("max_turnover_ratio", 0.30)
        max_turnover_amount = total_value * max_turnover

        positions = get_all_positions(
            account_key=self.account_key if self.account_key else None,
        )
        pos_map = {p.symbol: p for p in positions}

        orders: list[RebalanceOrder] = []
        total_trade_amount = 0.0

        for symbol in targets:
            target_w = targets[symbol]
            actual_w = actuals.get(symbol, 0.0)
            drift = target_w - actual_w

            target_value = investable * target_w
            actual_value = investable * actual_w
            trade_value = abs(target_value - actual_value)

            if trade_value < min_trade:
                continue

            if total_trade_amount + trade_value > max_turnover_amount:
                trade_value = max(0, max_turnover_amount - total_trade_amount)
                if trade_value < min_trade:
                    break

            price = prices.get(symbol, 0)
            if price <= 0:
                logger.warning("종목 {} 가격 조회 불가 — 스킵", symbol)
                continue

            quantity = int(trade_value / price)
            if quantity <= 0:
                continue

            if drift > 0:
                orders.append(RebalanceOrder(
                    symbol=symbol, action="BUY", quantity=quantity, price=price,
                    reason=f"비중 부족 ({actual_w:.1%} → {target_w:.1%}, +{drift:.1%})",
                ))
            elif drift < 0:
                pos = pos_map.get(symbol)
                if pos:
                    sell_qty = min(quantity, pos.quantity)
                    if sell_qty > 0:
                        orders.append(RebalanceOrder(
                            symbol=symbol, action="SELL", quantity=sell_qty, price=price,
                            reason=f"비중 초과 ({actual_w:.1%} → {target_w:.1%}, {drift:.1%})",
                        ))

            total_trade_amount += trade_value

        sells = [o for o in orders if o.action == "SELL"]
        buys = [o for o in orders if o.action == "BUY"]
        ordered = sells + buys

        logger.info(
            "리밸런싱 계획: 매도 {}건, 매수 {}건, 총 거래액 {:,.0f}원 (상한 {:,.0f}원)",
            len(sells), len(buys), total_trade_amount, max_turnover_amount,
        )

        return ordered

    def execute(self, orders: list[RebalanceOrder], dry_run: bool = False) -> dict:
        """
        리밸런싱 주문 실행.

        Args:
            orders: plan_rebalance()가 반환한 주문 목록
            dry_run: True이면 실제 주문 없이 로그만 출력

        Returns:
            {"executed": int, "skipped": int, "failed": int, "details": list}
        """
        from core.order_executor import OrderExecutor

        executor = OrderExecutor(self.config, account_key=self.account_key)
        results = {"executed": 0, "skipped": 0, "failed": 0, "details": []}

        for order in orders:
            if dry_run:
                logger.info("[DRY RUN] {} {} {}주 @{:,.0f} — {}",
                            order.action, order.symbol, order.quantity, order.price, order.reason)
                results["skipped"] += 1
                results["details"].append({"order": repr(order), "status": "dry_run"})
                continue

            try:
                if order.action == "BUY":
                    res = executor.execute_buy(
                        symbol=order.symbol, price=order.price, quantity=order.quantity,
                        reason=f"리밸런싱: {order.reason}", strategy="basket_rebalance",
                    )
                else:
                    res = executor.execute_sell(
                        symbol=order.symbol, price=order.price, quantity=order.quantity,
                        reason=f"리밸런싱: {order.reason}", strategy="basket_rebalance",
                    )

                if res.get("success"):
                    results["executed"] += 1
                    results["details"].append({"order": repr(order), "status": "success"})
                else:
                    results["failed"] += 1
                    results["details"].append({
                        "order": repr(order), "status": "failed",
                        "reason": res.get("reason", "unknown"),
                    })

            except Exception as e:
                logger.error("리밸런싱 주문 실행 오류: {} — {}", order.symbol, e)
                results["failed"] += 1
                results["details"].append({"order": repr(order), "status": "error", "reason": str(e)})

        logger.info(
            "리밸런싱 완료: 실행 {}건, 스킵 {}건, 실패 {}건",
            results["executed"], results["skipped"], results["failed"],
        )
        return results

    # ------------------------------------------------------------------
    # 리포트
    # ------------------------------------------------------------------

    def get_status_report(self) -> str:
        """바스켓 현황 리포트 문자열 반환."""
        prices = self._fetch_current_prices()
        drifts = self.calculate_drift(prices)
        should, reason = self.should_rebalance(prices)

        lines = [
            f"=== 바스켓: {self.basket.get('name', self.basket_name)} ===",
            f"종목 수: {len(self.holdings)}",
            f"리밸런싱 트리거: {self.rebalance_cfg.get('trigger', 'drift')}",
            f"리밸런싱 필요: {'예' if should else '아니오'} ({reason})",
            "",
            f"{'종목':>8}  {'목표':>6}  {'실제':>6}  {'드리프트':>8}",
            "-" * 40,
        ]

        for symbol, d in sorted(drifts.items(), key=lambda x: abs(x[1]["drift"]), reverse=True):
            lines.append(
                f"{symbol:>8}  {d['target']:>5.1%}  {d['actual']:>5.1%}  {d['drift']:>+7.1%}"
            )

        if should:
            orders = self.plan_rebalance(prices)
            if orders:
                lines.append("")
                lines.append("--- 계획된 주문 ---")
                for o in orders:
                    lines.append(f"  {o.action:4} {o.symbol} {o.quantity}주 @{o.price:,.0f} | {o.reason}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _fetch_current_prices(self) -> dict[str, float]:
        """바스켓 종목의 현재가를 일괄 조회."""
        prices = {}
        for symbol in self.holdings:
            try:
                df = self.data_collector.fetch_korean_stock(symbol, days=5)
                if df is not None and not df.empty:
                    prices[symbol] = float(df["close"].iloc[-1])
            except Exception as e:
                logger.warning("가격 조회 실패 {}: {}", symbol, e)
        return prices

    def _apply_signal_weights(self, base: dict[str, float]) -> dict[str, float]:
        """전략 점수에 따라 목표 비중을 동적 조정."""
        strategy_name = self.basket.get("signal_strategy", "scoring")
        weight_range = self.basket.get("signal_weight_range", [0.5, 1.5])
        min_mult, max_mult = weight_range[0], weight_range[1]

        from strategies import create_strategy

        try:
            strategy = create_strategy(strategy_name, self.config)
        except Exception as e:
            logger.warning("신호 가중 전략 생성 실패: {} — 기본 비중 사용", e)
            return base

        adjusted = {}
        for symbol, weight in base.items():
            try:
                df = self.data_collector.fetch_korean_stock(symbol, days=120)
                if df is None or df.empty:
                    adjusted[symbol] = weight
                    continue

                df = strategy.analyze(df)
                if "total_score" not in df.columns:
                    adjusted[symbol] = weight
                    continue

                score = float(df["total_score"].iloc[-1])
                buy_th = self.config.strategies.get("scoring", {}).get("buy_threshold", 2)
                sell_th = self.config.strategies.get("scoring", {}).get("sell_threshold", -2)
                score_range = buy_th - sell_th if buy_th != sell_th else 1

                normalized = (score - sell_th) / score_range
                multiplier = min_mult + normalized * (max_mult - min_mult)
                multiplier = max(min_mult, min(max_mult, multiplier))

                adjusted[symbol] = weight * multiplier
                logger.debug("{} 신호 가중: score={:.1f}, mult={:.2f}, weight {:.1%}→{:.1%}",
                             symbol, score, multiplier, weight, adjusted[symbol])

            except Exception as e:
                logger.warning("{} 신호 가중 실패: {} — 기본 비중 사용", symbol, e)
                adjusted[symbol] = weight

        return adjusted

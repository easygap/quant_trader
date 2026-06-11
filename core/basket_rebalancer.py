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
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from loguru import logger

_KST = ZoneInfo("Asia/Seoul")

from config.config_loader import Config
from core.portfolio_manager import PortfolioManager
from core.data_collector import DataCollector
from database.repositories import get_all_positions


def rebalance_live_strategy_id(basket_name: str) -> str:
    """live rebalance의 gate/account/order tag를 묶는 승인 단위."""
    return f"basket_rebalance:{basket_name}"


def check_basket_account_isolation(basket_names, config, mode: str) -> list[str]:
    """여러 enabled 바스켓이 같은 계좌(자본 풀)를 공유하는지 검사한다. fail-closed.

    각 BasketRebalancer는 자기 목표 비중을 '총자산 × stock_fraction'에 독립적으로
    배분한다. 두 바스켓이 같은 자본(계좌)을 공유하면 예컨대 80% + 80% = 160%를
    배분하려 들어 과배분·상호 간섭이 생긴다.

    paper: 바스켓별 가상 계정 키(basket_rebalance:<name>)로 DB가 격리되고 각자
    initial_capital 기준으로 독립 집계되므로 자연 격리 — 통과.
    live: 같은 KIS 실계좌(잔고)를 공유하면 차단. 다중 바스켓 live 운영은
    kis_api.accounts에 basket_rebalance:<name>별 계좌를 분리 지정해야 한다.

    반환: 이슈 문자열 리스트 (빈 리스트 = 통과).
    """
    names = list(basket_names or [])
    if len(names) <= 1 or str(mode).lower() != "live":
        return []

    by_account: dict[str, list[str]] = {}
    for name in names:
        try:
            acct = config.get_account_no(rebalance_live_strategy_id(name)) or "(기본 계좌)"
        except Exception as exc:
            return [f"바스켓 '{name}' 계좌 해석 실패(fail-closed): {exc}"]
        by_account.setdefault(acct, []).append(name)

    return [
        f"live 계좌 '{acct}'를 바스켓 {', '.join(ns)}가 공유합니다 — 같은 잔고에 "
        "목표 비중이 중복 배분됩니다. kis_api.accounts에 바스켓별 계좌를 분리 지정하세요."
        for acct, ns in by_account.items()
        if len(ns) > 1
    ]


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
        execution_strategy: str = "",
    ):
        self.config = config or Config.get()
        # 계정·귀속 키 기본값: paper/live 공통으로 바스켓 전용 키(basket_rebalance:<name>).
        # 기본 계정("")은 전 계정 합산 뷰라서 다른 전략의 paper 거래 한 건에도 NAV·드리프트·
        # 평가가 오염되고, 이름 없는 strategy("basket_rebalance")로는 어느 바스켓의 트랙레코드
        # 인지 귀속이 불가능하다(다른 바스켓 기록으로 승격되는 구멍). 키로 격리·귀속한다.
        default_key = rebalance_live_strategy_id(basket_name)
        self.account_key = account_key or default_key
        self.execution_strategy = execution_strategy or default_key
        self.basket_name = basket_name

        baskets_cfg = self._load_baskets_config()
        if basket_name not in baskets_cfg:
            available = ", ".join(baskets_cfg.keys())
            raise ValueError(f"바스켓 '{basket_name}' 없음. 사용 가능: {available}")

        self.basket = baskets_cfg[basket_name]
        self.holdings: dict[str, float] = self.basket.get("holdings", {})
        self.rebalance_cfg = self.basket.get("rebalance", {})

        # 바스켓별 자본: baskets.yaml의 initial_capital이 있으면 이 바스켓 계정의
        # 초기 자본으로 사용(전역 risk_params 값 대신). 고가 종목 슬롯(1주 가격 >
        # 목표 거래금액)을 채우려면 자본 규모가 설계의 일부여야 한다 — 운영자 레버.
        basket_capital = self.basket.get("initial_capital")
        # self.account_key(기본 키 적용 후)를 전달한다 — 원시 파라미터(account_key)를
        # 그대로 넘기면 인자 생략 시 portfolio_mgr가 ''(전 계정 합산 뷰)를 보게 되는
        # 잠재 버그(호출부들이 항상 명시 전달해 와서 가려져 있었다).
        self.portfolio_mgr = PortfolioManager(
            self.config,
            account_key=self.account_key,
            initial_capital=float(basket_capital) if basket_capital is not None else None,
        )
        self.data_collector = DataCollector(self.config)

        self._risk_params = self.config.risk_params
        self._min_cash_ratio = (
            self._risk_params.get("diversification", {}).get("min_cash_ratio", 0.20)
        )
        # target_stock_weight: 바스켓이 명시하는 주식 목표 비중(총자산 대비). 나머지는 현금으로
        # 보유한다. 미지정(None)이면 기존 동작(min_cash_ratio만 현금 유보)을 유지한다.
        # 정적 자산배분(docs/STATIC_ALLOCATION.md) 결론을 실행 기능으로 구현: 예) 0.5면 주식 50%
        # 보유 + 50% 현금 → 낙폭을 절반으로 줄인다. min_cash_ratio가 더 큰 현금을 요구하면 그쪽을 따른다.
        tsw = self.basket.get("target_stock_weight")
        self._target_stock_weight = float(tsw) if tsw is not None else None

        logger.info(
            "BasketRebalancer 초기화: {} ({}종목, trigger={}, target_stock_weight={})",
            basket_name, len(self.holdings), self.rebalance_cfg.get("trigger", "drift"),
            self._target_stock_weight if self._target_stock_weight is not None else "기본",
        )

    def _stock_fraction(self) -> float:
        """총자산 중 주식에 배정할 비중. target_stock_weight가 있으면 그것을(단, min_cash_ratio
        가 요구하는 현금 하한은 항상 지킴), 없으면 (1 - min_cash_ratio)."""
        max_stock = 1.0 - self._min_cash_ratio
        if self._target_stock_weight is None:
            return max_stock
        return max(0.0, min(self._target_stock_weight, max_stock))

    def _is_live(self) -> bool:
        """실전(live) 모드 여부."""
        return str(self.config.trading.get("mode", "paper")).lower() == "live"

    def save_daily_nav_snapshot(self) -> bool:
        """바스켓 계정의 일일 NAV 스냅샷 저장. 트랙레코드 시계열의 1행.

        보유 종목 가격이 전부 확보됐을 때만 저장한다 — 가격 미확보 시
        avg_price 폴백으로 평가된 가짜 NAV가 '커버된 영업일'로 집계되는 것보다,
        스킵하고 health의 끊김 감지에 노출되는 편이 정직하다.

        귀속 날짜는 NAV의 가격 기준일이다: 비거래일(주말·휴장일) 보충 실행에서
        조회되는 가격은 직전 거래일 종가이므로 그 거래일로 귀속한다 — PC가 꺼져
        있던 거래일을 다음날 보충 실행이 정당하게 커버한다(주말 날짜 스냅샷은
        커버리지에 영원히 안 잡히는 낭비였다). (account_key, date) upsert 멱등.
        """
        try:
            snapshot = getattr(self, "_market_snapshot", None) or self._fetch_market_snapshot()
            prices = {s: v["price"] for s, v in snapshot.items()}
            positions = get_all_positions(account_key=self.account_key)
            missing = [p.symbol for p in positions if p.symbol not in prices]
            if missing:
                logger.warning(
                    "바스켓 '{}' NAV 스냅샷 스킵 — 가격 미확보 종목: {} (가짜 NAV 방지)",
                    self.basket_name, missing,
                )
                return False
            self.portfolio_mgr.save_daily_snapshot(
                current_prices=prices or None,
                snapshot_date=self._nav_attribution_date(),
            )
            return True
        except Exception as e:
            logger.warning("바스켓 '{}' NAV 스냅샷 저장 실패: {}", self.basket_name, e)
            return False

    def _nav_attribution_date(self) -> datetime:
        """NAV 스냅샷 귀속 날짜: 오늘이 거래일이면 오늘, 아니면 직전 거래일.

        조회 가격이 직전 거래일 종가이므로 그 날짜가 정직한 귀속일이다.
        거래일 판정 실패 시 오늘로 폴백(보수적 — 기존 동작).
        """
        now = datetime.now(_KST).replace(tzinfo=None)
        try:
            from core.trading_hours import TradingHours

            th = TradingHours(self.config)
            d = now
            for _ in range(15):  # 최장 연휴 커버
                if th.is_trading_day(d):
                    return d
                d -= timedelta(days=1)
        except Exception as e:
            logger.debug("거래일 판정 실패 — 오늘로 귀속: {}", e)
        return now

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

        investable = total_value * self._stock_fraction()

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
            # 한국 거래일 기준(KST). 호스트 TZ가 UTC면 자정 부근에 요일이 어긋날 수 있다.
            today = datetime.now(_KST).weekday()
            if today == weekday:
                return True, f"주간 리밸런싱 (요일: {today})"
            return False, f"리밸런싱 요일 아님 (오늘: {today}, 대상: {weekday})"

        elif trigger == "monthly":
            day = self.rebalance_cfg.get("day", 1)
            today = datetime.now(_KST).day
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
        # live에서 KIS 잔고가 확인되지 않으면(broker_balance_ok=False) stale DB 자본으로
        # 주문 수량을 사이징하지 않는다 — get_current_capital/get_available_cash의 fail-closed와 동일 기조.
        if self._is_live() and summary.get("broker_balance_ok") is False:
            logger.error("live 리밸런싱 중단: KIS 잔고 미확인 — stale 자본으로 주문 사이징 금지(fail-closed)")
            return []
        total_value = summary.get("total_value", 0)
        if total_value <= 0:
            logger.warning("총 자산이 0 — 리밸런싱 불가")
            return []

        investable = total_value * self._stock_fraction()
        targets = self.get_target_weights()
        actuals = self.get_current_weights(prices)

        min_trade = self.rebalance_cfg.get("min_trade_amount", 100000)
        max_turnover = self.rebalance_cfg.get("max_turnover_ratio", 0.30)
        max_turnover_amount = total_value * max_turnover

        positions = get_all_positions(
            account_key=self.account_key if self.account_key else None,
        )
        pos_map = {p.symbol: p for p in positions}

        # 1) 후보 거래를 먼저 모두 계산(회전율 예산 적용 전). 거래액은 실제 주문 명목금액 기준.
        candidates: list[tuple[RebalanceOrder, float]] = []
        for symbol in targets:
            target_w = targets[symbol]
            actual_w = actuals.get(symbol, 0.0)
            drift = target_w - actual_w
            trade_value = abs(investable * target_w - investable * actual_w)
            if trade_value < min_trade:
                continue
            price = prices.get(symbol, 0)
            if price <= 0:
                logger.warning("종목 {} 가격 조회 불가 — 스킵", symbol)
                continue
            quantity = int(trade_value / price)
            if quantity <= 0:
                if drift > 0:
                    # 1주 가격이 목표 거래금액을 초과 — 현재 자본 규모로는 이 슬롯을
                    # 영원히 채울 수 없다(예: 자본 1,000만·목표 8%=80만 < SK하이닉스
                    # 1주 213만). 침묵 스킵하면 운영자가 모른 채 배분이 설계와
                    # 달라진다 — 자본 증액 또는 비중 조정이 필요한 운영자 결정 사항.
                    logger.warning(
                        "종목 {} 채움 불가: 1주 가격 {:,.0f}원 > 목표 거래금액 {:,.0f}원 "
                        "— 자본 증액 또는 baskets.yaml 비중 조정 필요 (현재 미보유 비중 {:.1%})",
                        symbol, price, trade_value, drift,
                    )
                continue
            if drift > 0:
                candidates.append((RebalanceOrder(
                    symbol=symbol, action="BUY", quantity=quantity, price=price,
                    reason=f"비중 부족 ({actual_w:.1%} → {target_w:.1%}, +{drift:.1%})",
                ), quantity * price))
            elif drift < 0:
                pos = pos_map.get(symbol)
                if not pos:
                    continue
                sell_qty = min(quantity, pos.quantity)
                if sell_qty <= 0:
                    continue
                candidates.append((RebalanceOrder(
                    symbol=symbol, action="SELL", quantity=sell_qty, price=price,
                    reason=f"비중 초과 ({actual_w:.1%} → {target_w:.1%}, {drift:.1%})",
                ), sell_qty * price))

        # 2) SELL을 먼저(현금 확보) 두고 거래액 큰 순으로 정렬해 회전율 예산 우선권을 준다.
        #    (기존엔 dict 순서대로라 BUY가 예산을 먼저 소진해 자금원 SELL이 누락될 수 있었다.)
        candidates.sort(key=lambda c: (0 if c[0].action == "SELL" else 1, -c[1]))

        # 3) 회전율 예산 적용: 개별 거래가 예산을 넘으면 그 거래만 건너뛰고(continue) 더 작은
        #    거래는 계속 검토한다(기존 break는 이후 거래를 모두 누락시켰다).
        orders: list[RebalanceOrder] = []
        total_trade_amount = 0.0
        for order, notional in candidates:
            remaining = max_turnover_amount - total_trade_amount
            if remaining < min_trade:
                # 예산 소진 — 이후 후보는 모두 min_trade 이상이라 어차피 담을 수 없다.
                break
            if notional > remaining:
                # 부분 실행: 예산 잔여분만큼 수량을 줄여 집행한다(드리프트 점진 수렴).
                shrunk_qty = int(remaining / order.price)
                if shrunk_qty <= 0:
                    continue
                shrunk_notional = shrunk_qty * order.price
                if shrunk_notional < min_trade:
                    continue
                order.quantity = shrunk_qty
                order.reason += " (회전상한 부분 실행)"
                notional = shrunk_notional
            orders.append(order)
            total_trade_amount += notional

        sells = [o for o in orders if o.action == "SELL"]
        buys = [o for o in orders if o.action == "BUY"]
        ordered = sells + buys

        logger.info(
            "리밸런싱 계획: 매도 {}건, 매수 {}건, 총 거래액 {:,.0f}원 (상한 {:,.0f}원)",
            len(sells), len(buys), total_trade_amount, max_turnover_amount,
        )

        return ordered

    def execute(
        self,
        orders: list[RebalanceOrder],
        dry_run: bool = False,
        *,
        live_confirmed: bool = False,
    ) -> dict:
        """
        리밸런싱 주문 실행.

        Args:
            orders: plan_rebalance()가 반환한 주문 목록
            dry_run: True이면 실제 주문 없이 로그만 출력
            live_confirmed: live 주문 경로의 운영자 확인/live gate 통과 여부

        Returns:
            {"executed": int, "skipped": int, "failed": int, "details": list}
        """
        results = {"executed": 0, "skipped": 0, "failed": 0, "details": []}
        mode = str(self.config.trading.get("mode", "paper")).lower()
        if mode == "live" and not dry_run and not live_confirmed and orders:
            reason = "live 리밸런싱은 운영자 확인 및 live gate 통과 후만 실행 가능합니다."
            logger.error(reason)
            results["failed"] = len(orders)
            results["blocked"] = True
            results["reason"] = reason
            results["details"] = [
                {"order": repr(order), "status": "blocked", "reason": reason}
                for order in orders
            ]
            return results
        if mode == "live" and not dry_run and orders:
            if not self.account_key or self.account_key != self.execution_strategy:
                reason = (
                    "live 리밸런싱 승인 단위 불일치: "
                    f"account_key={self.account_key!r}, strategy={self.execution_strategy!r}"
                )
                logger.error(reason)
                results["failed"] = len(orders)
                results["blocked"] = True
                results["reason"] = reason
                results["details"] = [
                    {"order": repr(order), "status": "blocked", "reason": reason}
                    for order in orders
                ]
                return results

        executor = None
        snapshot: dict[str, dict] = {}
        if not dry_run:
            from core.order_executor import OrderExecutor

            executor = OrderExecutor(
                self.config,
                account_key=self.account_key,
                live_gate_validated=live_confirmed,
            )
            # 유동성 체크용 20일 평균 거래량 — plan 단계 캐시 재사용, 없으면 새로 조회.
            snapshot = getattr(self, "_market_snapshot", None) or self._fetch_market_snapshot()

        for order in orders:
            if dry_run:
                logger.info("[DRY RUN] {} {} {}주 @{:,.0f} — {}",
                            order.action, order.symbol, order.quantity, order.price, order.reason)
                results["skipped"] += 1
                results["details"].append({"order": repr(order), "status": "dry_run"})
                continue

            try:
                if order.action == "BUY":
                    available_cash = self.portfolio_mgr.get_available_cash()
                    total_value = self.portfolio_mgr.get_current_capital()
                    res = executor.execute_buy_quantity(
                        symbol=order.symbol,
                        price=order.price,
                        quantity=order.quantity,
                        capital=total_value,
                        available_cash=available_cash,
                        reason=f"리밸런싱: {order.reason}",
                        strategy=self.execution_strategy,
                        avg_daily_volume=snapshot.get(order.symbol, {}).get("avg_volume"),
                    )
                else:
                    res = executor.execute_sell(
                        symbol=order.symbol, price=order.price, quantity=order.quantity,
                        reason=f"리밸런싱: {order.reason}", strategy=self.execution_strategy,
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

    @staticmethod
    def _recent_range(calendar_days: int) -> tuple[str, str]:
        """오늘 기준 과거 calendar_days일 ~ 오늘의 (start, end) 날짜 문자열.

        fetch_korean_stock는 days 인자가 없고 start_date/end_date만 받는다.
        거래일이 아닌 달력일 기준이므로 필요한 거래일보다 넉넉히 잡는다.
        """
        end = datetime.now()
        start = end - timedelta(days=calendar_days)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def _fetch_market_snapshot(self) -> dict[str, dict]:
        """바스켓 종목의 현재가 + 20일 평균 거래량 일괄 조회.

        평균 거래량은 paper/live 매수의 유동성 체크(_entry_liquidity_check)에 전달한다
        — 데이터 없이 주문하면 strict 유동성 필터가 fail-closed로 차단한다.
        결과는 인스턴스에 캐시해 plan→execute 한 사이클 내 중복 조회를 피한다.
        """
        snapshot: dict[str, dict] = {}
        # 거래일 20개(평균 거래량 산정분) 커버 위해 달력 45일 범위로 조회.
        start, end = self._recent_range(45)
        for symbol in self.holdings:
            try:
                df = self.data_collector.fetch_korean_stock(symbol, start, end)
                if df is None or df.empty:
                    continue
                entry = {"price": float(df["close"].iloc[-1])}
                if "volume" in df.columns:
                    recent_vol = df["volume"].tail(20).dropna()
                    if len(recent_vol) > 0 and float(recent_vol.mean()) > 0:
                        entry["avg_volume"] = float(recent_vol.mean())
                snapshot[symbol] = entry
            except Exception as e:
                logger.warning("시세 조회 실패 {}: {}", symbol, e)
        self._market_snapshot = snapshot
        return snapshot

    def _fetch_current_prices(self) -> dict[str, float]:
        """바스켓 종목의 현재가를 일괄 조회."""
        return {s: v["price"] for s, v in self._fetch_market_snapshot().items()}

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
        # 지표·신호 계산용으로 약 120거래일 커버 위해 달력 200일 범위 조회.
        start, end = self._recent_range(200)
        for symbol, weight in base.items():
            try:
                df = self.data_collector.fetch_korean_stock(symbol, start, end)
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

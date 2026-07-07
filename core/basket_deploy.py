"""바스켓 배포 헬퍼 — 운영자가 buy&hold 바스켓을 한눈에 점검하고 활성화 절차를 받는다.

수익성 결론(능동 alpha 없음 → 분산 보유가 현실적 고수익)을 실제로 굴리는 마지막 단계는
운영자의 검증·활성화다. 이 모듈은 그 마찰을 줄인다:
  - 계획된 주문 + 예상 거래비용(수수료/세금) + 회전율을 요약
  - enabled 여부와 다음 활성화 절차를 명확히 안내
순수 함수라(주입된 orders/prices/config로만 계산) 단위 테스트가 쉽다.
"""

from __future__ import annotations

from typing import Any

# config/risk_params.yaml 기본값과 동일 (편도 수수료, 매도세)
DEFAULT_COMMISSION_RATE = 0.00015   # 0.015% 양방향
DEFAULT_TAX_RATE = 0.0020           # 0.20% 매도만

DEFAULT_MIN_CASH_RATIO = 0.20       # diversification.min_cash_ratio 기본값과 동일


def effective_stock_fraction(basket_cfg: dict[str, Any], risk_params: dict[str, Any]) -> float:
    """바스켓의 유효 투자 비중(총자산 대비). 리밸런서·평가·헬스가 같은 규칙을 써야
    '설계 비중'이 서로 어긋나지 않으므로 여기 한 곳에 둔다.

    - target_stock_weight: 바스켓이 명시한 투자 목표 비중. 미지정이면 (1 - 현금 하한).
    - min_cash_ratio: 전역(diversification.min_cash_ratio, 기본 20%)이 하한이지만
      바스켓별로 오버라이드할 수 있다. 전역 20%는 개별 주식 바스켓의 안전판인데,
      보유분 절반이 현금성(CD금리 파킹 ETF)인 바스켓에는 과잉이라 유휴 현금만 남긴다.
    """
    div_cfg = (risk_params or {}).get("diversification", {}) or {}
    mcr_raw = basket_cfg.get("min_cash_ratio", div_cfg.get("min_cash_ratio", DEFAULT_MIN_CASH_RATIO))
    try:
        mcr = float(mcr_raw)
    except (TypeError, ValueError):
        mcr = float(div_cfg.get("min_cash_ratio", DEFAULT_MIN_CASH_RATIO))
    mcr = max(0.0, min(1.0, mcr))
    max_stock = 1.0 - mcr
    tsw = basket_cfg.get("target_stock_weight")
    if tsw is None:
        return max_stock
    return max(0.0, min(float(tsw), max_stock))


def estimate_order_costs(
    orders: list[Any],
    prices: dict[str, float],
    *,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    tax_rate: float = DEFAULT_TAX_RATE,
) -> dict[str, Any]:
    """주문 목록의 예상 거래비용(수수료+세금)과 총 거래액을 추정한다.

    orders는 .action('BUY'/'SELL'), .symbol, .quantity 속성을 가진 객체.
    """
    buy_amount = 0.0
    sell_amount = 0.0
    for o in orders:
        price = float(prices.get(o.symbol, 0) or 0)
        amount = price * int(o.quantity)
        if str(o.action).upper() == "BUY":
            buy_amount += amount
        else:
            sell_amount += amount

    total_amount = buy_amount + sell_amount
    commission = total_amount * commission_rate   # 매수·매도 양방향 수수료
    tax = sell_amount * tax_rate                   # 세금은 매도만
    total_cost = commission + tax
    return {
        "order_count": len(orders),
        "buy_amount": round(buy_amount, 0),
        "sell_amount": round(sell_amount, 0),
        "total_trade_amount": round(total_amount, 0),
        "est_commission": round(commission, 0),
        "est_tax": round(tax, 0),
        "est_total_cost": round(total_cost, 0),
        "cost_bps_of_trade": round(total_cost / total_amount * 10000, 1) if total_amount > 0 else 0.0,
    }


def summarize_basket_deployment(
    basket_name: str,
    basket_cfg: dict[str, Any],
    orders: list[Any],
    prices: dict[str, float],
    *,
    portfolio_value: float | None = None,
) -> dict[str, Any]:
    """바스켓 배포 점검 요약 — 계획·비용·회전율·활성화 절차.

    반환: {basket, enabled, holdings_count, plan, costs, turnover_pct, ready, next_steps}.
    """
    enabled = bool(basket_cfg.get("enabled", False))
    holdings = basket_cfg.get("holdings", {})
    rb = basket_cfg.get("rebalance", {}) or {}
    costs = estimate_order_costs(orders, prices)

    turnover_pct = None
    if portfolio_value and portfolio_value > 0:
        turnover_pct = round(costs["total_trade_amount"] / portfolio_value * 100, 1)

    plan = [
        {
            "action": str(o.action).upper(),
            "symbol": o.symbol,
            "quantity": int(o.quantity),
            "price": float(prices.get(o.symbol, 0) or 0),
            "amount": round(float(prices.get(o.symbol, 0) or 0) * int(o.quantity), 0),
        }
        for o in orders
    ]

    # 다음 단계 안내 (바스켓 정상 경로: dry-run 검증 → 활성화 → 바스켓 live gate)
    next_steps: list[str] = []
    if not prices or len(prices) < len(holdings):
        next_steps.append(
            "가격 조회 일부 실패 — 거래일/데이터 소스를 확인하거나 과거 거래일로 점검하세요."
        )
    if not enabled:
        next_steps.append(
            f"config/baskets.yaml의 '{basket_name}' enabled를 true로 바꿔야 실행됩니다 "
            "(paper로 며칠 dry-run 관찰 후 권장)."
        )
    next_steps.append(
        f"paper 검증: main.py --mode rebalance --basket {basket_name} --dry-run"
    )
    next_steps.append(
        "live 승격: 바스켓 live gate(basket_rebalance:<name>) + ENABLE_LIVE_TRADING=true + --confirm-live"
    )

    # ready = 가격 확보 + 비중합 정상(주문 계획 산출 가능). 활성화는 운영자 결정이므로 별도 표기.
    weights_ok = abs(sum(float(w) for w in holdings.values()) - 1.0) < 1e-6 if holdings else False
    prices_ok = bool(prices) and len(prices) >= len(holdings)
    ready_to_validate = bool(weights_ok and prices_ok)

    return {
        "basket": basket_name,
        "display_name": basket_cfg.get("name", basket_name),
        "enabled": enabled,
        "holdings_count": len(holdings),
        "weights_sum_ok": weights_ok,
        "rebalance_trigger": rb.get("trigger", "drift"),
        "drift_threshold_pct": round(float(rb.get("drift_threshold", 0)) * 100, 1),
        "max_turnover_ratio_pct": round(float(rb.get("max_turnover_ratio", 0)) * 100, 1),
        "plan": plan,
        "costs": costs,
        "turnover_pct_of_portfolio": turnover_pct,
        "ready_to_validate": ready_to_validate,
        "next_steps": next_steps,
    }

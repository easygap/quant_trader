"""자본 규모별 바스켓 슬롯 채움 시뮬레이터 — 자본 결정(#422 A안) 지원 도구.

배경(docs/PAPER_MONTH1_REVIEW_AND_PLAN.md §3): 자본이 작으면 고가 종목 슬롯이
정수 주식 절사로 못 채워진다(예: 자본 1,000만 → 하이닉스 슬롯 80만 < 1주 2백만대).
문서의 자본별 표는 작성 시점 시세로 고정이라, 결정 시점의 **실시간 시세**로
재계산해주는 읽기 전용 도구다. 주문·DB 변경 없음.

사용:
    .venv\\Scripts\\python.exe tools/capital_slot_simulator.py                     # 기본 스윕
    .venv\\Scripts\\python.exe tools/capital_slot_simulator.py --capital 34000000  # 단일 자본 상세
    .venv\\Scripts\\python.exe tools/capital_slot_simulator.py --basket kr_diversified_hold --capitals 30000000,34000000,62000000
"""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def simulate_slot_fills(
    holdings: dict[str, float],
    capital: float,
    stock_fraction: float,
    prices: dict[str, float],
    min_trade_amount: float = 0.0,
) -> dict[str, Any]:
    """자본 capital에서 각 슬롯의 채움율을 계산한다(순수 함수).

    슬롯 금액 = capital × stock_fraction × (목표비중/비중합). 채움 주수 = floor(슬롯/가격).
    1주도 못 사거나 슬롯이 min_trade_amount 미만이면 미체결(unfillable)로 표시한다
    — BasketRebalancer의 매수 판정과 같은 기준. 가격이 없거나 0 이하인 종목은
    판정 불가(unknown)로 분리한다(미체결로 오표기 방지).

    반환: {"slots": [{symbol, weight, slot_amount, price, shares, fill_amount,
           fill_ratio, unfillable, reason}...], "deployment_ratio",  # 총자산 대비
           "design_fraction", "unfillable_count", "unknown_count"}
    """
    total_w = sum(float(w) for w in holdings.values())
    slots: list[dict[str, Any]] = []
    filled_total = 0.0
    unfillable = 0
    unknown = 0

    for symbol, weight in holdings.items():
        wn = (float(weight) / total_w) if total_w > 0 else 0.0
        slot_amount = float(capital) * float(stock_fraction) * wn
        price = float(prices.get(symbol) or 0)
        row: dict[str, Any] = {
            "symbol": symbol, "weight": wn, "slot_amount": slot_amount,
            "price": price, "shares": 0, "fill_amount": 0.0, "fill_ratio": 0.0,
            "unfillable": False, "reason": "",
        }
        if price <= 0:
            row["reason"] = "가격 조회 불가"
            unknown += 1
        else:
            shares = int(slot_amount // price)
            if slot_amount < min_trade_amount:
                row["unfillable"] = True
                row["reason"] = f"슬롯 {slot_amount:,.0f} < 최소거래 {min_trade_amount:,.0f}"
                unfillable += 1
            elif shares <= 0:
                row["unfillable"] = True
                row["reason"] = f"1주 {price:,.0f} > 슬롯 {slot_amount:,.0f}"
                unfillable += 1
            else:
                row["shares"] = shares
                row["fill_amount"] = shares * price
                row["fill_ratio"] = (shares * price) / slot_amount if slot_amount > 0 else 0.0
                filled_total += shares * price
        slots.append(row)

    return {
        "slots": slots,
        "deployment_ratio": (filled_total / capital) if capital > 0 else 0.0,
        "design_fraction": float(stock_fraction),
        "unfillable_count": unfillable,
        "unknown_count": unknown,
    }


def format_simulation(basket_name: str, capital: float, result: dict[str, Any]) -> str:
    """시뮬레이션 결과를 사람이 읽는 표로 포맷한다(순수 함수)."""
    lines = [
        "=" * 72,
        f"  자본 {capital:,.0f}원 — 바스켓 '{basket_name}' 슬롯 채움 시뮬레이션",
        "=" * 72,
        f"  {'종목':<8} {'비중':>5} {'슬롯금액':>12} {'1주가격':>12} {'주수':>4} {'채움율':>7}  비고",
    ]
    for s in result["slots"]:
        mark = "❌" if s["unfillable"] else ("❓" if s["price"] <= 0 else "  ")
        lines.append(
            f"  {s['symbol']:<8} {s['weight']:>5.0%} {s['slot_amount']:>12,.0f} "
            f"{s['price']:>12,.0f} {s['shares']:>4} {s['fill_ratio']:>7.1%} {mark}{s['reason']}"
        )
    dep = result["deployment_ratio"]
    design = result["design_fraction"]
    lines.append("-" * 72)
    lines.append(
        f"  총 배치율 {dep:.1%} / 설계 {design:.0%} ({(dep - design) * 100:+.1f}%p) · "
        f"미체결 {result['unfillable_count']}개"
        + (f" · 가격불명 {result['unknown_count']}개" if result["unknown_count"] else "")
    )
    if result["unfillable_count"] == 0 and result["unknown_count"] == 0:
        lines.append("  → 이 자본이면 전 슬롯 매수 가능 (절사 잔여만 남음)")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="자본 규모별 바스켓 슬롯 채움 시뮬레이터 (읽기 전용)")
    parser.add_argument("--basket", type=str, default=None,
                        help="대상 바스켓 (미지정 시 enabled 바스켓, 정확히 1개여야 함)")
    parser.add_argument("--capital", type=float, default=None, help="단일 자본 금액")
    parser.add_argument(
        "--capitals", type=str,
        default="10000000,30000000,32000000,34000000,40000000,62000000",
        help="쉼표 구분 자본 스윕 (기본: 문서 §3 표의 구간)",
    )
    args = parser.parse_args()

    from config.config_loader import Config
    from core.basket_rebalancer import BasketRebalancer

    config = Config.get()
    basket_name = args.basket
    if not basket_name:
        enabled = BasketRebalancer.get_enabled_baskets()
        if len(enabled) != 1:
            logger.error("대상 바스켓이 모호합니다 (enabled={}) — --basket으로 지정하세요.", enabled)
            return 1
        basket_name = enabled[0]

    basket_cfg = BasketRebalancer._load_baskets_config().get(basket_name)
    if not basket_cfg:
        logger.error("바스켓 '{}' 설정 없음 (config/baskets.yaml)", basket_name)
        return 1
    holdings = basket_cfg.get("holdings") or {}
    min_trade = float((basket_cfg.get("rebalance") or {}).get("min_trade_amount", 0) or 0)

    # 실제 사이클과 같은 단일 규칙(바스켓별 min_cash_ratio 오버라이드 포함) — 여기만
    # 다르면 '이 자본으로 슬롯이 차는가'라는 이 도구의 답 자체가 틀린다.
    from core.basket_deploy import effective_stock_fraction
    stock_fraction = effective_stock_fraction(basket_cfg, config.risk_params)

    # 현재가 조회 (읽기 전용) — 실제 사이클과 동일 소스(BasketRebalancer 시세 경로) 재사용.
    rebalancer = BasketRebalancer(basket_name=basket_name, config=config)
    prices: dict[str, float] = rebalancer._fetch_current_prices()
    missing = [s for s in holdings if s not in prices]
    if missing:
        logger.warning("가격 조회 실패 종목: {} — 해당 슬롯은 '가격 조회 불가'로 표시", missing)

    capitals = (
        [float(args.capital)] if args.capital
        else [float(c) for c in args.capitals.split(",") if c.strip()]
    )
    for capital in capitals:
        result = simulate_slot_fills(holdings, capital, stock_fraction, prices, min_trade)
        print(format_simulation(basket_name, capital, result))

    print(
        "\n참고: 실효 배치율은 여기서 계산한 값에서 리밸런싱 회전 상한(1일 15%)에 따라\n"
        "수 거래일에 걸쳐 수렴한다. 자본 결정 후 절차는 docs/PAPER_MONTH1_REVIEW_AND_PLAN.md §3."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

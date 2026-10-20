"""소액 ETF 계좌의 정수 주 배분 — 종목마다 따로 내림하지 않고 수량 조합을 함께 고른다.

문제(2026-10-13 점검): 종목별로 '목표 금액 − 보유 금액'을 1주 가격으로 내림하면, 부족분과
초과분이 둘 다 1주보다 작을 때 아무 주문도 나오지 않는다. 10/12 적립 계좌는 주식 ETF
26.7%·CD ETF 61.2%(목표 47.5%·47.5%)였는데 계획이 0건이었다. CD ETF 2주를 팔아 주식
ETF 1주를 사면 주식이 53.5%가 돼 목표와의 차이가 20.8%p에서 6.0%p로 줄어든다.

방법: 종목마다 후보 수량 {현재, 목표 내림, 목표 올림, 목표 내림 − 1}을 두고 모든 조합을
평가한다(종목 수가 적은 계좌 전용). 현금 하한·최소 거래금액·1회 회전 한도·재매수 차단을
지키는 조합 중에서 '위험자산 비중 이탈 제곱합 + 놀고 있는 현금 + 거래비용'이 가장 작은
것을 고른다.

- 방어 자산(CD 파킹 ETF)과 현금은 위험이 거의 같다. 둘 사이의 비중 차이는 위험이 아니라
  이자 차이라서, 방어 자산 자체의 이탈은 세지 않고 목표를 넘는 현금만 낮은 가중치로 센다.
  그래야 CD를 팔아 현금으로 바꾸는(비용·세금만 드는) 거래를 하지 않고, 남는 현금은 CD로
  옮긴다. CD를 파는 것은 주식 비중을 맞출 돈이 필요할 때뿐이다.
- 거래비용(수수료·슬리피지·세금 추정)을 손실에 더해, 개선이 비용보다 작으면 거래하지
  않는다. 지금 조합이 가장 나으면 주문이 없다.
- 새 적립금(현금)은 매도 없이 부족한 자산부터 산다 — 매도는 비용과 세금이 들기 때문에
  같은 개선이면 매수만 하는 조합이 이긴다.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

DEFAULT_IDLE_CASH_WEIGHT = 0.25
MAX_SYMBOLS = 5


@dataclass
class IntegerPlan:
    quantities: dict[str, int]
    trades: dict[str, int]                 # 종목 → 수량 변화(+매수/−매도)
    loss_before: float
    loss_after: float
    turnover: float                        # 거래 명목금액 합(원)
    cash_after: float
    weights_before: dict[str, float] = field(default_factory=dict)
    weights_after: dict[str, float] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return any(self.trades.values())


def _finite_positive(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _candidates(current: int, target_qty: float, can_buy: bool) -> list[int]:
    low = max(0, math.floor(target_qty))
    options = {current, low, max(0, low - 1), max(0, math.ceil(target_qty))}
    if not can_buy:
        options = {q for q in options if q <= current}
    return sorted(options)


def choose_integer_holdings(
    *,
    total_value: float,
    cash: float,
    prices: dict[str, float],
    current: dict[str, int],
    targets: dict[str, float],
    min_cash_ratio: float,
    min_trade: float,
    max_turnover: float,
    unit_costs: dict[str, dict[str, float]] | None = None,
    default_cost_rate: float = 0.001,
    defensive_symbol: str | None = None,
    idle_cash_weight: float = DEFAULT_IDLE_CASH_WEIGHT,
    blocked_buys: set[str] | frozenset[str] = frozenset(),
    max_symbols: int = MAX_SYMBOLS,
) -> IntegerPlan | None:
    """총자산 대비 목표 비중(targets)에 가장 가까운 정수 수량 조합.

    unit_costs: {종목: {"buy": 1주 매수 비용, "sell": 1주 매도 비용}} (원, 체결가 대비).
    없으면 default_cost_rate × 가격으로 추정한다.
    defensive_symbol: 현금과 위험이 비슷한 방어 자산. 그 비중 이탈은 세지 않는다.
    적용할 수 없는 입력(종목 수 초과, 가격·자산 누락)이면 None — 호출부는 기존 계획기를 쓴다.
    """
    symbols = sorted(targets)
    if not symbols or len(symbols) > max_symbols:
        return None
    if not _finite_positive(total_value) or not math.isfinite(float(cash)):
        return None
    if any(not _finite_positive(prices.get(s)) for s in symbols):
        return None
    if any((not math.isfinite(float(w))) or w < 0 for w in targets.values()):
        return None
    total = float(total_value)
    reserve = max(0.0, float(min_cash_ratio)) * total
    budget = max(0.0, float(max_turnover)) * total
    costs = unit_costs or {}

    def unit(symbol: str, side: str) -> float:
        raw = (costs.get(symbol) or {}).get(side)
        if raw is not None and math.isfinite(float(raw)) and float(raw) >= 0:
            return float(raw)
        return float(prices[symbol]) * default_cost_rate

    cash_target = max(0.0, 1.0 - sum(float(w) for w in targets.values()))
    risky = [s for s in symbols if s != defensive_symbol]

    def deviation(quantities: dict[str, int], cash_left: float) -> float:
        risky_gap = sum(
            (quantities[s] * float(prices[s]) / total - float(targets[s])) ** 2 for s in risky
        )
        idle = max(0.0, cash_left / total - cash_target)
        return risky_gap + idle_cash_weight * idle ** 2

    now = {s: max(0, int(current.get(s, 0) or 0)) for s in symbols}
    options = [
        _candidates(now[s], float(targets[s]) * total / float(prices[s]), s not in blocked_buys)
        for s in symbols
    ]
    loss_before = deviation(now, float(cash))
    best = None
    for combo in itertools.product(*options):
        quantities = dict(zip(symbols, combo))
        cash_after = float(cash)
        turnover = 0.0
        trade_cost = 0.0
        feasible = True
        for s in symbols:
            delta = quantities[s] - now[s]
            if delta == 0:
                continue
            notional = abs(delta) * float(prices[s])
            if notional < min_trade:
                feasible = False
                break
            turnover += notional
            if delta > 0:
                cost = delta * unit(s, "buy")
                cash_after -= notional + cost
            else:
                cost = -delta * unit(s, "sell")
                cash_after += notional - cost
            trade_cost += cost
        if not feasible or turnover > budget + 1e-6:
            continue
        # 현금 하한: 지금보다 현금을 줄이는 조합은 하한을 지켜야 한다(이미 하한 아래면
        # 더 줄이지 않는 조합만 허용 — 매도만 하는 조합은 언제나 가능).
        if cash_after < min(reserve, float(cash)) - 1e-6:
            continue
        loss = deviation(quantities, cash_after) + trade_cost / total
        key = (round(loss, 12), turnover)
        if best is None or key < best[0]:
            best = (key, quantities, turnover, cash_after)

    if best is None:
        return None
    _key, quantities, turnover, cash_after = best
    loss_after = deviation(quantities, cash_after)
    trades = {s: quantities[s] - now[s] for s in symbols}
    if any(trades.values()) and not loss_after < loss_before:
        # 비용만 들고 목표에 가까워지지 않는 조합은 택하지 않는다(위의 비용 항으로 이미
        # 걸러지지만 반올림 경계에서 확실히 막는다).
        quantities, trades, turnover, cash_after, loss_after = dict(now), {s: 0 for s in symbols}, 0.0, float(cash), loss_before
    return IntegerPlan(
        quantities=quantities,
        trades=trades,
        loss_before=loss_before,
        loss_after=loss_after,
        turnover=turnover,
        cash_after=cash_after,
        weights_before={s: now[s] * float(prices[s]) / total for s in symbols},
        weights_after={s: quantities[s] * float(prices[s]) / total for s in symbols},
    )

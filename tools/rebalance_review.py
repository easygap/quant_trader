"""현재 주문 계획과 지정한 커밋의 계획을 같은 ETF 가격으로 비교한다.

실제 계좌·DB·주문 실행부에 연결하지 않는다. 전일 종가로 수량을 정하고 다음 거래일
종가에 비용을 더해 가상 체결한다. 모의 운용이나 실전 수익을 대신하는 검증은 아니다.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import basket_rebalancer as current
from core.risk_manager import RiskManager
from core.risk_overlays import compute_decision, parse_overlay_config
from tools import risk_overlay_backtest as research
from tools.risk_review import align_report_inputs, period_metrics


def replay(module, panel, basket, *, cost_multiple=1.0, risk_params=None):
    """주문 계획만 실제 코드를 사용한다. 체결과 비용은 아래의 단순 모형이다."""
    symbols = list(basket["holdings"])
    cfg = parse_overlay_config(basket)
    if set(symbols) != {"069500", "357870"} or cfg.volatility.enabled:
        raise ValueError("이 비교는 변동성 목표를 끈 KODEX 200·CD ETF 설정만 지원합니다")
    if basket["rebalance"].get("trigger") != "drift":
        raise ValueError("비중 이탈 기준의 주문 계획만 비교합니다")
    if not math.isfinite(cost_multiple) or cost_multiple < 0:
        raise ValueError("비용 배수는 유한한 0 이상의 값이어야 합니다")
    complete_etf = panel[symbols].dropna()
    if panel.empty or complete_etf.empty:
        raise ValueError("비교할 ETF 가격이 없습니다")
    first_etf_bar = complete_etf.index[0]
    checked_series = [panel.KS200, *(panel[s].loc[first_etf_bar:] for s in symbols)]
    if (not panel.index.is_unique or not panel.index.is_monotonic_increasing
            or not all(math.isfinite(v) and v > 0 for values in checked_series for v in values)):
        raise ValueError("비교할 가격과 날짜를 확인하세요")
    # ETF 상장 전의 지수 가격도 이동평균에 쓰되, 두 ETF의 전일 종가가 있어야 주문한다.
    start = max(200, cfg.trend.ma_days, panel.index.get_loc(first_etf_bar) + 1)
    if len(panel) <= start:
        raise ValueError("이동평균 준비 기간 이후의 가격이 필요합니다")

    initial = float(basket["initial_capital"])
    monthly = float(basket["contribution_plan"]["amount"])
    quantity = dict.fromkeys(symbols, 0)
    avg = dict.fromkeys(symbols, 0.0)
    cash = contributed = previous_total = initial
    twr = peak = 1.0
    previous_month = previous_state = None
    model_risk = copy.deepcopy(risk_params if risk_params is not None else {
        "transaction_costs": {
            "commission_rate": research.COMMISSION, "slippage": research.SLIPPAGE,
            "slippage_ticks": 0, "tax_exempt_symbols": symbols,
            "holding_period_income_tax": {"enabled": True, "rate": 0.154, "symbols": ["357870"]},
        },
    })
    costs_cfg = model_risk.setdefault("transaction_costs", {})
    for name, default in (("commission_rate", 0.00015), ("slippage", 0.0005), ("slippage_ticks", 2)):
        costs_cfg[name] = costs_cfg.get(name, default) * cost_multiple
    costs_model = RiskManager(types.SimpleNamespace(risk_params=model_risk))

    # 생성자를 부르지 않아 PortfolioManager와 DataCollector도 만들지 않는다.
    rb = module.BasketRebalancer.__new__(module.BasketRebalancer)
    rb.basket = copy.deepcopy(basket)
    rb.basket_name = "kr_pocket"
    rb.holdings = dict(basket["holdings"])
    rb.rebalance_cfg = dict(basket["rebalance"])
    rb.account_key = rb.execution_strategy = "research:kr_pocket"
    rb.config = types.SimpleNamespace(trading={"mode": "paper"})
    rb._risk_params = copy.deepcopy(model_risk)
    rb._risk_params.setdefault("diversification", {})["min_cash_ratio"] = basket["min_cash_ratio"]
    rb._target_stock_weight = basket["target_stock_weight"]

    def positions(**_):
        return [types.SimpleNamespace(symbol=s, quantity=q, avg_price=avg[s])
                for s, q in quantity.items() if q]

    def summary(current_prices):
        return {"total_value": cash + sum(quantity[s] * current_prices[s] for s in symbols),
                "cash": cash}

    rb.portfolio_mgr = types.SimpleNamespace(get_portfolio_summary=summary)
    rows = []
    with patch.object(module, "get_all_positions", positions), patch.object(
        module, "symbols_in_reentry_cooldown", lambda *a, **kw: {},
    ):
        for i in range(start, len(panel)):
            day = panel.index[i]
            month = (day.year, day.month)
            flow = monthly if previous_month is not None and month != previous_month else 0
            cash += flow
            contributed += flow
            previous_month = month
            decision = compute_decision(
                cfg, index_closes=panel.KS200.iloc[i - cfg.trend.ma_days:i].tolist(),
                cumulative_returns_pct=[(peak - 1) * 100, (twr - 1) * 100],
                prev_state=previous_state, now=day.to_pydatetime(),
            )
            previous_state = decision.to_dict()
            rb.overlay_decision = lambda: decision
            # 주문 수량에는 체결일 가격을 주지 않는다.
            signal_prices = {s: float(panel[s].iloc[i - 1]) for s in symbols}
            need, _ = rb.should_rebalance(signal_prices)
            orders = rb.plan_rebalance(signal_prices) if need else []
            prices = {s: float(panel[s].iloc[i]) for s in symbols}
            turnover = fees = slip_cost = 0.0
            filled = rejected = 0
            for order in orders:
                s, qty = order.symbol, order.quantity
                buy = order.action == "BUY"
                costs = costs_model.calculate_transaction_costs(
                    prices[s], qty, order.action, symbol=s, avg_price=avg[s] or None,
                )
                fill = costs["execution_price"]
                notional = fill * qty
                fee = costs["commission"] + costs["tax"] + costs["capital_gains_tax"]
                if buy:
                    total_now = cash + sum(quantity[k] * prices[k] for k in symbols)
                    limits = rb._policy_exposure_limits(s)
                    invested = total_now - cash
                    # 다음 날 가격이 바뀌어 감당할 수 없어진 주문은 전량 보류한다.
                    if (cash - notional - fee < total_now * limits["min_cash_ratio"]
                            or invested + notional > total_now * limits["max_investment_ratio"]
                            or quantity[s] * prices[s] + notional
                            > total_now * limits["max_position_ratio"]):
                        rejected += 1
                        continue
                    avg[s] = (avg[s] * quantity[s] + notional) / (quantity[s] + qty)
                    quantity[s] += qty
                    cash -= notional + fee
                else:
                    if qty > quantity[s]:
                        raise AssertionError("보유 수량보다 큰 매도 계획")
                    quantity[s] -= qty
                    cash += notional - fee
                filled += 1
                turnover += notional
                fees += fee
                slip_cost += costs["slippage"]
            total = cash + sum(quantity[s] * prices[s] for s in symbols)
            daily_return = total / (previous_total + flow) - 1
            twr *= 1 + daily_return
            peak = max(peak, twr)
            previous_total = total
            rows.append({
                "date": day, "total": total, "twr": twr, "drawdown": twr / peak - 1,
                "daily_return": daily_return, "flow": flow, "contributed": contributed,
                "cash": cash, "stock_w": quantity["069500"] * prices["069500"] / total,
                "trade_value": turnover, "cost": fees + slip_cost, "fees_and_tax": fees,
                "slippage_cost": slip_cost, "planned_orders": len(orders),
                "filled_orders": filled, "rejected_orders": rejected,
                **{f"qty_{s}": quantity[s] for s in symbols},
            })
    return pd.DataFrame(rows).set_index("date")


def summarize(frame):
    result = research.metrics(frame, {
        "turnover_value": frame.trade_value.sum(), "avg_exposure": frame.stock_w.mean(),
    })
    result.update({key: int(frame[key].sum()) for key in
                   ("planned_orders", "filled_orders", "rejected_orders")})
    result.update({key: round(float(frame[key].sum()), 2) for key in
                   ("trade_value", "cost", "fees_and_tax", "slippage_cost")})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="비교할 로컬 Git 커밋")
    parser.add_argument("--as-of", default="2026-09-22")
    parser.add_argument("--output")
    args = parser.parse_args()
    baseline_sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{args.baseline}^{{commit}}"], cwd=ROOT,
    ).decode().strip()
    before = types.ModuleType("rebalance_before")
    before.__file__ = str(ROOT / "core/basket_rebalancer.py")
    source = subprocess.check_output(
        ["git", "show", f"{baseline_sha}:core/basket_rebalancer.py"], cwd=ROOT,
    ).decode("utf-8")
    exec(compile(source, before.__file__, "exec"), before.__dict__)  # noqa: S102
    logger.disable(current.__name__)
    logger.disable(before.__name__)
    logger.disable("core.risk_manager")
    basket = yaml.safe_load((ROOT / "config/baskets.yaml").read_text(encoding="utf-8"))["baskets"]["kr_pocket"]
    risk_params = yaml.safe_load((ROOT / "config/risk_params.yaml").read_text(encoding="utf-8"))
    research.AS_OF = args.as_of
    series = {s: research._fdr(s, "2014-01-01") for s in ("069500", "357870", "KS200")}
    _, panel, audit = align_report_inputs(series, args.as_of)
    payload = {
        "as_of": args.as_of, "baseline_commit": baseline_sha,
        "planner_sha256": hashlib.sha256((ROOT / "core/basket_rebalancer.py").read_bytes()).hexdigest(),
        "cost_model_sha256": hashlib.sha256((ROOT / "core/risk_manager.py").read_bytes()).hexdigest(),
        "data_audit": audit, "input_sha256": hashlib.sha256(panel.to_csv().encode()).hexdigest(),
        "basket": basket, "cost_config": risk_params["transaction_costs"],
        "instrument_classes": risk_params["instrument_classes"], "comparisons": {},
        "limitations": [
            "같은 과거 자료를 다시 사용한 사후 비교이며 향후 수익률 검증이 아님",
            "전일 종가로 주문 수량 결정, 다음 거래일 종가에 수수료·슬리피지를 반영한 체결 근사",
            "분배금·실제 호가·유동성·부분체결 미반영, 실제 주문 실행부를 호출하지 않음",
            "현금 이자 0%, CD ETF 양의 매매차익에 15.4% 과세 상한 근사",
            "매수 시 시장가 기준 현금·비중 한도를 확인하는 별도 모형, 운영 엔진 전체의 재현이 아님",
            "3배 비용은 수수료·슬리피지만 늘리고 세율과 매매 설정은 유지",
            "주문 계획 차이를 보기 위해 수정 전후 모두 ETF 호가를 고친 현재 비용 계산 사용",
        ],
    }
    for multiple in (1, 3):
        comparison = {}
        for label, module in (("before", before), ("after", current)):
            frame = replay(module, panel, basket, cost_multiple=multiple, risk_params=risk_params)
            comparison[label] = {"all": summarize(frame), "periods": {
                period: period_metrics(frame, a, b) for period, a, b in (
                    ("2020_2022", "2020-01-01", "2022-12-31"),
                    ("2023_2025", "2023-01-01", "2025-12-31"),
                    ("2026", "2026-01-01", args.as_of),
                )
            }}
            payload["start"] = str(frame.index[0].date())
            payload["end"] = str(frame.index[-1].date())
        payload["comparisons"][f"cost_{multiple}x"] = comparison
        print(json.dumps({f"cost_{multiple}x": comparison}, ensure_ascii=False), flush=True)
    path = Path(args.output or f"reports/research/rebalance_review_{pd.Timestamp(args.as_of):%Y%m%d}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()

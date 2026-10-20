#!/usr/bin/env python3
"""대형주 분산 계좌의 손절·계좌 낙폭 매수 차단 조합을 과거 자료로 비교한다.

운영 계좌(kr_diversified_hold)에는 세 장치가 함께 걸려 있다.

1. 종목별 손절: 평균 매입가 대비 -25%면 전량 매도
2. 손절 종목 60일 재매수 차단
3. 계좌 낙폭 15% 이상이면 모든 신규 매수 차단(전역 risk_params)

각각은 따로 검토된 적이 있지만 셋이 겹친 결과는 시험한 적이 없다. 2026년 10월에는
시장 전체가 내리자 손절이 연달아 나갔고(10/15 삼성전자·현대차), 그 매도 대금은 3번 때문에
다시 들어가지 못해 주식 비중이 설계 60%에서 45%로 내려앉았다. 이 도구는 같은 9종목과
같은 리밸런싱 규칙에서 위 장치의 조합만 바꿔 비교한다.

모형
- 정수 주. 수수료 0.015%, 슬리피지 0.05%, 매도 거래세 0.20%, 현금 이자 0%.
- 판단과 체결 모두 당일 종가(운영은 오전 10시 가격 — 근사). 손절을 리밸런싱보다 먼저 본다.
- 리밸런싱: 종목 비중 8%p 이탈 또는 총 주식 비중 3%p 이탈. 최소 거래 20만원, 1회 회전
  15%, 매도 먼저. 종목별 교정으로 못 채운 부족분은 20만원 단위 보충 매수로 메운다.
- 상대 손절: (종목 가격 / 평균 매입가) ÷ (069500 가격 / 매입 시점 069500 평균) − 1 ≤ −25%.
  시장 전체 하락에는 걸리지 않고, 시장보다 25% 더 빠진 종목만 판다.

한계
- 지금 보유한 9종목은 2026년까지 살아남은 대형주다. 상장폐지·장기 몰락 종목이 빠져 있어
  손절의 '대형 사고 방지' 효과는 과소평가된다. 상대 손절도 개별 종목 사고에는 걸리도록 했다.
- 이미 본 기간을 다시 비교한 사후 검증이다. 임계값(25%, 15%, 60일)은 운영값 그대로이며
  수익률을 높이려고 탐색하지 않았다.

실행: python tools/hold_policy_backtest.py --as-of 2026-10-20
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tools import risk_overlay_backtest as rob  # noqa: E402

COMMISSION = 0.00015
SLIPPAGE = 0.0005
STOCK_TAX = 0.0020
BENCHMARK = "069500"


@dataclass(frozen=True)
class HoldPolicy:
    name: str
    label: str
    stop: str = "none"            # none | absolute | relative
    stop_pct: float = 0.25
    cooldown_days: int = 60
    account_lock: bool = False     # 계좌 낙폭이 lock_mdd 이상이면 매수 차단
    lock_mdd: float = 0.15


POLICIES = [
    HoldPolicy("static", "손절 없음 · 차단 없음"),
    HoldPolicy("lock15", "계좌 낙폭 15% 매수 차단만", account_lock=True),
    HoldPolicy("abs25", "절대 손절 -25%만", stop="absolute"),
    HoldPolicy("abs25_lock15", "절대 손절 + 낙폭 차단 (운영 규칙)", stop="absolute", account_lock=True),
    HoldPolicy("rel25", "시장 대비 상대 손절 -25%", stop="relative"),
    HoldPolicy("rel25_lock15", "상대 손절 + 낙폭 차단", stop="relative", account_lock=True),
]

# 결론이 임계값 하나에 기대는지 보는 점검용. 운영값(25%)을 고르는 데 쓰지 않는다.
SENSITIVITY = [
    HoldPolicy("abs20", "절대 손절 -20%", stop="absolute", stop_pct=0.20),
    HoldPolicy("abs30", "절대 손절 -30%", stop="absolute", stop_pct=0.30),
    HoldPolicy("rel20", "상대 손절 -20%", stop="relative", stop_pct=0.20),
    HoldPolicy("rel30", "상대 손절 -30%", stop="relative", stop_pct=0.30),
]


@dataclass
class _Position:
    qty: int = 0
    avg_price: float = 0.0
    bench_basis: float = 0.0   # 매입 시점 기준지수의 수량 가중 평균(평균 매입가와 같은 방식)


@dataclass
class SimConfig:
    initial: float = 10_000_000.0
    stock_fraction: float = 0.60
    drift_threshold: float = 0.08
    deployment_band: float = 0.03
    min_trade: float = 200_000.0
    max_turnover: float = 0.15
    min_cash_ratio: float = 0.20
    extra: dict = field(default_factory=dict)


def _buy_cost(price: float, qty: int) -> tuple[float, float]:
    fill = price * (1 + SLIPPAGE)
    return fill, fill * qty * COMMISSION


def _sell_proceeds(price: float, qty: int) -> tuple[float, float]:
    fill = price * (1 - SLIPPAGE)
    gross = fill * qty
    return fill, gross * (COMMISSION + STOCK_TAX)


def simulate_hold(panel, bench, policy: HoldPolicy, cfg: SimConfig | None = None):
    """정수 주 9종목 동일비중 + 현금. 반환 (일별 frame, 부가 통계, 손절 이벤트 목록)."""
    import pandas as pd

    cfg = cfg or SimConfig()
    symbols = list(panel.columns)
    n = len(symbols)
    target_w = 1.0 / n
    dates = panel.index
    cash = cfg.initial
    positions = {s: _Position() for s in symbols}
    blocked_until: dict[str, object] = {}
    peak = 1.0
    twr = 1.0
    prev_total = cfg.initial
    rows = []
    stop_events = []
    turnover_value = 0.0
    exposure_sum = 0.0
    locked_days = 0
    blocked_buys = 0

    for day in dates:
        prices = {s: float(panel.at[day, s]) for s in symbols}
        b = float(bench.at[day])
        total_before = cash + sum(p.qty * prices[s] for s, p in positions.items())
        drawdown_before = twr / peak - 1.0
        locked = policy.account_lock and drawdown_before <= -policy.lock_mdd
        locked_days += int(locked)
        traded_today = 0.0

        # 1) 손절 — 리밸런싱보다 먼저
        if policy.stop != "none":
            for s, p in positions.items():
                if p.qty <= 0:
                    continue
                own = prices[s] / p.avg_price - 1.0
                if policy.stop == "absolute":
                    hit = own <= -policy.stop_pct
                else:
                    rel = (prices[s] / p.avg_price) / (b / p.bench_basis) - 1.0
                    hit = rel <= -policy.stop_pct
                if not hit:
                    continue
                fill, cost = _sell_proceeds(prices[s], p.qty)
                proceeds = fill * p.qty - cost
                realized = proceeds - p.avg_price * p.qty
                stop_events.append({
                    "date": day, "symbol": s, "qty": p.qty, "price": prices[s],
                    "avg_price": p.avg_price, "own_return": own,
                    "bench_return": b / p.bench_basis - 1.0, "realized": realized,
                })
                cash += proceeds
                turnover_value += fill * p.qty
                traded_today += fill * p.qty
                positions[s] = _Position()
                blocked_until[s] = day + timedelta(days=policy.cooldown_days)

        blocked = {s for s, until in blocked_until.items() if day < until}

        # 2) 리밸런싱 판단
        total = cash + sum(p.qty * prices[s] for s, p in positions.items())
        investable = total * cfg.stock_fraction
        values = {s: positions[s].qty * prices[s] for s in symbols}
        stock_value = sum(values.values())
        weights = {s: (values[s] / investable if investable > 0 else 0.0) for s in symbols}
        max_drift = max((abs(target_w - weights[s]) for s in symbols if s not in blocked), default=0.0)
        gap = stock_value / total - cfg.stock_fraction if total > 0 else 0.0
        trigger = (max_drift >= cfg.drift_threshold) or (abs(gap) >= cfg.deployment_band)

        if trigger and total > 0:
            budget = total * cfg.max_turnover
            used = 0.0
            candidates = []
            for s in symbols:
                diff = investable * target_w - values[s]
                if diff > 0 and s in blocked:
                    continue
                trade_value = abs(diff)
                if trade_value < cfg.min_trade:
                    continue
                qty = int(trade_value / prices[s])
                if qty <= 0:
                    continue
                if diff > 0:
                    candidates.append(("BUY", s, qty))
                else:
                    candidates.append(("SELL", s, min(qty, positions[s].qty)))
            candidates.sort(key=lambda c: (0 if c[0] == "SELL" else 1, -c[2] * prices[c[1]]))
            planned = []
            for action, s, qty in candidates:
                left = budget - used
                if left < cfg.min_trade:
                    break
                notional = qty * prices[s]
                if notional > left:
                    qty = int(left / prices[s])
                    notional = qty * prices[s]
                if qty <= 0 or notional < cfg.min_trade:
                    continue
                planned.append((action, s, qty))
                used += notional

            # 보충 매수: 종목별 교정 후에도 총 주식 비중이 밴드 밖으로 모자라면 채운다
            projected = stock_value + sum(
                (1 if a == "BUY" else -1) * q * prices[s] for a, s, q in planned
            )
            shortfall = investable - projected
            band = cfg.deployment_band * total
            ordered = {s for _, s, _ in planned}
            if shortfall > band and budget - used >= cfg.min_trade:
                remaining = shortfall
                topups = []
                for s in symbols:
                    if s in ordered or s in blocked:
                        continue
                    if investable * target_w - values[s] <= 0:
                        continue
                    qty = max(1, int(cfg.min_trade // prices[s]) + (1 if cfg.min_trade % prices[s] else 0))
                    notional = qty * prices[s]
                    residual = abs(remaining - notional)
                    if residual >= abs(remaining):
                        continue
                    projected_w = (values[s] + notional) / investable
                    if projected_w > target_w + cfg.drift_threshold:
                        continue
                    topups.append((residual, notional, s, qty))
                topups.sort()
                for _residual, notional, s, qty in topups:
                    if remaining <= band:
                        break
                    if notional > budget - used:
                        continue
                    if abs(remaining - notional) >= abs(remaining):
                        continue
                    planned.append(("BUY", s, qty))
                    remaining -= notional
                    used += notional

            # 3) 체결 — 매도 먼저, 매수는 현금 하한과 계좌 낙폭 차단을 지킨다
            reserve = total * cfg.min_cash_ratio
            for action, s, qty in [x for x in planned if x[0] == "SELL"] + [x for x in planned if x[0] == "BUY"]:
                p = positions[s]
                if action == "SELL":
                    qty = min(qty, p.qty)
                    if qty <= 0:
                        continue
                    fill, cost = _sell_proceeds(prices[s], qty)
                    cash += fill * qty - cost
                    p.qty -= qty
                    if p.qty == 0:
                        positions[s] = _Position()
                    turnover_value += fill * qty
                    traded_today += fill * qty
                    continue
                if locked:
                    blocked_buys += 1
                    continue
                fill, cost = _buy_cost(prices[s], qty)
                while qty > 0 and cash - (fill * qty + cost) < reserve:
                    qty -= 1
                    fill, cost = _buy_cost(prices[s], qty)
                if qty <= 0 or qty * prices[s] < cfg.min_trade:
                    continue
                cash -= fill * qty + cost
                new_qty = p.qty + qty
                p.avg_price = (p.avg_price * p.qty + fill * qty) / new_qty
                p.bench_basis = (p.bench_basis * p.qty + b * qty) / new_qty
                p.qty = new_qty
                turnover_value += fill * qty
                traded_today += fill * qty

        total = cash + sum(p.qty * prices[s] for s, p in positions.items())
        daily_return = total / prev_total - 1.0
        twr *= 1.0 + daily_return
        peak = max(peak, twr)
        prev_total = total
        stock_now = total - cash
        exposure_sum += stock_now / total if total > 0 else 0.0
        rows.append({
            "date": day, "total": total, "twr": twr, "contributed": cfg.initial,
            "daily_return": daily_return, "drawdown": twr / peak - 1.0,
            "stock_w": stock_now / total if total > 0 else 0.0,
            "cash": cash, "locked": locked, "trade_value": traded_today,
            "held_names": sum(1 for p in positions.values() if p.qty > 0),
        })

    frame = pd.DataFrame(rows).set_index("date")
    extra = {
        "turnover_value": turnover_value,
        "avg_exposure": exposure_sum / max(len(frame), 1),
        "locked_days": locked_days,
        "blocked_buy_orders": blocked_buys,
    }
    return frame, extra, stop_events


def stop_forward_returns(stop_events, panel, bench, horizons=(20, 60, 120)):
    """손절 뒤 판 종목이 어떻게 됐는가 — 시장 대비 초과수익(판 뒤 오르면 양수 = 손절이 손해)."""
    out = {}
    index = list(panel.index)
    pos = {d: i for i, d in enumerate(index)}
    for h in horizons:
        excess = []
        for ev in stop_events:
            i = pos.get(ev["date"])
            if i is None or i + h >= len(index):
                continue
            later = index[i + h]
            own = float(panel.at[later, ev["symbol"]]) / float(panel.at[ev["date"], ev["symbol"]]) - 1.0
            mkt = float(bench.at[later]) / float(bench.at[ev["date"]]) - 1.0
            excess.append(own - mkt)
        if excess:
            out[f"{h}d"] = {
                "events": len(excess),
                "mean_excess_pct": round(sum(excess) / len(excess) * 100, 2),
                "rebound_share_pct": round(sum(1 for x in excess if x > 0) / len(excess) * 100, 1),
            }
    return out


def run(start: str, symbols=None, policies=None, cfg: SimConfig | None = None):
    import pandas as pd

    symbols = list(symbols or rob.configured_basket_symbols())
    panel = pd.DataFrame({s: rob._fdr(s, start) for s in symbols}).dropna(how="any")
    bench = rob._fdr(BENCHMARK, start).reindex(panel.index).ffill()
    if bench.isna().any():
        raise ValueError("기준 ETF 자료가 비어 있는 날이 있습니다")
    results = {}
    frames = {}
    for policy in (policies or POLICIES):
        frame, extra, events = simulate_hold(panel, bench, policy, cfg)
        m = rob.metrics(frame, extra, rf_annual=0.0)
        m.update({
            "label": policy.label,
            "stop_events": len(events),
            "stop_realized_krw": round(sum(e["realized"] for e in events)),
            "locked_days": extra["locked_days"],
            "locked_days_pct": round(extra["locked_days"] / max(len(frame), 1) * 100, 1),
            "blocked_buy_orders": extra["blocked_buy_orders"],
            "after_stop": stop_forward_returns(events, panel, bench),
            "min_stock_weight_pct": round(float(frame["stock_w"].min()) * 100, 1),
        })
        results[policy.name] = m
        frames[policy.name] = frame
    return {
        "symbols": symbols,
        "benchmark": BENCHMARK,
        "start": str(panel.index[0].date()),
        "end": str(panel.index[-1].date()),
        "days": len(panel),
        "results": results,
    }, frames


def _markdown(title: str, summary: dict) -> str:
    lines = [
        f"### {title}",
        "",
        f"- 기간: {summary['start']} → {summary['end']} ({summary['days']}거래일), 종목 {len(summary['symbols'])}개, 상대 손절 기준 {summary['benchmark']}",
        "",
        "| 규칙 | 연수익률 | 최대낙폭 | 샤프(금리 0%) | 칼마 | 최악 연도 | 평균 주식 비중 | 손절 횟수 | 손절 실현손익 | 매수 차단일 비율 | 손절 60일 뒤 시장 대비 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary["results"].values():
        after = r["after_stop"].get("60d")
        after_txt = f"{after['mean_excess_pct']:+.1f}%p ({after['events']}건)" if after else "—"
        calmar = f"{r['calmar']:.2f}" if r["calmar"] is not None else "—"
        lines.append(
            f"| {r['label']} | {r['cagr_pct']:+.2f}% | {r['mdd_pct']:.1f}% | {r['sharpe']:.2f} | {calmar} | "
            f"{r['worst_year_pct']:+.1f}% | {r['avg_exposure_pct']:.0f}% | {r['stop_events']} | "
            f"{r['stop_realized_krw']:+,}원 | {r['locked_days_pct']:.0f}% | {after_txt} |"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", default=rob.AS_OF, help="이 날짜 미만의 확정 일봉만 사용 (YYYY-MM-DD)")
    parser.add_argument("--start", action="append", help="시작일. 여러 번 주면 구간마다 따로 계산")
    parser.add_argument("--out-dir", default=str(_ROOT / "reports" / "research"))
    parser.add_argument("--sensitivity", action="store_true", help="손절 임계값 20%/30% 점검 표도 만든다")
    args = parser.parse_args()
    from datetime import date

    rob.AS_OF = date.fromisoformat(args.as_of).isoformat()
    starts = args.start or ["2012-01-02", "2021-12-01"]
    report = {"as_of": rob.AS_OF, "windows": {}}
    md = ["# 대형주 계좌 손절·낙폭 차단 규칙 비교", ""]
    for start in starts:
        summary, _ = run(start)
        report["windows"][start] = summary
        md += [_markdown(f"{start}부터", summary), ""]
        if args.sensitivity:
            check, _ = run(start, policies=[POLICIES[0], *SENSITIVITY])
            report.setdefault("sensitivity", {})[start] = check
            md += [_markdown(f"{start}부터 · 임계값 점검(운영값 선택에 쓰지 않음)", check), ""]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hold_policy_backtest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    (out_dir / "hold_policy_backtest.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""2026-09 위험 관리 검증. 주문 없이 과거 종가와 격리된 가상 계좌만 사용한다.

고정한 정책을 기간별·비용별로 비교한다. 미래에 수집될 자료를 확보한 실험이 아니므로
이 결과를 독립적인 미사용 표본 검증이나 향후 수익 보장으로 해석하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.risk_overlays import (
    compute_decision,
    overlay_target_weights,
    parse_overlay_config,
)
from tools import risk_overlay_backtest as research


def align_report_inputs(series, as_of):
    """공통 확정일까지만 비교하고, 기간 중 누락된 종가는 채우지 않는다."""
    cutoff = pd.Timestamp(as_of)
    clean = {}
    for symbol in ("069500", "357870", "KS200"):
        values = series[symbol].loc[series[symbol].index < cutoff]
        if (
            values.empty
            or not values.index.is_unique
            or not values.index.is_monotonic_increasing
            or not all(math.isfinite(v) and v > 0 for v in values)
        ):
            raise ValueError(f"{symbol}: 종가와 날짜를 확인하세요")
        clean[symbol] = values
    end = min(values.index[-1] for values in clean.values())
    aligned = {symbol: values.loc[:end] for symbol, values in clean.items()}
    if any(values.empty for values in aligned.values()):
        raise ValueError("세 자료가 겹치는 비교 기간이 없습니다")
    panel = pd.DataFrame(aligned)
    start = max(values.index[0] for values in aligned.values())
    incomplete = panel.loc[start:].isna().any(axis=1)
    if incomplete.any():
        dates = ", ".join(str(day.date()) for day in incomplete[incomplete].index[:5])
        raise ValueError(
            f"비교 기간 중 종가 누락: {dates}. 누락을 채우지 않고 검증을 중단합니다"
        )
    audit = {
        "common_last_bar": str(end.date()),
        "latest_available": {
            symbol: str(values.index[-1].date()) for symbol, values in clean.items()
        },
        "excluded_after_common_bar": {
            symbol: int((values.index > end).sum()) for symbol, values in clean.items()
        },
    }
    return aligned, panel, audit


def integer_etf_simulation(
    panel,
    policy,
    *,
    redirect=True,
    cost_multiple=1.0,
    initial=300_000.0,
    monthly=100_000.0,
    min_trade=50_000.0,
):
    """ETF 1주 단위, 다음 거래일 종가, 현금 5%, 편도 회전 상한 60%로 비교.

    실제 주문 엔진의 호가·체결 지연·괴리율은 재현하지 않는다. CD ETF는 양의 매매차익에
    15.4%를 차감하는 상한 근사(과표기준가 미확보), KODEX 200 분배금은 미반영이다.
    """
    cfg = parse_overlay_config(
        {
            "overlays": {
                "combination": policy.combination,
                "trend_filter": {
                    "enabled": policy.trend,
                    "ma_days": policy.trend_ma_days,
                    "band": policy.trend_band,
                },
                "drawdown_guard": {
                    "enabled": policy.dd,
                    "trigger": policy.dd_trigger,
                    "release": policy.dd_release,
                    "scale": policy.dd_scale,
                },
            }
        }
    )
    symbols = ["069500", "357870"]
    quantity = {s: 0 for s in symbols}
    avg = {s: 0.0 for s in symbols}
    cash = initial
    contributed = initial
    previous_total = initial
    twr = peak = 1.0
    previous_state = None
    previous_month = None
    rows = []
    # 200일선 계산 기간은 성과 집계 전에 따로 확보한다.
    start = max(200, panel.index.get_indexer([panel[symbols].dropna().index[0]])[0])
    commission = research.COMMISSION * cost_multiple
    slippage = research.SLIPPAGE * cost_multiple
    # 일마다 전체 과거를 복사하지 않고 필요한 이동평균 창만 전달한다.
    # 다음 날짜와 당일 종가는 신호 입력에 포함하지 않는다.
    index_prices = panel["KS200"].to_numpy()
    execution_prices = panel[symbols].to_numpy()
    for i in range(start, len(panel)):
        day = panel.index[i]
        prices = {s: float(execution_prices[i, j]) for j, s in enumerate(symbols)}
        if any(not math.isfinite(p) or p <= 0 for p in prices.values()):
            raise ValueError("ETF 종가가 누락됐습니다")
        month = (day.year, day.month)
        flow = (
            monthly if previous_month is not None and month != previous_month else 0.0
        )
        cash += flow
        contributed += flow
        previous_month = month
        decision = compute_decision(
            cfg,
            index_closes=index_prices[max(0, i - cfg.trend.ma_days) : i].tolist(),
            cumulative_returns_pct=[(peak - 1) * 100, (twr - 1) * 100],
            prev_state=previous_state,
            now=day.to_pydatetime(),
        )
        previous_state = decision.to_dict()
        targets = overlay_target_weights(
            {s: 0.5 for s in symbols},
            0.95,
            decision.scale,
            "357870" if redirect else None,
        )
        total_before = cash + sum(quantity[s] * prices[s] for s in symbols)
        invested_fraction = sum(targets.values())
        sleeve = total_before * invested_fraction
        invested = total_before - cash
        drift = (
            max(
                abs(quantity[s] * prices[s] / sleeve - targets[s] / invested_fraction)
                for s in symbols
            )
            if sleeve > 0
            else 0.0
        )
        need = (
            i == start
            or drift >= 0.08
            or abs(invested / total_before - invested_fraction) >= 0.03
        )
        trade_value = costs = 0.0
        budget = total_before * 0.6
        if need:
            # 매도 후 매수. 목표 미달 1주를 억지로 올려 사지 않는다.
            diffs = {
                s: total_before * targets[s] - quantity[s] * prices[s] for s in symbols
            }
            for s in sorted(symbols, key=lambda symbol: diffs[symbol]):
                price, diff = prices[s], diffs[s]
                qty = int(abs(diff) / price)
                if qty <= 0 or qty * price < min_trade:
                    continue
                fill = price * (1 + slippage if diff > 0 else 1 - slippage)
                qty = min(qty, int(budget / fill))
                if diff < 0:
                    qty = min(qty, quantity[s])
                else:
                    available = max(0.0, cash - total_before * 0.05)
                    qty = min(qty, int(available / (fill * (1 + commission))))
                if qty <= 0 or qty * fill < min_trade:
                    continue
                notional = qty * fill
                cost = notional * commission
                if diff < 0:
                    if s == "357870":
                        cost += max(0.0, fill - avg[s]) * qty * 0.154
                    quantity[s] -= qty
                    cash += notional - cost
                else:
                    avg[s] = (avg[s] * quantity[s] + notional + cost) / (
                        quantity[s] + qty
                    )
                    quantity[s] += qty
                    cash -= notional + cost
                budget -= notional
                trade_value += notional
                costs += cost
        total = cash + sum(quantity[s] * prices[s] for s in symbols)
        daily_return = total / (previous_total + flow) - 1.0
        twr *= 1 + daily_return
        peak = max(peak, twr)
        previous_total = total
        rows.append(
            {
                "date": day,
                "total": total,
                "twr": twr,
                "drawdown": twr / peak - 1,
                "daily_return": daily_return,
                "flow": flow,
                "contributed": contributed,
                "cash": cash,
                "stock_w": quantity["069500"] * prices["069500"] / total,
                "scale": decision.scale,
                "trade_value": trade_value,
                "cost": costs,
                **{f"qty_{s}": quantity[s] for s in symbols},
            }
        )
    frame = pd.DataFrame(rows).set_index("date")
    return frame, {
        "turnover_value": frame.trade_value.sum(),
        "avg_exposure": frame.stock_w.mean(),
    }


def period_metrics(frame, start, end):
    part = frame.loc[start:end].copy()
    if len(part) < 2:
        return None
    part["twr"] = (1 + part.daily_return).cumprod()
    part["drawdown"] = part.twr / part.twr.cummax().clip(lower=1) - 1
    extra = {
        "turnover_value": part.trade_value.sum(),
        "avg_exposure": part.stock_w.mean(),
    }
    return research.metrics(part, extra)


def plot_comparison(frames, path):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    for name in ["Malgun Gothic", "Noto Sans CJK KR", "Apple SD Gothic Neo"]:
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(
        2, 1, figsize=(11, 6.6), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )
    names = {
        "static": "고정 비중",
        "old_product": "기존 방식 (TWR 오류 수정)",
        "minimum": "주식만 조절·중복 축소 방지",
    }
    colors = {"static": "#6f6f6f", "old_product": "#9f1853", "minimum": "#0f62fe"}
    for key, f in frames.items():
        if key not in names:
            continue
        ax[0].plot(f.index, f.twr * 100, label=names[key], color=colors[key], lw=1.8)
        ax[1].plot(f.index, f.drawdown * 100, color=colors[key], lw=1.2)
    ax[0].set_title("ETF 적립 계좌의 수익과 하락 구간", loc="left", fontsize=16, pad=17)
    ax[0].set_ylabel("시간가중 지수\n시작 = 100", fontsize=10)
    ax[1].set_ylabel("고점 대비 하락률 (%)", fontsize=10)
    ax[0].legend(frameon=False, fontsize=9, loc="upper left")
    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
        a.spines[["left", "bottom"]].set_color("#c6c6c6")
        a.grid(axis="y", alpha=0.12)
        a.tick_params(labelsize=9, colors="#525252")
    fig.text(
        0.07,
        0.025,
        "실제 ETF 종가 · 1주 단위 · 월 10만원 적립 · 수수료·슬리피지 반영 · 과거 성과이며 미래 수익을 보장하지 않음",
        fontsize=9,
        color="#525252",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default="2026-09-17")
    parser.add_argument("--output")
    parser.add_argument("--image")
    args = parser.parse_args()
    stamp = pd.Timestamp(args.as_of).strftime("%Y%m%d")
    args.output = args.output or f"reports/research/risk_review_{stamp}.json"
    args.image = args.image or f"docs/images/risk-review-{stamp}.png"
    research.AS_OF = args.as_of
    series = {s: research._fdr(s, "2014-01-01") for s in ["069500", "357870", "KS200"]}
    series, panel, data_audit = align_report_inputs(series, args.as_of)
    policies = {
        "static": research.Policy("static", "고정 비중"),
        "old_product": research.Policy("old_product", "기존 방식", trend=True, dd=True),
        "minimum": research.Policy(
            "minimum",
            "주식만 조절·중복 축소 방지",
            trend=True,
            dd=True,
            combination="minimum",
        ),
    }
    frames = {}
    result = {}
    for name, policy in policies.items():
        f, extra = integer_etf_simulation(panel, policy, redirect=name != "old_product")
        frames[name] = f
        result[name] = {
            "label": policy.label,
            "all": research.metrics(f, extra),
            "periods": {
                label: period_metrics(f, a, b)
                for label, a, b in [
                    ("2020_2022", "2020-01-01", "2022-12-31"),
                    ("2023_2025", "2023-01-01", "2025-12-31"),
                    ("2026", "2026-01-01", args.as_of),
                ]
            },
        }
        stressed, ex = integer_etf_simulation(
            panel, policy, redirect=name != "old_product", cost_multiple=3
        )
        result[name]["triple_cost"] = research.metrics(stressed, ex)
    # 소수 주 장기 연구: 고정된 정책으로 구간을 나눠 보고, 금리 0%에서도 비교한다.
    long_run = {}
    for name, policy in policies.items():
        f, extra = research.simulate(
            series["069500"], policy, index_closes=series["KS200"]
        )
        zero, ex = research.simulate(
            series["069500"], policy, index_closes=series["KS200"], rf_annual=0
        )
        long_run[name] = {
            "all": research.metrics(f, extra),
            "zero_cash_yield": research.metrics(zero, ex),
            "periods": {
                label: period_metrics(f, a, b)
                for label, a, b in [
                    ("2014_2018", "2014-01-01", "2018-12-31"),
                    ("2019_2022", "2019-01-01", "2022-12-31"),
                    ("2023_2026", "2023-01-01", args.as_of),
                ]
            },
        }
    manifest = {
        s: {
            "first": str(v.index[0].date()),
            "last": str(v.index[-1].date()),
            "rows": len(v),
            "sha256": hashlib.sha256(v.to_csv().encode()).hexdigest(),
        }
        for s, v in series.items()
    }
    payload = {
        "as_of": args.as_of,
        "last_complete_bar": str(frames["static"].index[-1].date()),
        "integer_start": str(frames["static"].index[0].date()),
        "source": "FinanceDataReader, close prices",
        "data_audit": data_audit,
        "data": manifest,
        "integer_etf": result,
        "fractional_research": long_run,
        "limitations": [
            "동일 기간을 이미 살펴본 사후 검증이며 독립적인 미사용 표본이 아님",
            "ETF 분배금 미포함; 현금 이자 0%, CD ETF 양의 매매차익 15.4% 상한 과세",
            "다음 거래일 종가에 비용을 더한 근사 체결; 실시간 호가·괴리율·미체결 미재현",
            "소수 주 연구의 현금금리는 연 3% 고정 가정; 금리 0% 민감도도 공개",
            "부분 연도는 연도 전체 수익률이 아님; 실전 자동 전환 없음",
            "자료별 마지막 날짜가 다르면 공통 확정일까지 비교; 뒤 날짜는 제외",
        ],
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    plot_comparison(frames, Path(args.image))
    print(
        json.dumps(
            {
                "integer_start": payload["integer_start"],
                "data_audit": data_audit,
                "last": payload["last_complete_bar"],
                "integer_etf": result,
                "fractional": long_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

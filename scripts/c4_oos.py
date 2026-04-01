"""
C-4 breakout_volume OOS 검증
- 개발 구간: 2021-01-01 ~ 2023-12-31
- 검증 구간: 2024-01-01 ~ 2025-12-31
- 종목: 005930, 000660, 035720
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import itertools
import pandas as pd
from loguru import logger
from core.data_collector import DataCollector
from backtest.backtester import Backtester

logger.remove()
logger.add(sys.stderr, level="WARNING")

SYMBOLS = ["005930", "000660", "035720"]
DEV_START = "2021-01-01"
DEV_END = "2023-12-31"
OOS_START = "2024-01-01"
OOS_END = "2025-12-31"

SWEEP = {
    "breakout_period": [10, 20],
    "surge_ratio": [1.5, 2.0, 2.5],
    "adx_min": [15, 20],
}


def run_single(symbol, params, start, end):
    collector = DataCollector()
    df = collector.fetch_stock(symbol, start, end)
    if df is None or df.empty:
        return None

    bt = Backtester()
    result = bt.run(
        df,
        strategy_name="breakout_volume",
        strict_lookahead=False,
        param_overrides={"breakout_volume": params},
        notify_overtrading=False,
    )
    if not result or "metrics" not in result:
        return None

    m = result["metrics"]
    return {
        "symbol": symbol,
        "breakout_period": params["breakout_period"],
        "surge_ratio": params["surge_ratio"],
        "adx_min": params["adx_min"],
        "total_trades": m.get("total_trades", 0),
        "total_return": round(m.get("total_return", 0), 2),
        "sharpe": round(m.get("sharpe_ratio", 0), 2),
        "mdd": round(m.get("max_drawdown", 0), 2),
        "win_rate": round(m.get("win_rate", 0), 1),
    }


if __name__ == "__main__":
    keys = list(SWEEP.keys())
    values = list(SWEEP.values())
    combos = list(itertools.product(*values))

    # ── Phase 1: 개발 구간 스윕 ──
    print("=" * 100)
    print("Phase 1: 개발 구간 (2021-01-01 ~ 2023-12-31)")
    print("=" * 100)

    dev_results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        for sym in SYMBOLS:
            r = run_single(sym, params, DEV_START, DEV_END)
            if r:
                dev_results.append(r)

    # 조합별 합산
    print(f"\n{'period':>6} {'surge':>5} {'adx':>3} | {'trades':>6} {'agg_ret':>8} {'avg_sharpe':>10} {'avg_mdd':>8}")
    print("-" * 60)

    best_combo = None
    best_agg_return = -999

    for combo in combos:
        params = dict(zip(keys, combo))
        subset = [r for r in dev_results
                  if r["breakout_period"] == params["breakout_period"]
                  and r["surge_ratio"] == params["surge_ratio"]
                  and r["adx_min"] == params["adx_min"]]
        if not subset:
            continue
        total_trades = sum(r["total_trades"] for r in subset)
        agg_return = sum(r["total_return"] for r in subset)
        avg_sharpe = sum(r["sharpe"] for r in subset) / len(subset)
        avg_mdd = sum(r["mdd"] for r in subset) / len(subset)

        print(f"{params['breakout_period']:>6} {params['surge_ratio']:>5} {params['adx_min']:>3} | "
              f"{total_trades:>6} {agg_return:>8.2f}% {avg_sharpe:>10.2f} {avg_mdd:>7.2f}%")

        if agg_return > best_agg_return:
            best_agg_return = agg_return
            best_combo = params.copy()

    print(f"\n>>> 개발 구간 Best: {best_combo} (agg_return={best_agg_return:.2f}%)")

    # ── Phase 2: 검증 구간 ──
    print("\n" + "=" * 100)
    print(f"Phase 2: 검증 구간 (2024-01-01 ~ 2025-12-31) — 조합 고정: {best_combo}")
    print("=" * 100)

    oos_results = []
    for sym in SYMBOLS:
        r = run_single(sym, best_combo, OOS_START, OOS_END)
        if r:
            oos_results.append(r)
            print(f"  {sym}: trades={r['total_trades']}, ret={r['total_return']}%, "
                  f"sharpe={r['sharpe']}, mdd={r['mdd']}%, win={r['win_rate']}%")
        else:
            print(f"  {sym}: FAILED")

    # 합산
    if oos_results:
        total_trades = sum(r["total_trades"] for r in oos_results)
        agg_return = sum(r["total_return"] for r in oos_results)
        avg_sharpe = sum(r["sharpe"] for r in oos_results) / len(oos_results)
        avg_mdd = sum(r["mdd"] for r in oos_results) / len(oos_results)
        non_neg_count = sum(1 for r in oos_results if r["total_return"] >= 0)
        t035720 = next((r["total_trades"] for r in oos_results if r["symbol"] == "035720"), 0)

        print(f"\n검증 구간 합산:")
        print(f"  총 trades: {total_trades}")
        print(f"  aggregate return: {agg_return:.2f}%")
        print(f"  평균 Sharpe: {avg_sharpe:.2f}")
        print(f"  평균 MDD: {avg_mdd:.2f}%")
        print(f"  non-negative return 종목: {non_neg_count}/3")
        print(f"  035720 trades: {t035720}")

        # 판정
        pass_trades = total_trades >= 20
        pass_return = agg_return >= 0
        pass_mdd = avg_mdd > -6
        pass_non_neg = non_neg_count >= 2
        pass_035720 = t035720 >= 4

        all_pass = pass_trades and pass_return and pass_mdd and pass_non_neg and pass_035720
        some_pass = pass_return or (non_neg_count >= 2)

        if all_pass:
            verdict = "PASSES_HONEST_OOS"
        elif some_pass:
            verdict = "MIXED_OOS"
        else:
            verdict = "FAILS_OOS"

        print(f"\n판정: {verdict}")
        print(f"  trades>=20: {pass_trades} | return>=0: {pass_return} | "
              f"MDD>-6%: {pass_mdd} | non-neg>=2: {pass_non_neg} | 035720>=4: {pass_035720}")

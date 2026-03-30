"""
C-3A 눌림목 전략 소규모 스윕
- 종목: 005930, 000660, 035720
- 기간: 2021-01-01 ~ 2025-12-31
- 스윕: sma_period=[60], rsi_entry=[45,50], adx_min=[15,20]
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
START = "2021-01-01"
END = "2025-12-31"

SWEEP = {
    "sma_period": [60],
    "rsi_entry": [45, 50],
    "adx_min": [15, 20],
}

def run_single(symbol, params):
    collector = DataCollector()
    df = collector.fetch_stock(symbol, START, END)
    if df is None or df.empty:
        return None

    bt = Backtester()
    result = bt.run(
        df,
        strategy_name="trend_pullback",
        strict_lookahead=False,  # 스윕 속도 위해 False
        param_overrides={"trend_pullback": params},
        notify_overtrading=False,
    )
    if not result or "metrics" not in result:
        return None

    m = result["metrics"]
    trades = result.get("trades", [])
    # avg holding days 계산
    holding_days = []
    for t in trades:
        if t.get("action") not in ("BUY",):
            buy_date = t.get("buy_date")
            sell_date = t.get("date")
            if buy_date and sell_date:
                try:
                    bd = pd.Timestamp(buy_date)
                    sd = pd.Timestamp(sell_date)
                    holding_days.append((sd - bd).days)
                except Exception:
                    pass

    return {
        "symbol": symbol,
        "sma_period": params["sma_period"],
        "rsi_entry": params["rsi_entry"],
        "adx_min": params["adx_min"],
        "total_trades": m.get("total_trades", 0),
        "total_return": round(m.get("total_return", 0), 2),
        "sharpe": round(m.get("sharpe_ratio", 0), 2),
        "mdd": round(m.get("max_drawdown", 0), 2),
        "win_rate": round(m.get("win_rate", 0), 1),
        "avg_holding_days": round(sum(holding_days) / len(holding_days), 1) if holding_days else 0,
    }


if __name__ == "__main__":
    keys = list(SWEEP.keys())
    values = list(SWEEP.values())
    combos = list(itertools.product(*values))

    all_results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        print(f"\n=== Sweep: {params} ===")
        for sym in SYMBOLS:
            r = run_single(sym, params)
            if r:
                all_results.append(r)
                print(f"  {sym}: trades={r['total_trades']}, ret={r['total_return']}%, "
                      f"sharpe={r['sharpe']}, mdd={r['mdd']}%, "
                      f"win={r['win_rate']}%, hold={r['avg_holding_days']}d")
            else:
                print(f"  {sym}: FAILED")

    # 종합 결과표
    print("\n" + "="*100)
    print("종목별 결과표")
    print("="*100)
    df_res = pd.DataFrame(all_results)
    print(df_res.to_string(index=False))

    # 파라미터 조합별 합산
    print("\n" + "="*100)
    print("파라미터 조합별 3종목 합산")
    print("="*100)
    for combo in combos:
        params = dict(zip(keys, combo))
        subset = [r for r in all_results
                  if r["sma_period"] == params["sma_period"]
                  and r["rsi_entry"] == params["rsi_entry"]
                  and r["adx_min"] == params["adx_min"]]
        if not subset:
            continue
        total_trades = sum(r["total_trades"] for r in subset)
        avg_return = sum(r["total_return"] for r in subset) / len(subset)
        avg_sharpe = sum(r["sharpe"] for r in subset) / len(subset)
        avg_mdd = sum(r["mdd"] for r in subset) / len(subset)
        avg_win = sum(r["win_rate"] for r in subset) / len(subset)
        trades_per_sym = {r["symbol"]: r["total_trades"] for r in subset}
        sym_with_6plus = sum(1 for t in trades_per_sym.values() if t >= 6)
        t035720 = trades_per_sym.get("035720", 0)

        print(f"\nParams: rsi_entry={params['rsi_entry']}, adx_min={params['adx_min']}")
        print(f"  총 trades: {total_trades}")
        print(f"  평균 return: {avg_return:.2f}%")
        print(f"  평균 Sharpe: {avg_sharpe:.2f}")
        print(f"  평균 MDD: {avg_mdd:.2f}%")
        print(f"  평균 win_rate: {avg_win:.1f}%")
        print(f"  종목별 trades: {trades_per_sym}")
        print(f"  trades>=6 종목 수: {sym_with_6plus}/3")
        print(f"  035720 trades: {t035720}")

        # 판정
        pass_trades = total_trades >= 25
        pass_sym6 = sym_with_6plus >= 2
        pass_return = avg_return >= 0
        pass_mdd = avg_mdd > -6
        pass_035720 = t035720 >= 4

        if pass_trades and pass_sym6 and pass_return and pass_mdd and pass_035720:
            verdict = "VIABLE_PULLBACK"
        elif total_trades > 10 and (t035720 < 4 or not pass_return):
            verdict = "STILL_TOO_SPARSE"
        else:
            verdict = "SWITCH_TO_BREAKOUT"

        print(f"  판정: {verdict}")
        print(f"    trades>=25: {pass_trades} | sym>=6: {pass_sym6} | "
              f"return>=0: {pass_return} | MDD>-6%: {pass_mdd} | 035720>=4: {pass_035720}")

#!/usr/bin/env python3
"""분산 대형주 buy&hold의 강건성(robustness) 분석.

"+132.9%/3년"이 2023-25 강세장 한 번의 운인지, 여러 구간에서 안정적인지 정직하게 본다.
- 연도별 수익률(2023/2024/2025 각각)
- 약세장 스트레스(2022 KOSPI 급락장) 포함 구간
- 롤링 1년 수익률의 분포(최저/중앙/최고)
실제 종가 동일비중 보유로만 계산. 벤치마크 트릭 없이 재현 가능.

Usage:
    python tools/buy_hold_robustness.py
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 분산 대형주 10종목(섹터 다양) — buy&hold 결론의 권장 바스켓과 동일 구성.
SYMBOLS = ["005930", "000660", "035420", "005380", "051910",
           "005490", "055550", "035720", "012330", "105560"]


def _equal_weight_nav(panel, capital=10_000_000, commission=0.00015):
    """패널(컬럼=종목 종가) 첫날 동일비중 매수→보유 NAV 시계열."""
    import pandas as pd
    first = panel.iloc[0]
    shares, invested, comm = {}, 0.0, 0.0
    per_name = capital / len(panel.columns)
    for sym in panel.columns:
        qty = int(per_name / first[sym])
        shares[sym] = qty
        invested += qty * first[sym]
        comm += qty * first[sym] * commission
    cash = capital - invested - comm
    return (panel * pd.Series(shares)).sum(axis=1) + cash


def _stats(nav, capital=10_000_000):
    total_ret = (nav.iloc[-1] / capital - 1) * 100
    dr = nav.pct_change().dropna()
    sharpe = (dr.mean() * 252 - 0.03) / (dr.std() * (252 ** 0.5)) if dr.std() > 0 else 0
    mdd = ((nav / nav.cummax() - 1) * 100).min()
    return round(total_ret, 1), round(float(sharpe), 2), round(float(mdd), 1)


def analyze(capital=10_000_000):
    import warnings, logging
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    import pandas as pd
    from core.data_collector import DataCollector

    dc = DataCollector()
    closes = {}
    # 2022 약세장 포함 위해 넉넉히 받음
    for sym in SYMBOLS:
        df = dc.fetch_stock(sym, start_date="2021-12-01", end_date="2025-12-31")
        if df is not None and not df.empty:
            d = df.copy()
            d.index = pd.to_datetime(d["date"]) if "date" in d.columns else pd.to_datetime(d.index)
            closes[sym] = d["close"].astype(float)
    panel_all = pd.DataFrame(closes).dropna(how="any")
    if panel_all.empty:
        raise SystemExit("가격 데이터 없음")

    results = {"symbols": len(panel_all.columns), "rows": len(panel_all),
               "data_start": str(panel_all.index[0].date()), "data_end": str(panel_all.index[-1].date())}

    # 연도별 + 스트레스 구간
    windows = {
        "2022 (약세장 스트레스)": ("2022-01-01", "2022-12-31"),
        "2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2022-2025 전체(약세 포함)": ("2022-01-01", "2025-12-31"),
        "2023-2025 전체(강세만)": ("2023-01-01", "2025-12-31"),
    }
    period_stats = {}
    for label, (s, e) in windows.items():
        sub = panel_all[(panel_all.index >= pd.Timestamp(s)) & (panel_all.index <= pd.Timestamp(e))]
        if len(sub) < 20:
            period_stats[label] = None
            continue
        nav = _equal_weight_nav(sub, capital)
        period_stats[label] = _stats(nav, capital)
    results["periods"] = period_stats

    # 롤링 1년(252거래일) 수익률 분포
    nav_full = _equal_weight_nav(panel_all, capital)
    roll = []
    vals = nav_full.values
    for i in range(252, len(vals)):
        roll.append((vals[i] / vals[i - 252] - 1) * 100)
    if roll:
        roll_sorted = sorted(roll)
        n = len(roll_sorted)
        results["rolling_1y"] = {
            "count": n,
            "min": round(roll_sorted[0], 1),
            "p25": round(roll_sorted[n // 4], 1),
            "median": round(roll_sorted[n // 2], 1),
            "max": round(roll_sorted[-1], 1),
            "negative_pct": round(sum(1 for r in roll if r < 0) / n * 100, 1),
        }
    return results


def main():
    p = argparse.ArgumentParser(description="buy&hold 강건성 분석")
    p.add_argument("--capital", type=float, default=10_000_000)
    args = p.parse_args()
    r = analyze(args.capital)

    print(f"=== 분산 대형주 buy&hold 강건성 분석 ===")
    print(f"데이터: {r['symbols']}종목, {r['data_start']} ~ {r['data_end']} ({r['rows']} 거래일)")
    print(f"\n구간별 (총수익% / Sharpe / MDD%):")
    for label, st in r["periods"].items():
        if st is None:
            print(f"  {label:28} 데이터 부족")
        else:
            print(f"  {label:28} {st[0]:+7.1f}%  Sharpe {st[1]:+.2f}  MDD {st[2]:.1f}%")
    rl = r.get("rolling_1y")
    if rl:
        print(f"\n롤링 1년 수익률 분포({rl['count']}개 관측):")
        print(f"  최저 {rl['min']:+.1f}% | 25%분위 {rl['p25']:+.1f}% | 중앙 {rl['median']:+.1f}% | 최고 {rl['max']:+.1f}%")
        print(f"  1년 보유 시 손실 확률: {rl['negative_pct']}%")
    return r


if __name__ == "__main__":
    main()

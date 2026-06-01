#!/usr/bin/env python3
"""주식+현금 정적 자산배분(static allocation) 강건성 분석.

"안정적 고수익"은 종목 선택이 아니라 자산배분으로 접근하는 게 정석이다. 분산 대형주
buy&hold(주식 100%)는 손실 연도 + ~-25% 낙폭이 있었다(docs/BUY_HOLD_ROBUSTNESS.md).
주식 비중을 낮추고 현금(무위험 ~연 3%)을 섞으면, 수익을 일부 포기하는 대신 낙폭·손실
연도를 얼마나 줄일 수 있는지 데이터로 본다. 매년 1회 목표 비중으로 리밸런싱.

실제 종가 동일비중 주식 + 현금 일할 이자. 재현 가능.

Usage:
    python tools/static_allocation_analysis.py
"""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

SYMBOLS = ["005930", "000660", "035420", "005380", "051910",
           "005490", "055550", "035720", "012330", "105560"]
RF_ANNUAL = 0.03           # 현금/단기국공채 연 3% 가정
STOCK_WEIGHTS = [1.0, 0.7, 0.6, 0.5, 0.4]
START = "2022-01-01"       # 약세장 포함(정직한 스트레스)
END = "2025-12-31"


def _stock_nav(panel, capital, commission=0.00015):
    """동일비중 주식 buy&hold NAV (현금 레그 제외, 주식 100% 기준 가치)."""
    import pandas as pd
    first = panel.iloc[0]
    shares, invested, comm = {}, 0.0, 0.0
    per = capital / len(panel.columns)
    for sym in panel.columns:
        qty = int(per / first[sym])
        shares[sym] = qty
        invested += qty * first[sym]
        comm += qty * first[sym] * commission
    leftover = capital - invested - comm
    return (panel * pd.Series(shares)).sum(axis=1) + leftover


def _blended_nav(stock_nav, stock_w, capital):
    """주식 비중 stock_w + 현금(연 RF) 혼합, 매년초 목표비중으로 리밸런싱한 NAV.

    단순화: 매 연도 시작에 직전 NAV를 stock_w:cash_w로 재분배. 주식 레그는 그 해
    주식 일간수익률을 따르고, 현금 레그는 RF 일할로 증가.
    """
    import pandas as pd
    stock_ret = stock_nav.pct_change().fillna(0.0)
    rf_daily = (1 + RF_ANNUAL) ** (1 / 252) - 1
    cash_w = 1.0 - stock_w

    nav = []
    cur = capital
    stock_val = cur * stock_w
    cash_val = cur * cash_w
    prev_year = stock_nav.index[0].year
    for i, (ts, r) in enumerate(stock_ret.items()):
        if ts.year != prev_year:
            # 연초 리밸런싱
            total = stock_val + cash_val
            stock_val = total * stock_w
            cash_val = total * cash_w
            prev_year = ts.year
        stock_val *= (1 + r)
        cash_val *= (1 + rf_daily)
        nav.append(stock_val + cash_val)
    return pd.Series(nav, index=stock_nav.index)


def _metrics(nav, capital):
    total = (nav.iloc[-1] / capital - 1) * 100
    dr = nav.pct_change().dropna()
    sharpe = (dr.mean() * 252 - RF_ANNUAL) / (dr.std() * (252 ** 0.5)) if dr.std() > 0 else 0
    mdd = ((nav / nav.cummax() - 1) * 100).min()
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 0.1)
    cagr = ((nav.iloc[-1] / capital) ** (1 / years) - 1) * 100
    # 연도별 수익률 → 손실 연도 수
    yearly = nav.resample("YE").last()
    yr_rets = yearly.pct_change()
    first_yr = (yearly.iloc[0] / capital - 1)
    all_yr = [first_yr] + [v for v in yr_rets.dropna().values]
    neg_years = sum(1 for v in all_yr if v < 0)
    return {
        "total_return": round(total, 1),
        "cagr": round(cagr, 1),
        "sharpe": round(float(sharpe), 2),
        "mdd": round(float(mdd), 1),
        "negative_years": neg_years,
        "total_years": len(all_yr),
        "worst_year": round(min(all_yr) * 100, 1),
    }


def analyze(capital=10_000_000):
    import warnings, logging
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    import pandas as pd
    from core.data_collector import DataCollector

    dc = DataCollector()
    closes = {}
    for sym in SYMBOLS:
        df = dc.fetch_stock(sym, start_date="2021-12-01", end_date=END)
        if df is not None and not df.empty:
            d = df.copy()
            d.index = pd.to_datetime(d["date"]) if "date" in d.columns else pd.to_datetime(d.index)
            closes[sym] = d["close"].astype(float)
    panel = pd.DataFrame(closes).dropna(how="any")
    if panel.empty:
        raise SystemExit("가격 데이터 없음")
    panel = panel[(panel.index >= pd.Timestamp(START)) & (panel.index <= pd.Timestamp(END))]
    if len(panel) < 50:
        raise SystemExit("가격 데이터 부족")

    stock_nav = _stock_nav(panel, capital)
    rows = {}
    for w in STOCK_WEIGHTS:
        nav = stock_nav if w == 1.0 else _blended_nav(stock_nav, w, capital)
        rows[w] = _metrics(nav, capital)
    return {"start": str(panel.index[0].date()), "end": str(panel.index[-1].date()),
            "rf_annual": RF_ANNUAL, "allocations": rows}


def main():
    p = argparse.ArgumentParser(description="주식/현금 정적 배분 분석")
    p.add_argument("--capital", type=float, default=10_000_000)
    args = p.parse_args()
    r = analyze(args.capital)
    print(f"=== 주식+현금 정적 배분 분석 ({r['start']}~{r['end']}, 현금 연 {r['rf_annual']*100:.0f}%) ===")
    print(f"{'주식비중':>8} {'총수익':>8} {'CAGR':>7} {'Sharpe':>7} {'MDD':>8} {'손실연도':>8} {'최악연도':>8}")
    for w, m in r["allocations"].items():
        print(f"{int(w*100):>7}% {m['total_return']:>+7.1f}% {m['cagr']:>+6.1f}% "
              f"{m['sharpe']:>+7.2f} {m['mdd']:>+7.1f}% {m['negative_years']:>4}/{m['total_years']}년 "
              f"{m['worst_year']:>+7.1f}%")
    return r


if __name__ == "__main__":
    main()

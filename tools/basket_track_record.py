#!/usr/bin/env python3
"""분산 대형주 buy&hold 바스켓의 실현 트랙레코드 생성.

수익성 결론(능동 alpha 없음 → 분산 보유가 현실적 고수익)을 날짜별 구체 수치로 증명한다.
실제 과거 종가로 동일비중 buy&hold를 시뮬레이션하고(편도 수수료 0.015% 진입 비용 반영),
월별 평가액·수익률·MDD를 Markdown 리포트로 남긴다.

투명성: 능동 리밸런싱 없이 "초기 매수 후 보유"만 한다(결론대로 회전 최소화). 즉 실행 가능한
최선의 형태다. 벤치마크 트릭 없이 실제 종가만 사용하므로 누구나 재현·검증할 수 있다.

Usage:
    python tools/basket_track_record.py --basket kr_diversified_hold --start 2023-01-01 --end 2025-12-31
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

COMMISSION_RATE = 0.00015  # 편도 수수료 0.015% (config/risk_params.yaml과 동일)


def build_track_record(basket_name, start, end, capital=10_000_000):
    import warnings, logging
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    import pandas as pd
    from core.basket_rebalancer import BasketRebalancer
    from core.data_collector import DataCollector

    baskets = BasketRebalancer._load_baskets_config()
    if basket_name not in baskets:
        raise SystemExit(f"바스켓 '{basket_name}' 없음. 가능: {', '.join(baskets)}")
    holdings = baskets[basket_name].get("holdings", {})

    # 주식 슬리브 비중 — 운영(BasketRebalancer._stock_fraction)과 동일하게
    # target_stock_weight(바스켓 명시 주식 비중)와 min_cash_ratio(현금 하한)를 반영한다.
    # 이걸 무시하면 '주식50/현금50' 바스켓이 주식 100%로 시뮬레이션돼
    # 수익·MDD가 과대 보고된다(트랙레코드는 운영과 같은 조건이어야 정직하다).
    rb = BasketRebalancer(basket_name=basket_name)
    stock_fraction = rb._stock_fraction()

    dc = DataCollector()
    closes = {}
    for sym in holdings:
        df = dc.fetch_stock(sym, start_date=start, end_date=end)
        if df is not None and not df.empty:
            d = df.copy()
            d.index = pd.to_datetime(d["date"]) if "date" in d.columns else pd.to_datetime(d.index)
            closes[sym] = d["close"].astype(float)
    if len(closes) < len(holdings):
        missing = set(holdings) - set(closes)
        raise SystemExit(f"가격 누락: {missing}")

    panel = pd.DataFrame(closes).dropna(how="any")
    if panel.empty or len(panel) < 20:
        raise SystemExit("가격 데이터 부족")

    # 초기 매수: 주식 슬리브(capital*stock_fraction)를 holdings 비중대로 배정, 진입 수수료 차감.
    # 나머지는 현금 보유(운영의 현금 배분과 동일).
    first_px = panel.iloc[0]
    shares, invested, commission_paid = {}, 0.0, 0.0
    for sym, w in holdings.items():
        alloc = capital * stock_fraction * float(w)
        qty = int(alloc / first_px[sym])
        shares[sym] = qty
        cost = qty * first_px[sym]
        invested += cost
        commission_paid += cost * COMMISSION_RATE
    cash = capital - invested - commission_paid

    # 일별 평가액 시계열
    nav = (panel * pd.Series(shares)).sum(axis=1) + cash
    nav_pct = nav / capital

    total_return = (nav.iloc[-1] / capital - 1) * 100
    daily_ret = nav.pct_change().dropna()
    sharpe = (daily_ret.mean() * 252 - 0.03) / (daily_ret.std() * (252 ** 0.5)) if daily_ret.std() > 0 else 0
    peak = nav.cummax()
    mdd = ((nav / peak - 1) * 100).min()
    years = max((panel.index[-1] - panel.index[0]).days / 365.25, 0.1)
    cagr = ((nav.iloc[-1] / capital) ** (1 / years) - 1) * 100

    # 월말 평가액
    monthly = nav.resample("ME").last()
    monthly_rows = [(d.strftime("%Y-%m"), float(v), (v / capital - 1) * 100) for d, v in monthly.items()]

    return {
        "basket": basket_name,
        "display_name": baskets[basket_name].get("name", basket_name),
        "start": str(panel.index[0].date()),
        "end": str(panel.index[-1].date()),
        "capital": capital,
        "final_value": float(nav.iloc[-1]),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "sharpe": round(float(sharpe), 2),
        "mdd_pct": round(float(mdd), 2),
        "entry_commission": round(commission_paid, 0),
        "holdings": holdings,
        "stock_fraction": round(float(stock_fraction), 4),
        "monthly": monthly_rows,
        "years": round(years, 2),
    }


def render_markdown(tr):
    lines = [
        f"# {tr['display_name']} — buy&hold 트랙레코드",
        "",
        f"> 능동 alpha가 없다는 결론에 따라 **초기 동일비중 매수 후 보유**(회전 0)만 한 실현 트랙레코드.",
        f"> 실제 과거 종가 기반, 진입 수수료(0.015%) 반영. 벤치마크 트릭 없이 재현 가능.",
        f"> 생성 기준: {tr['start']} ~ {tr['end']} ({tr['years']}년), 초기자본 {tr['capital']:,.0f}원.",
        "",
        "## 요약",
        "",
        "| 지표 | 값 |",
        "|------|----|",
        f"| 최종 평가액 | {tr['final_value']:,.0f}원 |",
        f"| 총 수익률 | **{tr['total_return_pct']:+.2f}%** |",
        f"| CAGR | **{tr['cagr_pct']:+.2f}%** |",
        f"| Sharpe | {tr['sharpe']} |",
        f"| MDD | {tr['mdd_pct']:.2f}% |",
        f"| 진입 수수료 | {tr['entry_commission']:,.0f}원 |",
        f"| 주식 비중 | {tr['stock_fraction']*100:.0f}% (나머지 현금 — target_stock_weight·min_cash_ratio 반영, 운영과 동일) |",
        f"| 보유 종목 | {len(tr['holdings'])}개 동일비중 |",
        "",
        "## 월별 평가액",
        "",
        "| 월 | 평가액(원) | 누적수익률 |",
        "|----|-----------|-----------|",
    ]
    for ym, v, r in tr["monthly"]:
        lines.append(f"| {ym} | {v:,.0f} | {r:+.1f}% |")
    lines += [
        "",
        "## 종목 구성 (동일비중)",
        "",
        "| 종목 | 비중 |",
        "|------|------|",
    ]
    for sym, w in tr["holdings"].items():
        lines.append(f"| {sym} | {float(w)*100:.0f}% |")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="바스켓 buy&hold 트랙레코드 생성")
    p.add_argument("--basket", default="kr_diversified_hold")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--out", default=None, help="Markdown 출력 경로")
    args = p.parse_args()

    tr = build_track_record(args.basket, args.start, args.end, args.capital)
    md = render_markdown(tr)
    out = args.out or f"reports/track_record_{args.basket}.md"
    out_path = _ROOT / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  총수익률 {tr['total_return_pct']:+.2f}% | CAGR {tr['cagr_pct']:+.2f}% | "
          f"Sharpe {tr['sharpe']} | MDD {tr['mdd_pct']:.2f}%")


if __name__ == "__main__":
    main()

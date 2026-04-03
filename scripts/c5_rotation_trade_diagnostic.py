"""
C-5 Rotation trade-level 손실 구조 진단
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
- MFE/MAE, holding bucket, exit reason, symbol별 분석
- entry 필터 A가 제거한 거래가 손실/수익 거래인지 분석
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester
from core.data_collector import DataCollector

logger.remove()
logger.add(sys.stderr, level="WARNING")

SYMBOLS = ["005930", "000660", "035720", "051910"]
INITIAL_CAPITAL = 10_000_000

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}

PERIODS = {
    "DEV": ("2021-01-01", "2023-12-31"),
    "OOS": ("2024-01-01", "2025-12-31"),
}


def run_rotation(period_name, abs_filter="none"):
    start, end = PERIODS[period_name]
    config = Config.get()
    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["abs_momentum_filter"] = abs_filter
    rot_params["market_filter_sma200"] = False

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt = PortfolioBacktester(config)
        return pbt.run(
            symbols=SYMBOLS,
            strategy_name="relative_strength_rotation",
            initial_capital=INITIAL_CAPITAL,
            start_date=start,
            end_date=end,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v
        rot_params["abs_momentum_filter"] = "none"


def pair_trades(trades):
    """BUY/SELL을 매칭하여 complete trade list 생성."""
    buys = {}  # symbol -> buy trade
    paired = []
    for t in trades:
        if t["action"] == "BUY":
            buys[t["symbol"]] = t
        else:
            buy = buys.pop(t["symbol"], None)
            if buy:
                paired.append({
                    "symbol": t["symbol"],
                    "entry_date": buy["date"],
                    "exit_date": t["date"],
                    "entry_price": buy["price"],
                    "exit_price": t["price"],
                    "quantity": buy["quantity"],
                    "pnl": t["pnl"],
                    "pnl_rate": t["pnl_rate"],
                    "holding_days": t.get("holding_days", 0),
                    "exit_reason": t["action"],
                    "entry_score": buy.get("entry_score", 0),
                    "notional": buy["price"] * buy["quantity"],
                })
    return paired


def compute_mfe_mae(paired, price_data):
    """각 거래에 대해 MFE/MAE를 계산."""
    for t in paired:
        sym = t["symbol"]
        df = price_data.get(sym)
        if df is None:
            t["mfe"] = 0
            t["mae"] = 0
            continue

        entry_d = t["entry_date"]
        exit_d = t["exit_date"]
        entry_p = t["entry_price"]

        mask = (df.index >= entry_d) & (df.index <= exit_d)
        window = df.loc[mask]

        if window.empty or entry_p <= 0:
            t["mfe"] = 0
            t["mae"] = 0
            continue

        highs = window["high"].astype(float) if "high" in window else window["close"].astype(float)
        lows = window["low"].astype(float) if "low" in window else window["close"].astype(float)

        t["mfe"] = round((highs.max() / entry_p - 1) * 100, 2)
        t["mae"] = round((lows.min() / entry_p - 1) * 100, 2)


def bucket_analysis(paired, label):
    """holding_days bucket별 성과."""
    buckets = [
        ("1~5일", 1, 5),
        ("6~10일", 6, 10),
        ("11~20일", 11, 20),
        ("21~30일", 21, 30),
        ("31일+", 31, 9999),
    ]
    print(f"\n  [{label}] Holding Days Bucket 분석")
    print(f"  {'bucket':<10} {'trades':>7} {'win_rate':>10} {'avg_pnl%':>10} {'total_pnl':>12} {'avg_MAE%':>10} {'avg_MFE%':>10}")
    print(f"  {'-'*10} {'-'*7} {'-'*10} {'-'*10} {'-'*12} {'-'*10} {'-'*10}")

    for name, lo, hi in buckets:
        bucket = [t for t in paired if lo <= t["holding_days"] <= hi]
        if not bucket:
            print(f"  {name:<10} {'0':>7}")
            continue
        n = len(bucket)
        wins = sum(1 for t in bucket if t["pnl"] > 0)
        wr = wins / n * 100
        avg_pnl = sum(t["pnl_rate"] for t in bucket) / n
        total_pnl = sum(t["pnl"] for t in bucket)
        avg_mae = sum(t["mae"] for t in bucket) / n
        avg_mfe = sum(t["mfe"] for t in bucket) / n
        print(f"  {name:<10} {n:>7} {wr:>10.1f} {avg_pnl:>10.2f} {total_pnl:>12,.0f} {avg_mae:>10.2f} {avg_mfe:>10.2f}")


def exit_reason_analysis(paired, label):
    """exit reason별 성과."""
    reasons = {}
    for t in paired:
        r = t["exit_reason"]
        reasons.setdefault(r, []).append(t)

    print(f"\n  [{label}] Exit Reason 분석")
    print(f"  {'reason':<16} {'trades':>7} {'win_rate':>10} {'avg_pnl%':>10} {'total_pnl':>12} {'avg_hold':>10}")
    print(f"  {'-'*16} {'-'*7} {'-'*10} {'-'*10} {'-'*12} {'-'*10}")

    for reason in sorted(reasons.keys()):
        bucket = reasons[reason]
        n = len(bucket)
        wins = sum(1 for t in bucket if t["pnl"] > 0)
        wr = wins / n * 100
        avg_pnl = sum(t["pnl_rate"] for t in bucket) / n
        total_pnl = sum(t["pnl"] for t in bucket)
        avg_hold = sum(t["holding_days"] for t in bucket) / n
        print(f"  {reason:<16} {n:>7} {wr:>10.1f} {avg_pnl:>10.2f} {total_pnl:>12,.0f} {avg_hold:>10.1f}")


def symbol_analysis(paired, label):
    """symbol별 손실 구조."""
    by_sym = {}
    for t in paired:
        by_sym.setdefault(t["symbol"], []).append(t)

    print(f"\n  [{label}] Symbol별 분석")
    print(f"  {'symbol':<8} {'trades':>7} {'win_rate':>10} {'total_pnl':>12} {'avg_pnl%':>10} {'avg_hold':>10} {'avg_MAE%':>10} {'avg_MFE%':>10}")
    print(f"  {'-'*8} {'-'*7} {'-'*10} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    for sym in SYMBOLS:
        bucket = by_sym.get(sym, [])
        if not bucket:
            print(f"  {sym:<8} {'0':>7}")
            continue
        n = len(bucket)
        wins = sum(1 for t in bucket if t["pnl"] > 0)
        wr = wins / n * 100
        total_pnl = sum(t["pnl"] for t in bucket)
        avg_pnl = sum(t["pnl_rate"] for t in bucket) / n
        avg_hold = sum(t["holding_days"] for t in bucket) / n
        avg_mae = sum(t["mae"] for t in bucket) / n
        avg_mfe = sum(t["mfe"] for t in bucket) / n
        print(f"  {sym:<8} {n:>7} {wr:>10.1f} {total_pnl:>12,.0f} {avg_pnl:>10.2f} {avg_hold:>10.1f} {avg_mae:>10.2f} {avg_mfe:>10.2f}")


def top_loss_trades(paired, label, n=5):
    """손실 상위 거래."""
    losses = sorted([t for t in paired if t["pnl"] < 0], key=lambda x: x["pnl"])
    print(f"\n  [{label}] 손실 상위 {n}개 거래")
    print(f"  {'symbol':<8} {'entry':>12} {'exit':>12} {'hold':>5} {'pnl_rate%':>10} {'pnl':>12} {'MAE%':>8} {'MFE%':>8} {'exit_reason':<14} {'notional':>10}")
    print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*5} {'-'*10} {'-'*12} {'-'*8} {'-'*8} {'-'*14} {'-'*10}")
    for t in losses[:n]:
        ed = str(t["entry_date"])[:10]
        xd = str(t["exit_date"])[:10]
        print(f"  {t['symbol']:<8} {ed:>12} {xd:>12} {t['holding_days']:>5} "
              f"{t['pnl_rate']:>10.2f} {t['pnl']:>12,.0f} {t['mae']:>8.2f} {t['mfe']:>8.2f} "
              f"{t['exit_reason']:<14} {t['notional']:>10,.0f}")


def top_win_trades(paired, label, n=5):
    """수익 상위 거래."""
    wins = sorted([t for t in paired if t["pnl"] > 0], key=lambda x: -x["pnl"])
    print(f"\n  [{label}] 수익 상위 {n}개 거래")
    print(f"  {'symbol':<8} {'entry':>12} {'exit':>12} {'hold':>5} {'pnl_rate%':>10} {'pnl':>12} {'MAE%':>8} {'MFE%':>8} {'exit_reason':<14}")
    print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*5} {'-'*10} {'-'*12} {'-'*8} {'-'*8} {'-'*14}")
    for t in wins[:n]:
        ed = str(t["entry_date"])[:10]
        xd = str(t["exit_date"])[:10]
        print(f"  {t['symbol']:<8} {ed:>12} {xd:>12} {t['holding_days']:>5} "
              f"{t['pnl_rate']:>10.2f} {t['pnl']:>12,.0f} {t['mae']:>8.2f} {t['mfe']:>8.2f} "
              f"{t['exit_reason']:<14}")


def mfe_mae_summary(paired, label):
    """MFE/MAE 요약 — exit 효율성 진단."""
    if not paired:
        return

    wins = [t for t in paired if t["pnl"] > 0]
    losses = [t for t in paired if t["pnl"] <= 0]

    print(f"\n  [{label}] MFE/MAE 요약")

    if wins:
        avg_mfe_w = sum(t["mfe"] for t in wins) / len(wins)
        avg_realized_w = sum(t["pnl_rate"] for t in wins) / len(wins)
        capture_w = avg_realized_w / avg_mfe_w * 100 if avg_mfe_w > 0 else 0
        print(f"    수익 거래 ({len(wins)}건):")
        print(f"      avg MFE: {avg_mfe_w:.2f}%  avg realized: {avg_realized_w:.2f}%  capture: {capture_w:.0f}%")

    if losses:
        avg_mae_l = sum(t["mae"] for t in losses) / len(losses)
        avg_realized_l = sum(t["pnl_rate"] for t in losses) / len(losses)
        overshoot_l = avg_realized_l / avg_mae_l * 100 if avg_mae_l < 0 else 0
        avg_mfe_l = sum(t["mfe"] for t in losses) / len(losses)
        print(f"    손실 거래 ({len(losses)}건):")
        print(f"      avg MAE: {avg_mae_l:.2f}%  avg realized: {avg_realized_l:.2f}%  overshoot: {overshoot_l:.0f}%")
        print(f"      avg MFE: {avg_mfe_l:.2f}% (손실 전 최대 이익 — exit 타이밍 잠재 개선 여지)")

    # 전체
    all_mae = sum(t["mae"] for t in paired) / len(paired)
    all_mfe = sum(t["mfe"] for t in paired) / len(paired)
    all_pnl = sum(t["pnl_rate"] for t in paired) / len(paired)
    print(f"    전체 ({len(paired)}건):")
    print(f"      avg MAE: {all_mae:.2f}%  avg MFE: {all_mfe:.2f}%  avg realized: {all_pnl:.2f}%")


def sizing_diagnostic(paired, label):
    """sizing 진단 — 고변동 종목에 손실 집중 여부."""
    if not paired:
        return

    print(f"\n  [{label}] Sizing 진단")

    # notional별 분석
    losses = [t for t in paired if t["pnl"] < 0]
    wins = [t for t in paired if t["pnl"] > 0]

    if losses:
        avg_notional_loss = sum(t["notional"] for t in losses) / len(losses)
        avg_notional_win = sum(t["notional"] for t in wins) / len(wins) if wins else 0
        print(f"    평균 notional — 손실: {avg_notional_loss:,.0f}  수익: {avg_notional_win:,.0f}")

        # MAE 기준 고변동 = MAE < -3%
        high_vol_losses = [t for t in losses if t["mae"] < -3]
        low_vol_losses = [t for t in losses if t["mae"] >= -3]
        print(f"    손실 거래 중 MAE < -3% (고변동): {len(high_vol_losses)}건, "
              f"total_pnl: {sum(t['pnl'] for t in high_vol_losses):,.0f}")
        print(f"    손실 거래 중 MAE >= -3% (저변동): {len(low_vol_losses)}건, "
              f"total_pnl: {sum(t['pnl'] for t in low_vol_losses):,.0f}")

        if high_vol_losses:
            avg_not_hv = sum(t["notional"] for t in high_vol_losses) / len(high_vol_losses)
            avg_not_lv = sum(t["notional"] for t in low_vol_losses) / len(low_vol_losses) if low_vol_losses else 0
            print(f"    고변동 손실 avg notional: {avg_not_hv:,.0f} vs 저변동 손실: {avg_not_lv:,.0f}")
            if avg_not_hv > avg_not_lv * 1.2:
                print(f"    → 고변동 종목에 과도한 비중 배분 가능성 있음")
            else:
                print(f"    → notional 차이 미미 — sizing보다 exit 문제일 가능성")


def filter_removed_analysis(paired_base, paired_a, label):
    """필터 A가 제거한 거래가 손실/수익 거래였는지 분석."""
    # 제거된 거래 = base에 있지만 A에 없는 것
    a_keys = {(t["symbol"], str(t["entry_date"])[:10]) for t in paired_a}
    removed = [t for t in paired_base if (t["symbol"], str(t["entry_date"])[:10]) not in a_keys]

    if not removed:
        print(f"\n  [{label}] 필터 A가 제거한 거래: 없음")
        return

    wins = [t for t in removed if t["pnl"] > 0]
    losses = [t for t in removed if t["pnl"] <= 0]

    print(f"\n  [{label}] 필터 A가 제거한 거래 분석 ({len(removed)}건)")
    print(f"    수익 거래: {len(wins)}건, total_pnl: {sum(t['pnl'] for t in wins):,.0f}")
    print(f"    손실 거래: {len(losses)}건, total_pnl: {sum(t['pnl'] for t in losses):,.0f}")
    print(f"    제거 거래 순 PnL: {sum(t['pnl'] for t in removed):,.0f}")

    if sum(t['pnl'] for t in removed) < 0:
        print(f"    → 필터 A가 제거한 거래는 순손실 — 필터 방향은 올바름")
    else:
        print(f"    → 필터 A가 수익 거래도 제거 — 필터가 비효율적")

    for t in removed:
        ed = str(t["entry_date"])[:10]
        tag = "WIN" if t["pnl"] > 0 else "LOSS"
        print(f"      {tag:>4} {t['symbol']:<8} {ed} pnl_rate={t['pnl_rate']:+.2f}% hold={t['holding_days']}d")


if __name__ == "__main__":
    print("=" * 100)
    print("  C-5 Rotation Trade-Level 손실 구조 진단")
    print("=" * 100)

    # 가격 데이터 로드
    collector = DataCollector()
    price_data = {}
    for sym in SYMBOLS:
        df = collector.fetch_stock(sym, "2020-06-01", "2025-12-31")
        if df is not None and not df.empty:
            price_data[sym] = df

    for period in ["DEV", "OOS"]:
        print(f"\n{'='*100}")
        print(f"  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"{'='*100}")

        # baseline
        r_base = run_rotation(period, abs_filter="none")
        paired_base = pair_trades(r_base["trades"])
        compute_mfe_mae(paired_base, price_data)

        # 필터 A (for removed analysis)
        r_a = run_rotation(period, abs_filter="A")
        paired_a = pair_trades(r_a["trades"])

        # ── 전체 요약 ──
        n = len(paired_base)
        wins = sum(1 for t in paired_base if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in paired_base)
        print(f"\n  전체: {n}건, 승: {wins}, 패: {n-wins}, 승률: {wins/n*100:.1f}%")
        print(f"  총 PnL: {total_pnl:,.0f}")

        bucket_analysis(paired_base, period)
        exit_reason_analysis(paired_base, period)
        symbol_analysis(paired_base, period)
        mfe_mae_summary(paired_base, period)
        sizing_diagnostic(paired_base, period)
        top_loss_trades(paired_base, period)
        top_win_trades(paired_base, period)
        filter_removed_analysis(paired_base, paired_a, period)

    # ── 최종 판정 ──
    print(f"\n{'='*100}")
    print(f"  최종 판정")
    print(f"{'='*100}")

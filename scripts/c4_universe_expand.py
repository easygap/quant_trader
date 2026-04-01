"""
breakout_volume 유니버스 확장 검증 스크립트
- Step 1: KR_CORE_5 전 종목 단일종목 honest OOS
- Step 2: 편입 게이트 판정
- Step 3: 편입 종목으로 포트폴리오 OOS + 신호 밀도 진단
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from collections import Counter, defaultdict

from config.config_loader import Config
from core.data_collector import DataCollector
from strategies.breakout_volume import BreakoutVolumeStrategy
from core.risk_manager import RiskManager
from backtest.backtester import Backtester

# ── 설정 ──
KR_CORE_5 = ["005930", "000660", "035720", "051910", "006400"]
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스", "035720": "카카오",
         "051910": "LG화학", "006400": "삼성SDI"}
DEV_START, DEV_END = "2021-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2025-12-31"

config = Config.get()
dc = DataCollector(config)
rm = RiskManager(config)

# ============================================================
# STEP 1: 단일종목 honest OOS
# ============================================================
print("=" * 70)
print("  STEP 1: KR_CORE_5 단일종목 honest OOS (frozen params)")
print("  breakout_period=10, surge_ratio=1.5, adx_min=20")
print("=" * 70)

bt = Backtester(config)
oos_results = {}

for sym in KR_CORE_5:
    print(f"\n--- {sym} ({NAMES[sym]}) ---")

    # OOS 구간 데이터로 백테스트
    try:
        df_oos = dc.fetch_stock(sym, start_date=OOS_START, end_date=OOS_END)
        if df_oos.empty or len(df_oos) < 30:
            print(f"  데이터 부족: {len(df_oos)}행 → SKIP")
            oos_results[sym] = {"status": "SKIP", "reason": "데이터 부족"}
            continue

        result = bt.run(
            df_oos,
            strategy_name="breakout_volume",
            strict_lookahead=True,
            notify_overtrading=False,
        )

        if result is None:
            print(f"  백테스트 실패 → SKIP")
            oos_results[sym] = {"status": "SKIP", "reason": "백테스트 실패"}
            continue

        metrics = result.get("metrics", {})
        trades = result.get("trades", [])

        # 매도 거래만 카운트
        sell_trades = [t for t in trades if t.get("action") != "BUY"]
        total_trades = len(sell_trades)
        wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sum(t.get("pnl", 0) for t in sell_trades)
        total_return = metrics.get("total_return", 0)
        sharpe = metrics.get("sharpe_ratio", 0)
        mdd = metrics.get("max_drawdown", 0)

        # 단일 이벤트 의존 검사: 최대 단일 거래 PnL이 전체의 80% 이상이면 flag
        max_single_pnl = max((t.get("pnl", 0) for t in sell_trades), default=0)
        single_event_flag = (total_pnl > 0 and max_single_pnl / total_pnl > 0.8) if total_pnl > 0 else False

        oos_results[sym] = {
            "status": "OK",
            "total_trades": total_trades,
            "total_return": total_return,
            "sharpe": sharpe,
            "mdd": mdd,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "single_event_flag": single_event_flag,
            "max_single_pnl": max_single_pnl,
            "max_single_pnl_pct": max_single_pnl / total_pnl * 100 if total_pnl > 0 else 0,
        }

        print(f"  trades={total_trades}  return={total_return:.2f}%  "
              f"sharpe={sharpe:.2f}  MDD={mdd:.2f}%  win_rate={win_rate:.1f}%  "
              f"single_event={'YES' if single_event_flag else 'no'}")

    except Exception as e:
        print(f"  오류: {e}")
        oos_results[sym] = {"status": "ERROR", "reason": str(e)}

# ============================================================
# STEP 2: 편입 게이트 판정
# ============================================================
print("\n" + "=" * 70)
print("  STEP 2: 편입 게이트 판정")
print("=" * 70)

GATE_RETURN = 0       # total_return >= 0
GATE_MDD = -6         # MDD > -6%
GATE_TRADES = 6       # total_trades >= 6

admitted = []
for sym in KR_CORE_5:
    r = oos_results[sym]
    if r["status"] != "OK":
        print(f"  {sym} ({NAMES[sym]}): SKIP ({r.get('reason', 'N/A')})")
        continue

    pass_return = r["total_return"] >= GATE_RETURN
    pass_mdd = r["mdd"] > GATE_MDD
    pass_trades = r["total_trades"] >= GATE_TRADES
    pass_single = not r["single_event_flag"]

    verdict = "PASS" if (pass_return and pass_mdd and pass_trades and pass_single) else "FAIL"

    reasons = []
    if not pass_return: reasons.append(f"return={r['total_return']:.2f}%<0")
    if not pass_mdd: reasons.append(f"MDD={r['mdd']:.2f}%<-6%")
    if not pass_trades: reasons.append(f"trades={r['total_trades']}<6")
    if not pass_single: reasons.append(f"single_event={r['max_single_pnl_pct']:.0f}%")

    print(f"  {sym} ({NAMES[sym]}): {verdict}"
          + (f"  [{', '.join(reasons)}]" if reasons else ""))

    if verdict == "PASS":
        admitted.append(sym)

print(f"\n  편입 종목: {admitted} ({len(admitted)}개)")

if len(admitted) < 2:
    print("\n  편입 종목 2개 미만 → 포트폴리오 구성 불가")
    sys.exit(0)

# ============================================================
# STEP 3: 편입 종목 포트폴리오 OOS + 신호 밀도 진단
# ============================================================
print("\n" + "=" * 70)
print(f"  STEP 3: 포트폴리오 OOS ({len(admitted)}종목)")
print("=" * 70)

from backtest.portfolio_backtester import PortfolioBacktester

pbt = PortfolioBacktester(config)
portfolio_result = pbt.run(
    symbols=admitted,
    strategy_name="breakout_volume",
    start_date=OOS_START,
    end_date=OOS_END,
)

if portfolio_result:
    pbt.print_report(portfolio_result)

# ── 신호 밀도 진단 ──
print("\n" + "=" * 70)
print("  신호 밀도 진단 (3종목 vs 확장)")
print("=" * 70)

strategy = BreakoutVolumeStrategy(config)
signals = {}
for sym in admitted:
    df = dc.fetch_stock(sym, start_date=OOS_START, end_date=OOS_END)
    analyzed = strategy.analyze(df)
    signals[sym] = analyzed

all_dates = sorted(set.union(*[set(signals[s].index) for s in admitted]))
total_days = len(all_dates)

daily_buy_counts = []
daily_positions_count = []
positions_sim = {}

# 간단 시뮬레이션으로 days_in_market 측정
risk_params = config.risk_params
sl_config = risk_params.get("stop_loss", {})
sl_type = sl_config.get("type", "fixed")
sl_rate = sl_config.get("fixed_rate", 0.03)
atr_mult = sl_config.get("atr_multiplier", 2.0)
tp_rate = risk_params.get("take_profit", {}).get("fixed_rate", 0.08)
ts_enabled = risk_params.get("trailing_stop", {}).get("enabled", False)
ts_fixed_rate = risk_params.get("trailing_stop", {}).get("fixed_rate", 0.05)
ts_type = risk_params.get("trailing_stop", {}).get("type", "fixed")
ts_atr_mult = risk_params.get("trailing_stop", {}).get("atr_multiplier", 3.0)
max_hold = risk_params.get("position_limits", {}).get("max_holding_days", 0)

def stop_loss_price(avg_price, row_atr):
    if sl_type == "atr" and row_atr is not None and row_atr > 0:
        return avg_price - row_atr * atr_mult
    return avg_price * (1 - sl_rate)

def trailing_stop_price(hwm, row_atr):
    if not ts_enabled or hwm <= 0:
        return None
    if ts_type == "atr" and row_atr is not None:
        return hwm - row_atr * ts_atr_mult
    return hwm * (1 - ts_fixed_rate)

cash = 10_000_000
positions = {}
div_cfg = risk_params.get("diversification", {})
max_positions = div_cfg.get("max_positions", 10)
max_position_ratio = div_cfg.get("max_position_ratio", 0.20)
max_investment_ratio = div_cfg.get("max_investment_ratio", 0.70)
min_cash_ratio = div_cfg.get("min_cash_ratio", 0.20)
max_risk_per_trade = risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)

per_sym_exposure_days = defaultdict(int)

for date in all_dates:
    # count buy candidates
    n_buy = 0
    for sym in admitted:
        if date in signals[sym].index and signals[sym].loc[date, "signal"] == "BUY":
            n_buy += 1
    daily_buy_counts.append(n_buy)

    # sell
    to_sell = []
    for sym in list(positions.keys()):
        if date not in signals[sym].index:
            continue
        row = signals[sym].loc[date]
        close = float(row["close"])
        pos = positions[sym]
        pos["hwm"] = max(pos["hwm"], close)
        row_atr = float(row.get("atr", 0)) if pd.notna(row.get("atr")) and row.get("atr", 0) > 0 else None

        sell_reason = None
        if max_hold > 0 and hasattr(date, "date"):
            hd = (date - pos["buy_date"]).days
            if hd >= max_hold:
                sell_reason = "MAX_HOLD"
        if not sell_reason and close <= stop_loss_price(pos["avg_price"], row_atr):
            sell_reason = "STOP_LOSS"
        if not sell_reason and close >= pos["avg_price"] * (1 + tp_rate):
            sell_reason = "TAKE_PROFIT"
        ts_p = trailing_stop_price(pos["hwm"], row_atr)
        if not sell_reason and ts_p is not None and close <= ts_p:
            sell_reason = "TRAILING_STOP"
        if not sell_reason and row.get("signal") == "SELL":
            sell_reason = "SELL"

        if sell_reason:
            to_sell.append((sym, close))

    for sym, close in to_sell:
        pos = positions.pop(sym)
        costs = rm.calculate_transaction_costs(close, pos["qty"], "SELL", avg_price=pos["avg_price"])
        sell_price = costs["execution_price"]
        cash += sell_price * pos["qty"] - costs["commission"] - costs["tax"]

    # buy
    buy_cands = []
    for sym in admitted:
        if sym in positions:
            continue
        if date not in signals[sym].index:
            continue
        row = signals[sym].loc[date]
        if row.get("signal") == "BUY":
            score = float(row.get("strategy_score", 0))
            buy_cands.append((sym, float(row["close"]), score))
    buy_cands.sort(key=lambda x: -x[2])

    for sym, close, score in buy_cands:
        pos_value = sum(
            (float(signals[s].loc[date, "close"]) if date in signals[s].index else positions[s]["avg_price"]) * positions[s]["qty"]
            for s in positions
        )
        total_equity = cash + pos_value
        if total_equity <= 0 or len(positions) >= max_positions:
            break
        invested = pos_value
        if total_equity > 0 and invested / total_equity >= max_investment_ratio:
            break
        if total_equity > 0 and cash / total_equity < min_cash_ratio:
            break

        max_invest = total_equity * max_position_ratio
        row_data = signals[sym].loc[date]
        buy_atr = float(row_data.get("atr", 0)) if pd.notna(row_data.get("atr")) and row_data.get("atr", 0) > 0 else None
        stop_at = stop_loss_price(close, buy_atr)
        risk_per_share = max(close - stop_at, close * 0.001)
        risk_amount = total_equity * max_risk_per_trade
        qty = min(int(risk_amount / risk_per_share), int(max_invest / close) if close > 0 else 0)
        scale = rm._signal_scale(score)
        qty = int(qty * scale)
        if qty <= 0 or close * qty > cash * 0.95:
            continue
        costs = rm.calculate_transaction_costs(close, qty, "BUY")
        buy_price = costs["execution_price"]
        total_cost = buy_price * qty + costs["commission"]
        if total_cost > cash:
            continue
        cash -= total_cost
        positions[sym] = {"qty": qty, "avg_price": buy_price, "buy_date": date, "hwm": buy_price}

    n_pos = len(positions)
    daily_positions_count.append(n_pos)
    for s in positions:
        per_sym_exposure_days[s] += 1

days_with_buy = sum(1 for c in daily_buy_counts if c > 0)
days_in_market = sum(1 for p in daily_positions_count if p > 0)
avg_pos = np.mean(daily_positions_count)
max_concurrent = max(daily_positions_count) if daily_positions_count else 0

buy_dist = Counter(daily_buy_counts)

print(f"\n  총 거래일: {total_days}")
print(f"\n  날짜별 동시 BUY candidate 분포:")
for k in sorted(buy_dist.keys()):
    print(f"    {k}종목: {buy_dist[k]}일 ({buy_dist[k]/total_days*100:.1f}%)")

print(f"\n  BUY 있는 날: {days_with_buy}일 ({days_with_buy/total_days*100:.1f}%)")
print(f"  days_in_market: {days_in_market}일 ({days_in_market/total_days*100:.1f}%)")
print(f"  avg_positions: {avg_pos:.2f}")
print(f"  max_concurrent_positions: {max_concurrent}")

print(f"\n  종목별 노출일:")
for sym in admitted:
    d = per_sym_exposure_days.get(sym, 0)
    print(f"    {sym} ({NAMES[sym]}): {d}일 ({d/total_days*100:.1f}%)")

# ── 비교 요약 ──
print("\n" + "=" * 70)
print("  3종목 → 확장 비교")
print("=" * 70)
print(f"  {'지표':<30} {'3종목':>10} {'확장({0}종목)':>10}".format(len(admitted)))
print(f"  {'-'*30} {'-'*10} {'-'*10}")
print(f"  {'BUY 있는 날':.<30} {'29일':>10} {f'{days_with_buy}일':>10}")
print(f"  {'BUY 있는 날 비율':.<30} {'6.0%':>10} {f'{days_with_buy/total_days*100:.1f}%':>10}")
print(f"  {'days_in_market':.<30} {'45일(9.3%)':>10} {f'{days_in_market}일({days_in_market/total_days*100:.1f}%)':>10}")
print(f"  {'avg_positions':.<30} {'0.10':>10} {f'{avg_pos:.2f}':>10}")
print(f"  {'max_concurrent':.<30} {'2':>10} {f'{max_concurrent}':>10}")

print("\n" + "=" * 70)
print("  완료")
print("=" * 70)

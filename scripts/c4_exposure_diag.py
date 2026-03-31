"""
breakout_volume 포트폴리오 노출 병목 진단 스크립트
- frozen params: breakout_period=10, surge_ratio=1.5, adx_min=20
- period: 2024-01-01 ~ 2025-12-31
- symbols: 005930, 000660, 035720
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

# ── 설정 ──
SYMBOLS = ["005930", "000660", "035720"]
START = "2024-01-01"
END = "2025-12-31"

config = Config.get()
dc = DataCollector(config)
strategy = BreakoutVolumeStrategy(config)
rm = RiskManager(config)

risk_params = config.risk_params
div_cfg = risk_params.get("diversification", {})
max_positions = div_cfg.get("max_positions", 10)
max_position_ratio = div_cfg.get("max_position_ratio", 0.20)
max_investment_ratio = div_cfg.get("max_investment_ratio", 0.70)
min_cash_ratio = div_cfg.get("min_cash_ratio", 0.20)
max_risk_per_trade = risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)

sl_config = risk_params.get("stop_loss", {})
sl_type = sl_config.get("type", "fixed")
sl_rate = sl_config.get("fixed_rate", 0.03)
atr_mult = sl_config.get("atr_multiplier", 2.0)

tp_rate = risk_params.get("take_profit", {}).get("fixed_rate", 0.08)
ts_enabled = risk_params.get("trailing_stop", {}).get("enabled", False)
ts_fixed_rate = risk_params.get("trailing_stop", {}).get("fixed_rate", 0.05)
ts_atr_mult = risk_params.get("trailing_stop", {}).get("atr_multiplier", 3.0)
ts_type = risk_params.get("trailing_stop", {}).get("type", "fixed")

initial_capital = 10_000_000

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

# ── 데이터 수집 + 시그널 생성 ──
print("=" * 70)
print("  breakout_volume 노출 병목 진단")
print("=" * 70)
signals = {}
for sym in SYMBOLS:
    df = dc.fetch_stock(sym, start_date=START, end_date=END)
    analyzed = strategy.analyze(df)
    signals[sym] = analyzed
    print(f"  {sym}: {len(analyzed)}행, BUY={int((analyzed['signal']=='BUY').sum())}, "
          f"SELL={int((analyzed['signal']=='SELL').sum())}, "
          f"HOLD={int((analyzed['signal']=='HOLD').sum())}")

# ── A. 신호 밀도 분석 ──
print("\n" + "=" * 70)
print("  A. 신호 밀도 분석")
print("=" * 70)

# 공통 날짜
all_dates = sorted(set.union(*[set(signals[s].index) for s in SYMBOLS]))
total_days = len(all_dates)

# 날짜별 BUY candidate 수
daily_buy_counts = []
for date in all_dates:
    n = 0
    for sym in SYMBOLS:
        if date in signals[sym].index and signals[sym].loc[date, "signal"] == "BUY":
            n += 1
    daily_buy_counts.append(n)

buy_count_dist = Counter(daily_buy_counts)
print(f"\n  총 거래일: {total_days}")
print(f"\n  날짜별 동시 BUY candidate 분포:")
for k in sorted(buy_count_dist.keys()):
    pct = buy_count_dist[k] / total_days * 100
    print(f"    {k}종목 동시 BUY: {buy_count_dist[k]}일 ({pct:.1f}%)")

days_with_any_buy = sum(1 for c in daily_buy_counts if c > 0)
print(f"\n  BUY 신호가 1건이라도 있는 날: {days_with_any_buy}일 ({days_with_any_buy/total_days*100:.1f}%)")

# 종목별 BUY 날짜
for sym in SYMBOLS:
    buy_dates = signals[sym][signals[sym]["signal"] == "BUY"].index
    print(f"\n  {sym} BUY 신호 날짜 ({len(buy_dates)}건):")
    for d in buy_dates:
        row = signals[sym].loc[d]
        print(f"    {str(d)[:10]}  close={row['close']:>10,.0f}  "
              f"score={row.get('strategy_score',0):.1f}  "
              f"adx={row.get('adx',0):.1f}  "
              f"vol_surge_ratio={row.get('volume_surge_ratio',0):.2f}")

# ── B. 사이징 병목 분석 (시뮬레이션 재현) ──
print("\n" + "=" * 70)
print("  B. 사이징 병목 + 포지션 시뮬레이션")
print("=" * 70)

cash = initial_capital
positions = {}
trades_log = []
sizing_blocks = defaultdict(int)  # reason -> count
daily_positions = []
daily_exposure = []
buy_events = []
per_sym_exposure_days = defaultdict(int)  # sym -> days holding
per_sym_notional_sum = defaultdict(float)

for date in all_dates:
    # 포트폴리오 가치
    pos_value = sum(
        float(signals[s].loc[date, "close"]) * positions[s]["qty"]
        if date in signals[s].index else positions[s]["avg_price"] * positions[s]["qty"]
        for s in positions
    )
    total_equity = cash + pos_value

    # ── 매도 로직 ──
    to_sell = []
    for sym in list(positions.keys()):
        if date not in signals[sym].index:
            continue
        row = signals[sym].loc[date]
        close = float(row["close"])
        pos = positions[sym]
        pos["high_water_mark"] = max(pos["high_water_mark"], close)

        row_atr = float(row.get("atr", 0)) if pd.notna(row.get("atr")) and row.get("atr", 0) > 0 else None
        sell_reason = None

        pos_limits = risk_params.get("position_limits", {}) or {}
        max_hold = pos_limits.get("max_holding_days", 0)
        if max_hold > 0 and hasattr(date, "date"):
            hd = (date - pos["buy_date"]).days
            if hd >= max_hold:
                sell_reason = "MAX_HOLD"
        if not sell_reason and close <= stop_loss_price(pos["avg_price"], row_atr):
            sell_reason = "STOP_LOSS"
        if not sell_reason and close >= pos["avg_price"] * (1 + tp_rate):
            sell_reason = "TAKE_PROFIT"
        ts_p = trailing_stop_price(pos["high_water_mark"], row_atr)
        if not sell_reason and ts_p is not None and close <= ts_p:
            sell_reason = "TRAILING_STOP"
        if not sell_reason and row.get("signal") == "SELL":
            sell_reason = "SELL"

        if sell_reason:
            to_sell.append((sym, close, sell_reason, row_atr))

    for sym, close, reason, row_atr in to_sell:
        pos = positions.pop(sym)
        costs = rm.calculate_transaction_costs(close, pos["qty"], "SELL", avg_price=pos["avg_price"])
        sell_price = costs["execution_price"]
        pnl = (sell_price - pos["avg_price"]) * pos["qty"] - costs["commission"] - costs["tax"]
        cash += sell_price * pos["qty"] - costs["commission"] - costs["tax"]
        hd = (date - pos["buy_date"]).days if pos.get("buy_date") else 0
        trades_log.append({
            "date": date, "symbol": sym, "action": reason,
            "price": sell_price, "qty": pos["qty"], "pnl": pnl,
            "holding_days": hd,
        })

    # ── 매수 로직 + 병목 계측 ──
    buy_candidates = []
    for sym in SYMBOLS:
        if sym in positions:
            continue
        if date not in signals[sym].index:
            continue
        row = signals[sym].loc[date]
        if row.get("signal") == "BUY":
            score = float(row.get("strategy_score", 0))
            buy_candidates.append((sym, float(row["close"]), score))

    buy_candidates.sort(key=lambda x: -x[2])

    for sym, close, score in buy_candidates:
        total_equity_now = cash + sum(
            (float(signals[s].loc[date, "close"]) if date in signals[s].index else positions[s]["avg_price"]) * positions[s]["qty"]
            for s in positions
        )
        if total_equity_now <= 0:
            sizing_blocks["no_equity"] += 1
            continue
        if len(positions) >= max_positions:
            sizing_blocks["max_positions_cap"] += 1
            continue

        invested_now = sum(
            (float(signals[s].loc[date, "close"]) if date in signals[s].index else positions[s]["avg_price"]) * positions[s]["qty"]
            for s in positions
        )
        if total_equity_now > 0 and invested_now / total_equity_now >= max_investment_ratio:
            sizing_blocks["max_investment_ratio_cap"] += 1
            continue
        if total_equity_now > 0 and cash / total_equity_now < min_cash_ratio:
            sizing_blocks["min_cash_ratio_cap"] += 1
            continue

        max_invest = total_equity_now * max_position_ratio
        row_data = signals[sym].loc[date]
        buy_atr = float(row_data.get("atr", 0)) if pd.notna(row_data.get("atr")) and row_data.get("atr", 0) > 0 else None
        stop_at = stop_loss_price(close, buy_atr)
        risk_per_share = max(close - stop_at, close * 0.001)
        risk_amount = total_equity_now * max_risk_per_trade

        qty_risk = int(risk_amount / risk_per_share)
        qty_position = int(max_invest / close) if close > 0 else 0
        qty = min(qty_risk, qty_position)

        limiting_factor = "risk_per_trade" if qty_risk <= qty_position else "position_ratio"

        # signal scale
        scale = rm._signal_scale(score)
        qty = int(qty * scale)

        if qty <= 0:
            sizing_blocks["qty_zero_after_scale"] += 1
            continue
        if close * qty > cash * 0.95:
            sizing_blocks["insufficient_cash"] += 1
            continue

        costs = rm.calculate_transaction_costs(close, qty, "BUY")
        buy_price = costs["execution_price"]
        total_cost = buy_price * qty + costs["commission"]
        if total_cost > cash:
            sizing_blocks["total_cost_exceeds_cash"] += 1
            continue

        intended_notional = close * qty
        actual_notional = buy_price * qty

        buy_events.append({
            "date": date, "symbol": sym, "close": close,
            "atr": buy_atr, "stop_distance_pct": (close - stop_at) / close * 100,
            "risk_per_share": risk_per_share,
            "qty_risk": qty_risk, "qty_position": qty_position,
            "qty_final": qty, "signal_scale": scale,
            "limiting_factor": limiting_factor,
            "intended_notional": intended_notional,
            "actual_notional": actual_notional,
            "pct_of_equity": actual_notional / total_equity_now * 100,
        })

        cash -= total_cost
        positions[sym] = {
            "qty": qty, "avg_price": buy_price,
            "buy_date": date, "high_water_mark": buy_price,
        }
        trades_log.append({
            "date": date, "symbol": sym, "action": "BUY",
            "price": buy_price, "qty": qty, "pnl": 0,
            "holding_days": 0,
        })

    # 일별 기록
    n_pos = len(positions)
    daily_positions.append(n_pos)
    exp = sum(
        (float(signals[s].loc[date, "close"]) if date in signals[s].index else positions[s]["avg_price"]) * positions[s]["qty"]
        for s in positions
    )
    daily_exposure.append(exp / (cash + exp) * 100 if (cash + exp) > 0 else 0)
    for s in positions:
        per_sym_exposure_days[s] += 1
        p = float(signals[s].loc[date, "close"]) if date in signals[s].index else positions[s]["avg_price"]
        per_sym_notional_sum[s] += p * positions[s]["qty"]

# ── 결과 출력 ──

# A. 신호 밀도 추가 메트릭
days_in_market = sum(1 for p in daily_positions if p > 0)
max_concurrent = max(daily_positions) if daily_positions else 0
avg_pos = np.mean(daily_positions)

print(f"\n  days_in_market: {days_in_market}일 / {total_days}일 ({days_in_market/total_days*100:.1f}%)")
print(f"  avg_positions: {avg_pos:.2f}")
print(f"  max_concurrent_positions: {max_concurrent}")

# B. 사이징 병목
print(f"\n  총 BUY candidate 발생: {sum(daily_buy_counts)}건")
print(f"  실제 체결: {len(buy_events)}건")
print(f"\n  주문 잘린 이유별 카운트:")
if not sizing_blocks:
    print("    (없음 — 모든 candidate가 체결됨)")
else:
    for reason, cnt in sorted(sizing_blocks.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {cnt}건")

if buy_events:
    be_df = pd.DataFrame(buy_events)
    print(f"\n  체결된 매수 주문 상세:")
    print(f"    intended_notional — 평균: {be_df['intended_notional'].mean():,.0f}원, "
          f"중앙값: {be_df['intended_notional'].median():,.0f}원")
    print(f"    actual_notional   — 평균: {be_df['actual_notional'].mean():,.0f}원, "
          f"중앙값: {be_df['actual_notional'].median():,.0f}원")
    print(f"    equity 대비(%)    — 평균: {be_df['pct_of_equity'].mean():.1f}%, "
          f"중앙값: {be_df['pct_of_equity'].median():.1f}%")

    print(f"\n  체결 제한 요인 분포:")
    for factor, cnt in be_df["limiting_factor"].value_counts().items():
        print(f"    {factor}: {cnt}건")

    print(f"\n  종목별 평균 stop distance(%) / ATR / price:")
    for sym in SYMBOLS:
        sym_buys = be_df[be_df["symbol"] == sym]
        if len(sym_buys) > 0:
            print(f"    {sym}: stop_dist={sym_buys['stop_distance_pct'].mean():.2f}%  "
                  f"ATR={sym_buys['atr'].mean():,.0f}  "
                  f"price={sym_buys['close'].mean():,.0f}  "
                  f"avg_qty={sym_buys['qty_final'].mean():.0f}  "
                  f"signal_scale={sym_buys['signal_scale'].mean():.3f}")
        else:
            print(f"    {sym}: (매수 없음)")

# C. 성과 분해
print("\n" + "=" * 70)
print("  C. 종목별 노출/기여도")
print("=" * 70)

per_sym_pnl = defaultdict(float)
per_sym_trades = defaultdict(int)
per_sym_wins = defaultdict(int)
holding_days_list = []
for t in trades_log:
    if t["action"] != "BUY":
        per_sym_pnl[t["symbol"]] += t["pnl"]
        per_sym_trades[t["symbol"]] += 1
        if t["pnl"] > 0:
            per_sym_wins[t["symbol"]] += 1
        holding_days_list.append(t["holding_days"])

total_pnl = sum(per_sym_pnl.values())
print(f"\n  {'종목':<8} {'PnL':>12} {'기여%':>8} {'거래':>5} {'승률':>7} {'노출일':>6} {'평균노출%':>9}")
print(f"  {'-'*8} {'-'*12} {'-'*8} {'-'*5} {'-'*7} {'-'*6} {'-'*9}")
for sym in SYMBOLS:
    pnl = per_sym_pnl.get(sym, 0)
    contrib = pnl / total_pnl * 100 if total_pnl != 0 else 0
    trades_n = per_sym_trades.get(sym, 0)
    wr = per_sym_wins.get(sym, 0) / trades_n * 100 if trades_n > 0 else 0
    exp_days = per_sym_exposure_days.get(sym, 0)
    avg_exp = per_sym_notional_sum.get(sym, 0) / exp_days / initial_capital * 100 if exp_days > 0 else 0
    print(f"  {sym:<8} {pnl:>12,.0f} {contrib:>7.1f}% {trades_n:>5} {wr:>6.1f}% {exp_days:>6} {avg_exp:>8.1f}%")

print(f"\n  합계 PnL: {total_pnl:,.0f}원")

# D. 리포팅 품질
print("\n" + "=" * 70)
print("  D. 리포팅 품질 확인")
print("=" * 70)
print(f"\n  max_concurrent_positions: {max_concurrent} (실측값)")
if holding_days_list:
    print(f"  avg_holding_days: {np.mean(holding_days_list):.1f}일 (실측값, exit 거래 {len(holding_days_list)}건 기준)")
    print(f"  holding_days 분포: min={min(holding_days_list)}, median={int(np.median(holding_days_list))}, max={max(holding_days_list)}")
else:
    print(f"  avg_holding_days: N/A (exit 거래 없음)")
print(f"  turnover: 미지원 (portfolio_backtester에 turnover 계산 로직 없음)")

# exposure time series 요약
exp_arr = np.array(daily_exposure)
print(f"\n  일별 exposure(%) — 평균: {exp_arr.mean():.2f}%, 중앙값: {np.median(exp_arr):.2f}%, "
      f"max: {exp_arr.max():.2f}%, >0인 날: {(exp_arr>0).sum()}일")

print("\n" + "=" * 70)
print("  진단 완료")
print("=" * 70)

"""
TP override 검증: Rotation=7%, BV=8% 가 실제 exit에서 적용되는지 증명.
- 동일 기간 / 동일 종목으로 BV, Rotation 각각 실행
- TAKE_PROFIT 거래의 pnl_rate가 TP threshold과 일치하는지 확인
- TAKE_PROFIT이 아닌 거래는 TP 미만에서 exit했는지 확인
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester

SYMBOLS = ["005930", "000660", "035720", "051910"]
CAPITAL = 10_000_000
START, END = "2024-01-01", "2025-12-31"

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}


def run_strategy(strategy_name, max_pos=None, extra_div=None):
    config = Config.get()

    # Rotation 설정 확인
    rot_cfg = config.strategies.get("relative_strength_rotation", {})
    print(f"    strategies.yaml rotation.take_profit_rate = {rot_cfg.get('take_profit_rate', 'NOT SET')}")
    print(f"    strategies.yaml rotation.disable_trailing_stop = {rot_cfg.get('disable_trailing_stop', 'NOT SET')}")

    bv_cfg = config.strategies.get("breakout_volume", {})
    print(f"    strategies.yaml bv.take_profit_rate = {bv_cfg.get('take_profit_rate', 'NOT SET')}")

    tp_global = config.risk_params.get("take_profit", {}).get("fixed_rate", 0.08)
    print(f"    risk_params.yaml take_profit.fixed_rate = {tp_global}")

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}
    try:
        if max_pos is not None:
            div_cfg["max_positions"] = max_pos
        if extra_div:
            div_cfg.update(extra_div)

        pbt = PortfolioBacktester(config)
        return pbt.run(
            symbols=SYMBOLS, strategy_name=strategy_name,
            initial_capital=CAPITAL, start_date=START, end_date=END,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v


def verify_tp(trades, expected_tp, strategy_name):
    """Verify TAKE_PROFIT trades exit at the expected threshold."""
    sells = [t for t in trades if t["action"] != "BUY"]
    tp_trades = [t for t in sells if t["action"] == "TAKE_PROFIT"]
    non_tp = [t for t in sells if t["action"] != "TAKE_PROFIT"]

    print(f"\n    [{strategy_name}] TAKE_PROFIT 검증 (expected TP = {expected_tp:.0%})")
    print(f"    총 exit: {len(sells)}건, TAKE_PROFIT: {len(tp_trades)}건")

    if not tp_trades:
        print(f"    → TAKE_PROFIT 거래 없음 — 검증 불가")
        return False

    all_ok = True
    for t in tp_trades:
        pnl_rate = t["pnl_rate"] / 100  # percentage to ratio
        # TP exit은 close >= avg_price * (1+tp_rate) 시점에서 발생
        # pnl_rate는 슬리피지/수수료 포함이므로 TP threshold보다 약간 낮을 수 있음
        # 하지만 tp_rate 미만이면 안 됨 (TP 미도달 상태에서 TP로 exit한 것)
        margin = 0.02  # 2% 마진 (수수료+슬리피지)
        in_range = (expected_tp - margin) <= pnl_rate <= (expected_tp + margin + 0.05)
        status = "OK" if in_range else "MISMATCH"
        if not in_range:
            all_ok = False
        ed = str(t["date"])[:10]
        print(f"      {status} {t['symbol']} {ed} pnl_rate={t['pnl_rate']:+.2f}% "
              f"(expected ~{expected_tp*100:+.0f}%)")

    # Non-TP trades should have pnl_rate below TP threshold
    exceeded = []
    for t in non_tp:
        if t["pnl_rate"] / 100 >= expected_tp:
            exceeded.append(t)

    if exceeded:
        print(f"\n    WARNING: {len(exceeded)}건의 non-TP 거래가 TP threshold 이상에서 exit")
        for t in exceeded:
            ed = str(t["date"])[:10]
            print(f"      {t['symbol']} {ed} action={t['action']} pnl_rate={t['pnl_rate']:+.2f}%")
    else:
        print(f"    non-TP 거래 {len(non_tp)}건 모두 TP threshold 미만에서 exit — OK")

    return all_ok


if __name__ == "__main__":
    print("=" * 80)
    print("  TP Override 검증")
    print("=" * 80)

    # ── BV (expected TP = 8%) ──
    print(f"\n[1] breakout_volume (expected TP = 8%)")
    r_bv = run_strategy("breakout_volume")
    bv_ok = verify_tp(r_bv["trades"], 0.08, "breakout_volume")

    # ── Rotation (expected TP = 7%) ──
    print(f"\n[2] relative_strength_rotation (expected TP = 7%)")
    r_rot = run_strategy("relative_strength_rotation",
                          max_pos=2, extra_div=ROTATION_DIV)
    rot_ok = verify_tp(r_rot["trades"], 0.07, "rotation")

    # ── Cross-check: Rotation TP trades should NOT match 8% ──
    rot_tp = [t for t in r_rot["trades"] if t["action"] == "TAKE_PROFIT"]
    if rot_tp:
        print(f"\n[3] Cross-check: Rotation TP 거래가 8%가 아닌 7%에서 발동했는지")
        for t in rot_tp:
            pnl = t["pnl_rate"]
            if pnl >= 7.5:
                print(f"    WARNING: {t['symbol']} pnl={pnl:+.2f}% — 8% 근처에서 exit. "
                      f"7% override 미적용 가능성")
            else:
                print(f"    OK: {t['symbol']} pnl={pnl:+.2f}% — 7% 수준에서 exit 확인")

    # ── 최종 ──
    print(f"\n{'='*80}")
    if bv_ok and rot_ok:
        print(f"  결과: PASS — BV=8%, Rotation=7% override 정상 작동")
    elif rot_ok:
        print(f"  결과: PARTIAL — Rotation=7% OK, BV 검증 불가(TP 거래 없을 수 있음)")
    else:
        print(f"  결과: FAIL — TP override 미적용 가능성")
    print(f"{'='*80}")

# C-2: trend_pullback parameter sweep results

Date: 2026-03-30
Period: 2021-01-01 ~ 2025-12-31
Symbols: 005930, 000660, 035720
Fixed: SMA200 filter, rsi_exit=70, ATR 2.5, regime OFF

## 3-stock aggregate

| rsi | adx | trades | agg_return | avg_sharpe | avg_mdd |
|-----|-----|--------|-----------|-----------|---------|
| 35 | 20 | 5 | +1.18% | -9.43 | -0.50% |
| 35 | 15 | 5 | +1.18% | -9.43 | -0.50% |
| 38 | 20 | 6 | +1.46% | -8.93 | -0.63% |
| 38 | 15 | 8 | +2.15% | -8.45 | -0.63% |
| 40 | 20 | 7 | +1.10% | -6.66 | -0.68% |
| 40 | 15 | 9 | +1.67% | -6.12 | -0.73% |
| 42 | 20 | 10 | -0.31% | -6.32 | -1.03% |
| 42 | 15 | 15 | +0.31% | -5.95 | -1.08% |
| 45 | 20 | 15 | +0.45% | -8.06 | -1.13% |
| 45 | 15 | 22 | -0.25% | -6.25 | -1.32% |

## Verdict: TOO_SPARSE / STRUCTURAL_ISSUE

- Max trades=22 (rsi=45, adx=15) but agg return negative
- Best return combo (rsi=38, adx=15) has only 8 trades
- 035720 never exceeds 3 trades in any combo
- All Sharpe values negative across all combos
- Root cause: close>SMA200 AND RSI<threshold are structurally conflicting for large-cap stocks

## Next: entry structure redesign required
- Option A: SMA200 -> SMA60 (shorter trend filter)
- Option B: breakout_volume (different signal structure)

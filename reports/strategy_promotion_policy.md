# Strategy Promotion Policy

## disabled -> experimental
- 백테스트 실행 가능
- FULL Sharpe > -1.0
- FULL MDD > -30%

## experimental -> paper_candidate
- OOS Sharpe >= 0.5
- WF 통과율 >= 60%
- EV/trade > 0
- 비용반영후 CAGR > 0%
- 벤치마크 초과수익 > 0%
- Turnover < 1000%/y
- MDD > -25%

## paper_candidate -> live_candidate
- Paper >= 60영업일
- Paper 승률 >= 40%
- Paper 누적수익률 >= 0%
- Paper MDD > -20%
- Paper Sharpe >= 0.3
- API 실패율 < 5%
- 블랙스완 < 3회
- 수익월 >= 2/3개월

## 강등 조건
- OOS Sharpe < 0
- MDD < -30%
- 연속 손실 10건

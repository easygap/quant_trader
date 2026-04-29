# Strategy Promotion Policy

## disabled -> experimental
- 백테스트 실행 가능
- FULL Sharpe > -1.0
- FULL MDD > -30%

## paper_only -> provisional_paper_candidate
- Full-period Sharpe >= 0.45
- Profit Factor >= 1.20
- WF positive window >= 60%
- WF Sharpe>0 window >= 60%
- WF windows >= 3
- WF trades >= 30
- EV/trade > 0 when available
- 비용반영후 CAGR > 0% when available
- Turnover < 1000%/y when available
- MDD > -20%
- 벤치마크 초과수익은 artifact에 기록하고 live gate에서 양수 필수

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

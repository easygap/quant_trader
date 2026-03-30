# Data Provider Validation

| Provider | 가격 | PER | ROE | 부채 | 영업이익 | 상태 |
|---|---|---|---|---|---|---|
| FDR | OK(수정주가) | X | X | X | X | 가격만 |
| pykrx | X(API장애) | X | X | X | X | 전면불가 |
| yfinance | OK | forwardPE | OK | debtToEquity(차입금/자본) | OK | 부채기준상이 |
| DART | 미연동 | OK | OK | OK | OK | 연동필요 |

yfinance debtToEquity: totalDebt/equity (한국 부채비율과 다름). debt_ratio_max 600%로 완화.

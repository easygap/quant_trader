# Schedule Dry Run Summary (격리 재증빙)
실행: 2026-03-26T17:38:15.561335 | Git: 4a1cfc9

## 시나리오 A: auto_entry=False
| 항목 | 전 | 후 | 차이 |
|------|---|---|------|
| TradeHistory | 0 | 0 | +0 |
| OperationEvent | 0 | 0 | +0 |
| PortfolioSnapshot | 0 | 0 | +0 |
| DailyReport | 0 | 0 | +0 |
> auto_entry=False + 비장중 → 전체 사이클 미실행. signal-only도 장중에만 동작.

## 시나리오 B: auto_entry=True, fresh DB
| 항목 | 전 | 후 | 차이 |
|------|---|---|------|
| TradeHistory | 0 | 0 | **+0** (3종목 모두 SELL → BUY 미발생) |
| OperationEvent | 0 | 3 | **+3** (SELL 신호 3건) |
| PortfolioSnapshot | 0 | 1 | **+1** (장마감 스냅샷) |
| DailyReport | 0 | 1 | **+1** (장마감 일간 리포트) |

## signal_at/order_at/fill_at 검증
- schedule 경로에서 TradeHistory가 0건이므로 **이번 드라이런에서 검증 불가**
- 원인: 현재 3종목 모두 SELL 신호 → BUY 없음
- 이전 셰이크다운의 수동 execute_buy 결과는 이번 fresh DB에 포함되지 않음

## 결론
- schedule 인프라(시작/장전/장마감/DB기록)는 정상 동작
- auto_entry=True이지만 매수 신호 부재로 full lifecycle 미검증
- TradeHistory lifecycle 필드는 BUY 신호 발생 시에만 검증 가능
- **signal-only 실험(auto_entry=False)은 즉시 시작 가능**
- **full paper 실험(auto_entry=True)은 시작 가능하나 BUY 없이 관측만 진행될 수 있음**

# Paper Shakedown Summary

## 셰이크다운 결과

### 필드 기록 확인
| 필드 | 상태 | 증빙 |
|------|------|------|
| signal_at | ✅ 기록됨 | 2026-03-26 17:11:17 (generate_signal 직전) |
| order_at | ✅ 기록됨 | 2026-03-26 17:11:18 (save_trade 시점) |
| executed_at | ✅ 기록됨 | 2026-03-26 17:11:18 |
| expected_price | ✅ 기록됨 | 54,200원 |
| price_gap | ✅ 기록됨 | 0.0원 (paper = 예상가 동일) |
| strategy | ✅ 기록됨 | scoring |
| symbol | ✅ 기록됨 | 005930 |
| reason | ✅ 기록됨 | shakedown BUY test |
| commission | ✅ 기록됨 | 293원 |
| slippage | ✅ 기록됨 | 3,600원 |
| warning_event | ✅ OperationEvent 테이블 | 4건 |
| day_pnl | ✅ PortfolioSnapshot | -0.04% |
| cumulative_pnl | ✅ PortfolioSnapshot | -0.04% |

### fill_at / actual_fill / fill_diff 참고
- paper 모드에서는 즉시 체결이므로 fill_at = executed_at, actual_fill = price, fill_diff = 0
- live 전환 시 KIS API 체결 응답으로 갱신 필요 (현재 미구현 — live 전환 전 작업)

### 일간 리포트
- save_daily_report(): DB DailyReport 테이블에 자동 저장
- 장마감(_run_post_market) 시 자동 호출
- Discord 알림 연동

### 주간 리포트
- WeeklyReportGenerator.generate(): DB 조회 기반
- 금요일 장마감 시 자동 생성 (scheduler._run_post_market)
- reports/paper_weekly_*.json + .txt 저장

### 실험 조건 (manifest)
- 전략: scoring (experimental)
- watchlist: 005930, 000660, 035420
- 벤치마크: KOSPI (KS11)
- 기간: 2026-03-27 ~ 2026-06-19 (60영업일)
- 비용: 수수료 0.015% + 세금 0.20% + 슬리피지 0.05%
- 실행: python main.py --mode schedule --strategy scoring

생성: 2026-03-26T17:11:57.452530
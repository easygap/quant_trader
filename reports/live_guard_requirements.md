# Live Guard Requirements

모두 충족 시에만 live 진입 가능하다. `--force-live` 우회는 제거됐다.

| # | 조건 | 검증 방법 | 현재 상태 |
|---|------|----------|----------|
| 1 | 현재 코드/설정과 일치하는 canonical bundle | `reports/promotion/run_metadata.json` | 구스키마/commit 불일치로 차단 |
| 2 | 전략이 `live_candidate` | `reports/promotion/promotion_result.json` + registry | live 후보 없음 |
| 3 | 벤치마크 초과수익 및 excess Sharpe 양수 | `reports/promotion/benchmark_comparison.json` | 현재 후보 모두 미충족 |
| 4 | Paper evidence package 적격 | `reports/paper_evidence/promotion_evidence_{strategy}.json` | scoring BLOCKED |
| 5 | execution-backed Paper 60영업일 이상 | `promotable_evidence_days` | 미충족 |
| 6 | benchmark final ratio 80% 이상 | `benchmark_final_ratio` | 미충족 |
| 7 | frozen day 0, sell 5건 이상, win rate 45% 이상 | promotion evidence | 미충족 |
| 8 | Evidence package 원본/무결성 검증 | `package_integrity.payload_hash`, `source_records.records_hash` | 불일치 시 live gate 차단 |
| 9 | 데이터 소스 health check | FDR/yfinance 수집 성공 | live gate 통과 후 확인 |
| 10 | 체결 조회 주문번호 일치 | KIS 일별체결조회 row의 `ODNO`/`ORD_NO` | 불일치 시 장부 반영 보류 |
| 11 | 직접 live BUY 호출 차단 | `OrderExecutor.live_gate_validated` | gate 통과 경로가 아니면 KIS 주문 전 차단 |
| 12 | live 주문 전 미체결 조회 성공 | `get_unfilled_order_status().checked` | 실패/불명확 시 BUY/SELL 제출 보류 |
| 13 | live 긴급 청산 현재가 검증 | `KISApi.get_current_price().price > 0` | 실패 시 평균단가 fallback 매도 차단 |

## 코드 위치
- `main.py:_check_live_readiness_gate()` — live gate 진입점
- `core/live_gate.py:validate_live_readiness()` — canonical artifact + paper evidence 검증
- `main.py:run_live_trading()` — gate 실패 시 `sys.exit(1)`
- `main.py:run_rebalance()` / `core.basket_rebalancer.BasketRebalancer.execute()` — live 바스켓 리밸런싱 주문도 운영자 확인과 live gate 통과 없이는 실행 차단
- `api.kis_api.KISApi.get_order_execution_after_order()` / `core.order_executor.OrderExecutor._resolve_live_execution()` — 현재 주문번호와 일치하는 체결 row만 DB 반영 허용
- `api.kis_api.KISApi.get_unfilled_order_status()` / `core.order_executor.OrderExecutor._live_unfilled_order_block()` — KIS 미체결 조회 실패 시 live 주문 제출 차단
- `main.py:run_emergency_liquidate()` — live 긴급 청산 현재가 조회 실패 시 평균단가 지정가 매도 fallback 차단
- 환경변수 `ENABLE_LIVE_TRADING=true` + `--confirm-live` 이중 확인 유지

생성: 2026-04-29
최신화: 2026-05-14

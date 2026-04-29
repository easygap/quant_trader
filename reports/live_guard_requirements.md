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
| 8 | 데이터 소스 health check | FDR/yfinance 수집 성공 | live gate 통과 후 확인 |

## 코드 위치
- `main.py:_check_live_readiness_gate()` — live gate 진입점
- `core/live_gate.py:validate_live_readiness()` — canonical artifact + paper evidence 검증
- `main.py:run_live_trading()` — gate 실패 시 `sys.exit(1)`
- 환경변수 `ENABLE_LIVE_TRADING=true` + `--confirm-live` 이중 확인 유지

생성: 2026-04-29

# Live Guard Requirements

## 5개 필수 조건 (Hard Gate)
모두 충족 시에만 live 진입 가능. `--force-live` 플래그로 강제 우회 가능하나 권장하지 않음.

| # | 조건 | 검증 방법 | 현재 상태 |
|---|------|----------|----------|
| 1 | 승인된 전략 1개 이상 | `reports/approved_strategies.json` | 0개 (미충족) |
| 2 | 해당 전략 WF 통과 | `reports/validation_walkforward_*.json` | scoring 0/2 실패 |
| 3 | 벤치마크 초과수익 양수 | `reports/benchmark_comparison.json` | scoring -80.6% (미충족) |
| 4 | Paper 60영업일 이상 | DB portfolio_snapshots | 0일 (미충족) |
| 5 | 데이터 소스 health check | FDR/yfinance 수집 성공 | FDR OK, PER yfinance OK |

## 코드 위치
- `main.py:_check_live_readiness_gate()` — 5개 조건 판정
- `main.py:run_live_trading()` — gate 실패 시 sys.exit(1)
- 환경변수 `ENABLE_LIVE_TRADING=true` + `--confirm-live` 이중 확인 기존 유지

생성: 2026-03-26T15:03:50.708194

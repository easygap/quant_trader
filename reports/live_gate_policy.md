# Live Gate Policy

Hard Gate (`main.py:_check_live_readiness_gate` → `core/live_gate.py`):
1. `reports/promotion/` canonical bundle 5종 존재
2. `run_metadata.json` schema/type, current git commit, `Config.yaml_hash`, `Config.resolved_hash`, freshness 일치
3. `promotion_result.json`에서 해당 전략이 `live_candidate`이고 `allowed_modes`에 `live` 포함
4. 같은 canonical bundle과 paper evidence를 `promotion_engine`으로 다시 로드해 승격 상태를 재계산하며, 재계산 결과도 `live_candidate`이고 `live` 허용이어야 함
5. `metrics_summary.json`/`walk_forward_summary.json` risk-adjusted 품질 조건 충족
6. `benchmark_comparison.json`의 전략별 excess return과 excess Sharpe가 모두 양수
7. `reports/paper_evidence/promotion_evidence_{strategy}.json` 내부 `strategy`가 현재 전략명과 정확히 일치하고 recommendation이 `ELIGIBLE`
8. execution-backed paper evidence 60영업일, benchmark_final_ratio >= 80%, 양의 same-universe/cash-adjusted excess와 cumulative return, sell_count >= 5, win_rate >= 45%, frozen_days = 0
9. paper evidence package payload hash와 원본 daily evidence JSONL source record hash가 재계산 결과와 일치
10. canonical `strategy_specs`가 target-weight 후보로 식별하는 전략은 verified pilot proof와 paper/canonical params hash 일치 필요
11. 데이터 health check 통과

레거시 `reports/approved_strategies.json`와 `reports/validation_walkforward_*.json`은 live 근거로 사용하지 않는다.

주문 실행 계층:
- `OrderExecutor` live 신규 BUY는 `live_gate_validated=True`로 생성된 인스턴스에서만 허용한다.
- 기본값은 fail-closed이며, 수동 스크립트/콘솔에서 `OrderExecutor(...).execute_buy()`를 직접 호출해도 KIS 주문 제출 전에 차단한다.
- SELL은 손절·긴급 청산 안전성을 위해 기존 청산 경로를 유지한다.

Soft Gate (strategies/__init__.py:is_strategy_allowed):
- backtest: 모든 전략
- paper/schedule: paper_only 이상
- live: live_candidate만

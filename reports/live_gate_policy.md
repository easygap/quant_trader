# Live Gate Policy

Hard Gate (`main.py:_check_live_readiness_gate` → `core/live_gate.py`):
1. `reports/promotion/` canonical bundle 5종 존재
2. `run_metadata.json` schema/type, current git commit, `Config.yaml_hash`, `Config.resolved_hash`, freshness 일치
3. `promotion_result.json`에서 해당 전략이 `live_candidate`이고 `allowed_modes`에 `live` 포함
4. `metrics_summary.json`/`walk_forward_summary.json` risk-adjusted 품질 조건 충족
5. `benchmark_comparison.json`의 전략별 excess return과 excess Sharpe가 모두 양수
6. `reports/paper_evidence/promotion_evidence_{strategy}.json` recommendation이 `ELIGIBLE`
7. execution-backed paper evidence 60영업일, benchmark_final_ratio >= 80%, 양의 excess/cumulative return, sell_count >= 5, win_rate >= 45%, frozen_days = 0
8. 데이터 health check 통과

레거시 `reports/approved_strategies.json`와 `reports/validation_walkforward_*.json`은 live 근거로 사용하지 않는다.

Soft Gate (strategies/__init__.py:is_strategy_allowed):
- backtest: 모든 전략
- paper/schedule: paper_only 이상
- live: live_candidate만

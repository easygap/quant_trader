# Live Gate Policy

5개 Hard Gate (main.py:_check_live_readiness_gate):
1. 승인전략>=1 (reports/approved_strategies.json)
2. WF 통과 (reports/validation_walkforward_*.json)
3. 벤치마크 초과수익>0 (reports/benchmark_comparison.json)
4. Paper>=60일 (DB portfolio_snapshots)
5. 데이터 health check

Soft Gate (strategies/__init__.py:is_strategy_allowed):
- backtest: 모든 전략
- paper/schedule: experimental 이상
- live: live_candidate만

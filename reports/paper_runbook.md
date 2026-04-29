# Paper Runbook

최종 수정: 2026-04-29

## 1. 시작 전 점검

```bash
python tools/paper_preflight.py --strategy scoring --with-pilot-check
python tools/paper_launch_readiness.py --strategy scoring --generate-runbook
```

확인할 것:
- runtime state가 `frozen`이 아닌지
- exit/finalize/evidence action이 허용되는지
- Discord notifier 설정 여부
- clean final days, benchmark final ratio, evidence freshness

## 2. signal-only 운영

신호와 evidence만 수집한다. 신규 주문은 내지 않는다.

```bash
python main.py --mode schedule --strategy scoring
```

## 3. full paper 운영

DB 모의 주문까지 실행한다. YAML 원본은 바꾸지 않고 환경변수로 전환한다.

```bash
QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring
```

## 4. 장마감 finalize / package

```bash
python tools/run_paper_evidence_pipeline.py --strategy scoring --finalize --generate-package
```

## 5. 모니터링

- `logs/`
- DB `trade_history`, `operation_events`, `portfolio_snapshots`
- `reports/paper_evidence/daily_evidence_scoring.jsonl`
- `reports/paper_runtime/runtime_status_scoring.json`
- `reports/paper_runtime/scoring_pilot_launch_readiness.md`

## 6. 중단 조건

`reports/experiment_stop_conditions.md`를 따른다. critical anomaly, phantom position, deep drawdown, repeated reject, notifier 장애는 operator 확인 전 신규 entry를 중단한다.

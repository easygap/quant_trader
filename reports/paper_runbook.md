# Paper Runbook

최종 수정: 2026-05-13

## 1. 시작 전 점검

```bash
python tools/paper_preflight.py --strategy scoring --with-pilot-check
python tools/paper_launch_readiness.py --strategy scoring --generate-runbook
```

확인할 것:
- runtime state가 `frozen`이 아닌지
- `preflight_status_{strategy}.json`이 생성되어 있고 critical fail이 아닌지
- 신규 BUY는 runtime `entry` 허용 또는 현재 pilot authorization 재검증이 있어야 실행되는지
- auto-entry 후보는 주문 직전 최신 데이터로 BUY 신호를 재검증하며, 재조회/API/전략 계산 실패 시 주문하지 않고 후보를 다음 루프로 보류하는지
- 신규 BUY 주문 직전 20일 평균 거래량이 전달되며, 누락/0 또는 평균 거래대금 하한 미달이면 주문이 차단되는지
- 갭업 차단용 최근 가격 조회가 실패하거나 데이터가 부족하면 신규 BUY가 차단되는지
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

full paper 신규 BUY는 preflight status가 없거나 손상되었거나 runtime 조회가 실패하면 주문 생성 전에 차단된다. 차단되어도 기존 포지션 SELL/exit는 계속 허용한다.

## 4. 장마감 finalize / package

```bash
python tools/run_paper_evidence_pipeline.py --strategy scoring --finalize --generate-package
```

수동 `--date`, `--backfill`, `--finalize` 실행으로 새로 생성된 record는 `backfill` provenance로 남기며 승격 증거로 카운트하지 않는다. 승격 패키지에는 실제 scheduler/pilot 세션에서 수집된 `real_paper`/`pilot_paper` evidence만 사용한다.

## 5. 모니터링

- `logs/`
- DB `trade_history`, `operation_events`, `portfolio_snapshots`
- `reports/paper_evidence/daily_evidence_scoring.jsonl`
- `reports/paper_runtime/runtime_status_scoring.json`
- `reports/paper_runtime/scoring_pilot_launch_readiness.md`

## 6. 중단 조건

`reports/experiment_stop_conditions.md`를 따른다. critical anomaly, phantom position, deep drawdown, repeated reject, notifier 장애는 operator 확인 전 신규 entry를 중단한다.

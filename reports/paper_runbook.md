# Paper Runbook

최종 수정: 2026-05-20

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
- 상관관계 리스크 확인용 대상/보유 종목 가격 데이터가 부족하거나 조회 실패하면 신규 BUY가 차단되는지
- 업종 비중 cap 확인용 섹터 맵이 비어 있거나 대상/보유 종목 업종 매핑이 누락되면 신규 BUY가 차단되는지
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
python tools/run_paper_evidence_pipeline.py --strategy scoring --finalize --date YYYY-MM-DD
python tools/run_paper_evidence_pipeline.py --strategy scoring --generate-package
```

`--finalize`는 날짜를 명시해야 실행된다. 날짜 없이 `--finalize --generate-package`를 주면 finalize가 누락된 승격 패키지를 만들 수 있으므로 CLI가 실패로 처리한다.

수동 `--date`, `--backfill`, `--finalize` 실행으로 새로 생성된 record는 `backfill` provenance로 남기며 승격 증거로 카운트하지 않는다. 승격 패키지에는 실제 scheduler/pilot 세션에서 수집된 `real_paper`/`pilot_paper` evidence만 사용한다. 같은 날짜에 backfill/shadow/비승격 repair record가 뒤에 추가되어도 canonical view는 검증된 paper/pilot evidence를 우선 보존한다.

Target-weight pilot evidence finalize가 `total_value` 또는 `daily_return` 미확정으로 막힌 경우에는 같은 finalize 명령을 즉시 반복하지 않는다. `tools/paper_pilot_control.py --status`의 `Operator next action`이 final portfolio performance evidence 대기를 안내하면, 성과 snapshot이 들어온 뒤 표시된 scheduled priority command를 다시 실행한다. 최신 finalize report는 source record와 portfolio metrics probe에서 확인된 성과 필드와 끝까지 누락된 필드를 기록하므로, 어떤 성과 snapshot이 아직 필요한지 함께 확인한다. source record에 필드가 있어도 값이 0 또는 파싱 불가라면 usable이 아니라 unusable로 표시되며, `total_value`/`daily_return` 확정 전에는 계속 missing performance로 본다. `Portfolio metrics probe`가 `missing_snapshot_history`면 해당 account_key의 portfolio snapshot 자체가 아직 없는 상태이므로 snapshot history를 먼저 복구하거나 생성한 뒤 finalize를 재실행한다. `missing_current_snapshot_after_trades`면 직전 snapshot 이후 거래가 있었지만 당일 snapshot이 없어 추론을 막은 상태이므로 장마감 portfolio snapshot capture를 먼저 실행한 뒤 finalize를 재실행한다. `Finalize diagnostics: missing`이 보이면 구버전 finalize report라서 판단 근거가 부족한 상태이므로, 표시된 diagnostics refresh command를 주문 없이 한 번 실행하고 current blockers/status를 다시 생성한다. 그 뒤에도 performance evidence guard가 남아 있으면 성과 snapshot 대기 상태로 본다. finalize CLI도 실패 시 artifact/report 경로와 missing performance fields를 바로 출력한다. `daily_return`/`portfolio_value`/benchmark excess 누락처럼 finalize로 promotable record가 될 수 있는 사유는 repair보다 finalize를 먼저 실행하고, repair는 finalize가 promotable proof를 만들 수 없을 때의 fallback으로만 사용한다.

생성된 promotion evidence package는 package payload hash와 source record hash를 포함한다. live gate는 이 값을 원본 `daily_evidence_{strategy}.jsonl`에서 재계산해 비교하므로, 패키지 요약값이나 원본 JSONL을 따로 수정했다면 package를 다시 생성해야 한다.

## 5. 모니터링

- `logs/`
- DB `trade_history`, `operation_events`, `portfolio_snapshots`
- `reports/paper_evidence/daily_evidence_scoring.jsonl`
- `reports/paper_runtime/runtime_status_scoring.json`
- `reports/paper_runtime/scoring_pilot_launch_readiness.md`

## 6. 중단 조건

`reports/experiment_stop_conditions.md`를 따른다. critical anomaly, phantom position, deep drawdown, repeated reject, notifier 장애는 operator 확인 전 신규 entry를 중단한다.

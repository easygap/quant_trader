# QUANT TRADER

국내 주식 자동매매를 공부하고 실험보려고 만든 개인 프로젝트입니다.  
지표·펀더멘털 기반으로 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

실전 주문과 잔고 조회는 KIS API를 사용합니다.  
데이터 수집, 리스크 관리, 알림, 대시보드, 리밸런싱 기능도 함께 붙여가며 확장하고 있습니다.

> **현재 상태 (2026-04-29)**:
> - GitHub 원격 브랜치 정리 완료: `main` 단일 브랜치 운영
> - 60영업일 Paper 실험 freeze pack 병합: `reports/experiment_freeze_pack.md`, 일/주간 ops checklist, stop condition 문서 추가
> - Paper Evidence 런타임: 일별 자동 수집 → benchmark finalization → promotion package → launch readiness
> - Paper Runtime State Machine: normal/degraded/frozen/blocked_insufficient_evidence 상태 자동 전환 + allowed_actions 제어
> - Paper Pilot Authorization: blocked 상태에서도 제한적 real paper 가능 (수동 승인 + 리스크 캡)
> - `QUANT_AUTO_ENTRY` 해석 단일화: YAML hash와 resolved hash를 분리해 실험 설정 drift 감지
> - scoring: clean_final_days=3 달성, infra_ready=true (pilot auth 대기)
> - rotation, scoring: **provisional_paper_candidate** (debiased WF 통과, 경제적 alpha 미확인)
> - live candidate: 없음. `--force-live` 제거, hard gate 우회 불가

## 주요 기능

- 백테스트 / 포트폴리오 백테스트 / 멀티전략 sleeve 비교
- 모의투자 / 실전 매매
- 전략 검증 / 성과 비교 / 파라미터 최적화
- 스코어링 / 평균회귀 / 추세추종 / 펀더멘털 / 앙상블 전략
- 거래량 돌파(C-4) / 상대강도 회전(C-5) / 2-sleeve 포트폴리오 (BV50/R50 **Paper 가동 중**)
- Paper Evidence 자동 수집 / Runtime State Machine / Pilot Authorization / Launch Readiness
- 리스크 관리, 알림, 바스켓 리밸런싱, 웹 대시보드

## 사용 환경

- Python 3.11 ~ 3.12

## 설치

```bash
pip install -r requirements.txt
```

## 설정

실행 전에 설정 파일과 환경변수를 먼저 준비해야 합니다.

- `config/settings.yaml.example` → `config/settings.yaml`
- `.env.example` 참고 후 `.env` 작성
- `config/holidays.yaml`은 필요 시 갱신 가능
- 미국 휴장일이 필요하면 `config/us_holidays.yaml` 추가

Paper schedule은 기본적으로 signal-only입니다. DB 모의 주문까지 실행하는 full paper는 환경변수로만 켭니다.

```bash
QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring
```

`trading.auto_entry`의 YAML 기본값은 `false`로 두고, 60영업일 실험에서는 `QUANT_AUTO_ENTRY`에 따라 resolved hash가 달라지는지 확인합니다. live 모드는 별도 hard gate를 통과해야 하며, 환경변수만으로 실전 주문을 열 수 없습니다.

## 실행

```bash
# 단일 종목 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 포트폴리오 백테스트
python main.py --mode portfolio_backtest --strategy scoring --symbols 005930,000660 --start 2023-01-01 --end 2024-12-31

# 거래량 돌파 전략 (C-4) 포트폴리오 백테스트
python main.py --mode portfolio_backtest --strategy breakout_volume --symbols 005930,000660,035720,051910 --start 2024-01-01 --end 2025-12-31

# 멀티전략 sleeve 비중 스윕 (C-5)
python scripts/c5_weight_sweep.py

# paper trading 월간 리포트 (BV50/R50 guardrail 모니터링)
python scripts/c5_paper_monthly_report.py

# paper trading 특정 기간 리포트
python scripts/c5_paper_monthly_report.py 2026-04-01 2026-04-02

# 모의투자
python main.py --mode paper --strategy scoring

# 모의 스케줄 루프
python main.py --mode schedule --strategy scoring

# full paper 스케줄 루프 (DB 모의 주문 실행)
QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring

# Paper 운영 preflight / launch readiness
python tools/paper_preflight.py --strategy scoring --with-pilot-check
python tools/paper_launch_readiness.py --strategy scoring --generate-runbook

# Paper evidence pipeline
python tools/run_paper_evidence_pipeline.py --strategy scoring --finalize --generate-package

# 실전 매매 (현재 모든 전략이 live 차단 상태 — live_candidate 승격 전까지 실행 불가)
# python main.py --mode live --strategy scoring --confirm-live

# 전략 검증
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5

# 성과 비교
python main.py --mode compare --start 2025-01-01 --end 2025-03-19 --strategy scoring

# 파라미터 최적화
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation

# 바스켓 리밸런싱
python main.py --mode rebalance

# 웹 대시보드
python main.py --mode dashboard

# 휴장일 갱신
python main.py --update-holidays
```

실전 매매는 `ENABLE_LIVE_TRADING=true` + `--confirm-live` + 전략 상태 `live_candidate` + 현재 commit/config와 일치하는 canonical promotion bundle + `ELIGIBLE` paper evidence package가 모두 필요합니다.
현재 모든 전략은 `provisional_paper_candidate` 또는 `disabled` 상태이며, **live 모드는 차단**되어 있습니다.  
`reports/approved_strategies.json`와 오래된 `validation_walkforward_*.json` 파일은 더 이상 live 근거가 아닙니다. `--force-live` 플래그는 제거되었으며, 어떤 조합으로도 hard gate를 우회할 수 없습니다.

## 리스크 관리

기본적인 안전장치는 넣어두었습니다.

- look-ahead 완화 백테스트
- 포지션 수 / 자금 비중 제한
- 미체결 / 중복 주문 방지
- 성과 열화 시 진입 제한
- 시장 국면 / 블랙스완 대응
- DB 백업 / 잔고 크로스체크 / 긴급 청산
- 알림 채널 fallback

세부 설정은 `config/risk_params.yaml`, `config/strategies.yaml`, `config/settings.yaml`에서 관리합니다.

## 테스트

```bash
pytest tests/ -q
```

외부 API나 웹소켓이 필요한 부분은 모킹해서 테스트합니다.

## 프로젝트 구조

* `config/` — 설정
* `core/` — 데이터, 지표, 신호, 리스크, 주문, 스케줄러, 알림, paper evidence/runtime/pilot/preflight
* `strategies/` — 전략 (scoring, breakout_volume, relative_strength_rotation 등)
* `scripts/` — 검증 스크립트 (C-4 OOS, C-5 sleeve 비교/비중 스윕/필터 테스트/rolling WF/paper 리포트)
* `tools/` — Paper 운영 도구 (evidence pipeline, pilot control, bootstrap, preflight, launch readiness)
* `api/` — KIS REST·웹소켓
* `backtest/` — 백테스트, 검증, 최적화, 비교
* `database/` — 모델·백업
* `monitoring/` — 로깅, 알림, 대시보드, 청산 트리거
* `tests/` — 테스트
* `docs/` — 문서
* `deploy/` — (선택) Oracle Cloud ARM 서버 상시 구동(systemd, logrotate)

## 문서

| 문서 | 내용 |
|------|------|
| [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md) | 파일 역할, 모드별 흐름, 설정 요약, 실전 전 체크리스트 |
| [`quant_trader_design.md`](quant_trader_design.md) | 아키텍처, 지표·전략·리스크 설계, 검증 관점, 로드맵 |
| [`BACKTEST_IMPROVEMENT.md`](BACKTEST_IMPROVEMENT.md) | 백테스트 신뢰성 개선 내역, 알려진 한계, 추가 과제 |
| [`deploy/README.md`](deploy/README.md) | Oracle Cloud Free Tier ARM 배포·systemd 상시 구동 가이드 |
| [`reports/strategy_promotion_policy.md`](reports/strategy_promotion_policy.md) | 전략 승격 정량 기준표 |
| [`reports/live_gate_policy.md`](reports/live_gate_policy.md) | Live 진입 5개 조건 |
| [`reports/paper_runbook.md`](reports/paper_runbook.md) | Paper Trading 운영 가이드 |
| [`reports/experiment_freeze_pack.md`](reports/experiment_freeze_pack.md) | 60영업일 Paper 실험 동결 기준, hash, 실행 모드 |
| [`reports/daily_ops_checklist.md`](reports/daily_ops_checklist.md) | 일일 Paper 운영 체크리스트 |
| [`reports/weekly_ops_checklist.md`](reports/weekly_ops_checklist.md) | 주간 Paper 운영 체크리스트 |
| [`reports/experiment_stop_conditions.md`](reports/experiment_stop_conditions.md) | 실험 중단·동결·재개 조건 |

## 전략 상태

승격 규칙 v3 — `core/promotion_engine.py`에서 metrics 기반 자동 판정. `tools/evaluate_and_promote.py --canonical`로 재현.  
Paper Evidence 체계 — `core/paper_evidence.py` 일별 22개 지표 자동 수집, `core/paper_runtime.py` entry gate, `core/paper_pilot.py` launch readiness/pilot auth 판정.

| 전략 | 상태 | Ret% | PF | WF P% | WF Sh+% | Paper Status |
|------|------|------|-----|-------|---------|--------------|
| relative_strength_rotation | **provisional_paper_candidate** | +18.09 | 1.62 | 100 | 83.3 | — |
| scoring | **provisional_paper_candidate** | +11.22 | 1.07 | 83.3 | 50.0 | clean_days=3, infra_ready |
| breakout_volume | disabled (research_only) | -13.31 | 0.79 | 0 | 0 | — |
| mean_reversion | disabled (research_only) | -8.36 | 0.85 | 33.3 | 0 | — |
| trend_following | disabled (research_only) | -6.94 | 0.67 | 16.7 | 0 | — |
| ensemble | disabled (research_only) | — | — | 0 | 0 | — |

## 주의

실전 투입 전에는 백테스트, 검증, 모의투자를 충분히 거친 뒤 사용하는 것을 권장합니다.  
현재 scoring Paper 실험은 2026-03-27~2026-06-19 60영업일 freeze pack 기준으로 관측합니다. Paper Evidence 체계로 승격/강등 근거를 자동 수집합니다.
Paper 운영 도구: `tools/run_paper_evidence_pipeline.py` (backfill/finalize/package), `tools/paper_preflight.py`, `tools/paper_launch_readiness.py`, `tools/paper_pilot_control.py`.

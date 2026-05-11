# QUANT TRADER

국내 주식 자동매매를 공부하고 실험보려고 만든 개인 프로젝트입니다.  
지표·펀더멘털 기반으로 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

실전 주문과 잔고 조회는 KIS API를 사용합니다.  
데이터 수집, 리스크 관리, 알림, 대시보드, 리밸런싱 기능도 함께 붙여가며 확장하고 있습니다.

> **현재 상태 (2026-05-08)**:
> - GitHub 원격 브랜치 정리 완료: 완료 브랜치 삭제, 활성 PR 브랜치만 유지
> - 60영업일 Paper 실험 freeze pack 병합: `reports/experiment_freeze_pack.md`, 일/주간 ops checklist, stop condition 문서 추가
> - Paper Evidence 런타임: v2 일별 자동 수집 → benchmark finalization → 날짜순 canonical evidence → promotion package → launch readiness
> - Paper Runtime State Machine: normal/degraded/frozen/blocked_insufficient_evidence 상태 자동 전환 + allowed_actions 제어
> - Paper Pilot Authorization: blocked 상태에서도 제한적 real paper 가능 (수동 승인 + 리스크 캡 + fail-closed/audited entry guard)
> - Paper 신규 진입 실행 경계 fail-closed: preflight 상태 누락/손상 또는 runtime 조회 실패 시 BUY 제출 전 차단, SELL 청산은 유지
> - `QUANT_AUTO_ENTRY` 해석 단일화: YAML hash와 resolved hash를 분리해 실험 설정 drift 감지
> - Research sweep: 기존 top-20 all-family 후보 재검증도 `NO_ALPHA_CANDIDATE`; `pullback`, benchmark-relative momentum, risk-budget, cash-switch, benchmark-aware rotation, target-weight top-N rotation/score-floor 후보군과 exposure-matched benchmark 진단을 research-only로 추가
> - scoring: **paper_only** (관찰 가능하지만 Sharpe/PF/WF 안정성 미달)
> - rotation: **provisional_paper_candidate** (risk-adjusted 기준 통과, live alpha는 미확인)
> - target-weight risk overlay 후보: canonical bundle 기준 **provisional_paper_candidate** + 전용 paper/pilot adapter/shadow proof, 유동성/비용 pre-trade/pilot 승인/실행일/장 시간/가격 최신성 guard 추가. 리서치 백테스트는 직전 거래일 점수 → 다음 거래일 시가 체결 → 종가 평가 기준으로 보수화했으며, 기존 target-weight research artifact는 execution price mode 확인 또는 재생성 후 사용 (live 미연결)
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

# 포트폴리오 백테스트 (gap/어닝/BlackSwan guard 포함)
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

Full paper 신규 BUY는 `reports/paper_runtime/preflight_status_{strategy}.json`이 존재하고, runtime state 조회가 성공하며, `entry` 허용 또는 현재 pilot authorization이 재검증될 때만 실행됩니다. preflight 누락/손상, runtime 조회 실패, critical fail은 주문 생성 전 차단되며 기존 포지션 SELL 청산은 차단하지 않습니다.

실전 매매는 `ENABLE_LIVE_TRADING=true` + `--confirm-live` + 전략 상태 `live_candidate` + 현재 commit/config와 일치하는 canonical promotion bundle + `ELIGIBLE` paper evidence package가 모두 필요합니다.
현재 모든 전략은 `provisional_paper_candidate` 또는 `disabled` 상태이며, **live 모드는 차단**되어 있습니다.  
`reports/approved_strategies.json`와 오래된 `validation_walkforward_*.json` 파일은 더 이상 live 근거가 아닙니다. `--force-live` 플래그는 제거되었으며, 어떤 조합으로도 hard gate를 우회할 수 없습니다.

## 리스크 관리

기본적인 안전장치는 넣어두었습니다.

- look-ahead 완화 백테스트
- 포지션 수 / 자금 비중 제한
- 미체결 / 중복 주문 방지 (live 미체결 조회 실패 시 주문 보류)
- live 주문 체결 확인 전 DB 거래·포지션 반영 보류
- 성과 열화 시 진입 제한
- 시장 국면 / 블랙스완 대응
- 단일종목/포트폴리오 백테스트 gap/어닝/BlackSwan 이벤트 guard
- 백테스트 / research sweep universe 20일 평균 거래대금 사전 필터
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
* `tools/` — Paper 운영 도구와 research sweep (evidence pipeline, pilot control, bootstrap, preflight, launch readiness, candidate sweep, target-weight pilot)
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
| [`BACKTEST_IMPROVEMENT.md`](docs/BACKTEST_IMPROVEMENT.md) | 백테스트 신뢰성 개선 내역, 알려진 한계, 추가 과제 |
| [`deploy/README.md`](deploy/README.md) | Oracle Cloud Free Tier ARM 배포·systemd 상시 구동 가이드 |
| [`reports/strategy_promotion_policy.md`](reports/strategy_promotion_policy.md) | 전략 승격 정량 기준표 |
| [`reports/live_gate_policy.md`](reports/live_gate_policy.md) | Live 진입 canonical/evidence hard gate |
| [`reports/paper_runbook.md`](reports/paper_runbook.md) | Paper Trading 운영 가이드 |
| [`reports/experiment_freeze_pack.md`](reports/experiment_freeze_pack.md) | 60영업일 Paper 실험 동결 기준, hash, 실행 모드 |
| [`reports/daily_ops_checklist.md`](reports/daily_ops_checklist.md) | 일일 Paper 운영 체크리스트 |
| [`reports/weekly_ops_checklist.md`](reports/weekly_ops_checklist.md) | 주간 Paper 운영 체크리스트 |
| [`reports/experiment_stop_conditions.md`](reports/experiment_stop_conditions.md) | 실험 중단·동결·재개 조건 |

## 전략 상태

승격 규칙 v3 — `core/promotion_engine.py`에서 metrics 기반 자동 판정. `tools/evaluate_and_promote.py --canonical`로 재현하며, canonical 평가 산출물에는 종목군 구성, 데이터 범위, 수집 오류를 바탕으로 만든 `data_snapshot_hash`를 남긴다. live gate와 승격 산출물 로더는 이 해시, 데이터 범위, 수집 오류, 평가 실패 상태를 다시 검증해 손상된 산출물 사용을 차단한다.
Research candidate sweep — `tools/research_candidate_sweep.py --quick --candidate-family all`로 promotion과 분리된 rotation/momentum/breakout/pullback/benchmark-relative/risk-budget/cash-switch/benchmark-aware rotation/target-weight top-N rotation 후보 랭킹 artifact를 생성. Raw EW B&H gate는 유지하되, defensive/cash-heavy 후보 해석을 위해 평균 노출률과 exposure-matched B&H excess도 진단값으로 기록합니다. 벤치마크 universe 일부 종목이라도 OHLCV 수집·기간 검증에 실패하면 `INSUFFICIENT_BENCHMARK_DATA`로 fail-closed 차단하고, 결측 종목과 커버리지 비율을 artifact/Markdown에 남깁니다. target-weight 후보는 `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금으로 남기고, `hold_rank_buffer`로 작은 랭킹 흔들림에 따른 불필요한 교체를 줄이며, `market_exposure_mode=benchmark_risk`로 KS11 SMA/낙폭/변동성 risk-off 구간의 부분 노출 축소를 검증합니다. target-weight 리서치 백테스트는 직전 거래일 점수로 다음 거래일 시가에 리밸런싱하고 일말 평가는 종가로 하며, 전일 기준 20일 평균 거래량을 비용 계산에 넘겨 동적 슬리피지와 participation 진단값을 남깁니다.
Paper Evidence 체계 — `core/paper_evidence.py` v2 일별 22개 지표 자동 수집, `core/paper_runtime.py` entry gate, `core/paper_pilot.py` launch readiness/pilot auth 판정. scheduler는 v2 collector만 canonical 증거로 기록하며, legacy `core/evidence_collector.py`는 import 호환용 deprecated no-op입니다. 승격 패키지는 `execution_backed=True`와 `real_paper`/`pilot_paper` 출처가 명시된 기록만 승격 증거로 인정해 예전 형식·수작업 기록 오염을 차단한다.

2026-04-29 all-family quick sweep: 5종목(`005930,000660,035720,051910,068270`)에서 rotation/momentum/breakout 후보 14개를 비교했지만 모두 benchmark excess return/Sharpe를 통과하지 못해 `NO_ALPHA_CANDIDATE`로 판정. 이 결과만으로 canonical promotion이나 paper/live 승격은 진행하지 않습니다.

2026-04-30 top-20 all-family quick sweep: canonical liquidity universe 20종목에서 동일 후보 14개를 재검증했지만 `NO_ALPHA_CANDIDATE` 유지. best=`momentum_factor_120d`는 return +118.56%, Sharpe 0.79였으나 benchmark excess=-30.83%p, MDD=-40.08%로 승격 불가. 다음 연구는 단순 후보 확장이 아니라 benchmark를 이기는 새로운 alpha 후보군 설계로 전환합니다.

2026-04-30 follow-up: 기존 전략 중 외부 재무 데이터 의존이 없는 `trend_pullback`을 `pullback` candidate family로 추가했습니다. 또한 기존 실패 원인(절대수익은 높지만 benchmark에 뒤처짐)을 직접 겨냥하기 위해 `momentum_factor`에 KS11 대비 초과 모멘텀/변동성 게이트 옵션을 추가하고 `benchmark_relative` candidate family로 노출했습니다. `all` sweep은 이제 rotation/momentum/breakout/pullback/benchmark-relative 후보군을 함께 평가합니다.

2026-04-30 5-symbol smoke sweep: 신규 `benchmark_relative` 3개와 `pullback` 4개 모두 `NO_ALPHA_CANDIDATE`. best=`benchmark_relative_momentum_60d` return +4.13%, excess=-169.50%p; best pullback=`trend_pullback_aggressive` return +3.04%, excess=-170.59%p. 두 후보군은 계속 research-only입니다.

2026-04-30 follow-up: 신호 필터만 추가하는 방향이 약하다고 판단해 `risk_budget` candidate family를 추가했습니다. 동일 신호를 집중형/균형형/방어형 exposure budget으로 나눠 평가하고, 각 후보 artifact에 적용된 `diversification` 설정을 기록합니다.

2026-04-30 risk-budget smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best return=`risk_budget_momentum_120d_concentrated` +11.40%, excess=-162.23%p, MDD=-32.28%; best risk-adjusted=`risk_budget_rotation_slow_defensive` +10.91%, excess=-162.72%p, MDD=-6.41%. 방어형 exposure는 낙폭을 줄였지만 alpha 자체는 아직 없습니다.

2026-04-30 follow-up: 방어형 exposure만으로 benchmark를 이기지 못해 `relative_strength_rotation`에 `market_filter_exit` 옵션을 추가하고 `cash_switch` candidate family를 추가했습니다. KS11이 이동평균 아래로 내려가면 신규 진입 차단을 넘어 기존 포지션도 현금화하는 구조입니다.

2026-04-30 cash-switch smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`cash_switch_rotation_slow_defensive` return +1.87%, excess=-171.76%p, Sharpe=-0.40, MDD=-11.78%. 현금 전환은 손실 방어에는 일부 유효했지만 benchmark 대비 alpha는 만들지 못해 research-only로 유지합니다.

2026-04-30 follow-up: `research_candidate_sweep`에 exposure-matched benchmark diagnostics를 추가했습니다. cash-switch 후보의 평균 노출은 8.4~10.0%에 불과했고 exposure-matched excess도 -7.87%p~-0.36%p로 음수였습니다. 즉 raw benchmark gap은 낮은 노출 영향이 크지만, 같은 노출로 비교해도 신호 edge가 아직 없어 다음 연구는 단순 현금화보다 benchmark-aware 랭킹/부분 헤지/노출 유지형 alpha 설계가 우선입니다.

2026-04-30 follow-up: `relative_strength_rotation`에 benchmark-aware ranked mode를 추가했습니다. `score_mode=benchmark_excess`는 종목 60/120d 복합 모멘텀에서 KS11 복합 모멘텀을 차감해 랭킹하고, `rank_entry_mode=dense_ranked`/`exit_rebalance_mode=score_floor`로 절대 추세 필터 때문에 과도하게 현금화되는 문제를 research-only로 분리 검증합니다.

2026-05-08 follow-up: `backtest/portfolio_backtester.py`에도 gap-up 신규 매수 차단, gap-down `GAP_DOWN` 청산, 어닝 윈도우 신규 매수 차단, BlackSwan 청산·쿨다운·recovery 사이징을 반영했습니다. 단일종목/포트폴리오 백테스트가 같은 리스크 이벤트 전제로 비교되도록 `gap_*`, `earnings_*`, `blackswan_*` 진단 카운터와 회귀 테스트를 보강했습니다.

2026-05-08 follow-up: `WatchlistManager.liquidity_filter_report()`를 공통 진단 API로 분리하고, 포트폴리오 백테스트와 `tools/research_candidate_sweep.py`가 평가 시작일 기준 20일 평균 거래대금 하한 미만 또는 strict 모드 데이터 누락 종목을 universe에서 먼저 제외하도록 보강했습니다. research artifact에는 입력 universe, 필터 통과 universe, 제외 종목/사유가 함께 남습니다.

2026-05-08 follow-up: `PortfolioBacktester`도 단일종목 백테스터처럼 종목별 20일 평균 거래량을 `RiskManager.calculate_transaction_costs()`에 전달해 동적 슬리피지 배수를 반영합니다. 포트폴리오 거래 기록에는 `participation_rate`, `slippage_multiplier`, `slippage_cost`가 남아 거래비용 과소추정을 점검할 수 있습니다.

2026-05-08 follow-up: `tools/research_candidate_sweep.py`의 target-weight 리서치 백테스트도 OHLCV `volume`으로 종목별 20일 평균 거래량을 계산해 매수·매도 비용에 전달합니다. target-weight trade와 metrics에는 `avg_daily_volume`, `participation_rate`, `slippage_multiplier`, `slippage_cost_total`이 남아 high-turnover 후보의 비용 과소추정을 더 빨리 확인할 수 있습니다.

2026-05-08 follow-up: `research_candidate_sweep`의 EW B&H 벤치마크가 일부 종목 결측 상태에서 전체 capital 대비 낮게 계산되어 후보 초과수익이 과대평가되는 경로를 차단했습니다. 벤치마크 입력 universe 전체가 수집·검증되지 않으면 excess gate와 decision action은 `INSUFFICIENT_BENCHMARK_DATA`로 고정됩니다.

2026-05-08 follow-up: `OrderExecutor` live BUY/SELL은 주문 ACK 이후 체결가·체결수량 확인이 되지 않거나 부분체결만 확인되면 더 이상 예상가 기준 전량 FILLED로 처리하지 않습니다. 이때 `success=False`는 브로커 주문 부재가 아니라 `order_pending=True`/`requires_reconcile=True`인 장부 반영 보류 상태이며, KIS 잔고 대조 전 DB 포지션·거래 기록 오염을 막습니다.

2026-05-08 follow-up: KIS 미체결 조회 실패를 더 이상 “미체결 없음”으로 해석하지 않습니다. live BUY/SELL은 주문 전 미체결 조회가 실패하거나 응답 형식이 불명확하면 `live_unfilled_check.checked=False`로 주문을 보류하고, 재시작 복구에서도 KIS 미체결 조회 실패를 별도 critical 알림으로 드러냅니다.

2026-05-08 follow-up: target-weight 리서치 백테스트의 리밸런싱 체결 기준을 당일 종가에서 다음 거래일 시가로 보수화했습니다. 랭킹과 risk-off 판단은 직전 거래일 종가까지만 사용하고, 리밸런싱 체결은 해당 거래일 원본 `open`, 일말 평가는 `close`로 분리합니다. 신규 top-N 매수 후보의 리밸런싱일 `open`이 누락되면 `target_weight_research_execution_price_missing`으로 중단하고, 이미 보유한 종목의 `open`이 없으면 해당 리밸런싱을 거래 없이 skip 진단으로 남깁니다. 동적 슬리피지의 20일 평균 거래량도 당일 거래량을 포함하지 않도록 1거래일 지연합니다.

2026-04-30 benchmark-aware rotation smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`benchmark_aware_rotation_60_120_balanced` return=+21.65%, Sharpe=0.50, avg exposure=24.1%였지만 raw excess=-151.98%p, exposure-matched excess=-16.05%p라 promotion 미진행. fast `40_100_dense`는 exposure-matched excess=+2.04%p였으나 raw excess=-163.35%p라 다음 연구 힌트로만 기록합니다. 다음 방향은 sparse BUY/SELL 신호를 넘어 monthly top-N 목표비중 리밸런싱을 별도 백테스터로 검증하는 것입니다.

2026-04-30 target-weight top-N rotation smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`target_weight_rotation_top3_40_100_excess` return=+128.44%, Sharpe=1.13, avg exposure=85.3%로 노출 부족 문제는 해결했지만 raw excess=-45.19%p, exposure-matched excess=-14.82%p라 promotion 미진행. 이 결과는 sparse 신호가 병목이었음을 확인했지만, 동일 유니버스 B&H를 이기는 alpha는 아직 아니라서 다음 연구는 더 넓은 유니버스와 부분 hedge/상대강도 필터 개선입니다.

2026-04-30 canonical top-20 target-weight full sweep: 기존 `target_weight_rotation_top5_60_120_floor0_hold3`는 return=+278.57%, raw excess=+129.18%p, exposure-matched excess=+150.88%p, Sharpe=1.65, WF positive/Sh+ 100%, turnover/year=807.8%였지만 MDD=-28.25%로 `paper_only`. benchmark-risk overlay 추가 후 best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`는 return=+210.24%, raw excess=+60.85%p, exposure-matched excess=+130.96%p, Sharpe=1.60, PF=5.73, MDD=-19.24%, turnover/year=858.0%, WF positive/Sh+ 100%, risk-off rebalance=38.9%로 처음 `provisional_paper_candidate`에 도달했습니다. 후속으로 `tools/evaluate_and_promote.py --canonical`이 이 후보를 canonical promotion bundle에 재현하도록 연결했고, `promotion_result.json`에서도 동일 후보가 `provisional_paper_candidate`로 로드됩니다. 추가로 `tools/target_weight_rotation_pilot.py`가 portfolio-level 목표비중 plan을 만들고 pilot cap을 검증한 뒤 paper-only exact-quantity 주문을 낼 수 있게 했습니다. dry-run은 `--record-shadow-evidence`로 non-promotable `shadow_bootstrap` evidence를 남기고, 같은 실행에서 launch readiness JSON/MD와 pilot runbook을 생성합니다. session artifact에는 기본 cap preview, plan 기반 최소/추천 pilot cap, enable 명령, launch artifact 경로가 함께 기록됩니다. live 자동운영은 여전히 금지입니다.

2026-05-11 follow-up: target-weight canonical을 next-open 체결과 결측 진단 기준으로 재검증했습니다. `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`는 return=+171.20%, PF=4.24, Sharpe=1.41, MDD=-19.90%, WF positive=100%, WF Sharpe+=83.3%로 `provisional_paper_candidate`를 유지했습니다. 이번 산출물은 stale score 후보 2개 제외, held stale valuation 21일, 보유 종목 시가 누락에 따른 리밸런싱 skip 1회를 metrics에 남기므로 paper pilot에서는 체결 가능성과 stale valuation 빈도를 함께 감시해야 합니다.

| 전략 | 상태 | Ret% | PF | WF P% | WF Sh+% | Paper Status |
|------|------|------|-----|-------|---------|--------------|
| relative_strength_rotation | **provisional_paper_candidate** | +18.09 | 1.62 | 100 | 83.3 | — |
| target_weight_rotation_top5_60_120_floor0_hold3_risk60_35 | **provisional_paper_candidate** (next-open canonical) | +171.20 | 4.24 | 100 | 83.3 | 전용 paper/pilot adapter 준비, 결측 진단 감시 필요 |
| scoring | **paper_only** | +11.22 | 1.07 | 83.3 | 50.0 | risk-adjusted alpha 미달 |
| breakout_volume | disabled (research_only) | -13.31 | 0.79 | 0 | 0 | — |
| mean_reversion | disabled (research_only) | -8.36 | 0.85 | 33.3 | 0 | — |
| trend_following | disabled (research_only) | -6.94 | 0.67 | 16.7 | 0 | — |
| ensemble | disabled (research_only) | — | — | 0 | 0 | — |

## 주의

실전 투입 전에는 백테스트, 검증, 모의투자를 충분히 거친 뒤 사용하는 것을 권장합니다.  
현재 scoring은 관찰용 paper_only로 강등되었습니다. 신규 우선 실험은 risk-adjusted 기준을 통과한 후보만 대상으로 하며, Paper Evidence 체계로 승격/강등 근거를 자동 수집합니다.
Paper 운영 도구: `tools/run_paper_evidence_pipeline.py` (backfill/finalize/package), `tools/paper_preflight.py`, `tools/paper_launch_readiness.py`, `tools/paper_pilot_control.py`, `tools/research_candidate_sweep.py`, `tools/target_weight_rotation_pilot.py`. target-weight 후보는 capped pilot 승인 전 `tools/target_weight_rotation_pilot.py --record-shadow-evidence`, `--shadow-days 3`, 또는 `--shadow-start-date YYYY-MM-DD --shadow-end-date YYYY-MM-DD`로 dry-run plan artifact, shadow readiness evidence, launch readiness artifact, plan 기반 cap 추천과 enable 명령이 포함된 pilot runbook을 먼저 누적합니다. 이후 `--readiness-audit`로 주문 제출이나 shadow/pilot evidence 기록 없이 clean shadow, launch readiness, active pilot caps, 중복 세션, 실행일/장 시간, 실행 전 포지션 드리프트, 추천 cap 충족 여부, 최근 평균 거래대금 대비 주문 비율, 비용 반영 후 현금/투자비중 한도를 JSON artifact와 Markdown 운영 리포트로 점검합니다. 유동성 preflight는 기본적으로 주문별 20일 평균 거래대금의 5% 초과를 차단하며 `--max-order-adv-pct`로 조정할 수 있습니다. pre-trade risk 검증은 주문별 수수료/세금/동적 슬리피지를 반영한 예상 체결가로 현금 부족, 최소 현금비중, 총투자비중, 종목별 비중, 최대 보유 종목 수 위반을 실행 전에 차단합니다. Markdown 리포트에는 shadow 수집, audit 재실행, 추천 cap 승인, capped paper 실행 명령이 함께 기록됩니다. `--shadow-days N`은 휴장/데이터 공백으로 같은 거래일에 매핑되는 경우 과거 평일을 추가 스캔해 N개 고유 resolved trade_day 충족을 목표로 하며, 목표 미달이나 날짜별 실패가 있으면 CLI가 non-zero로 종료해 자동화가 불완전한 증거를 성공으로 처리하지 못하게 합니다. 실행형 pilot evidence는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, 주문 제출 직전 실제 paper position이 계획 입력 장부 `position_quantities_before`와 일치하며, `plan.trade_day`가 KST 실행일과 일치하고 KRX 정규장 주문 가능 시간인 경우에만 진행됩니다. 이후 유동성 preflight와 pre-trade risk 검증을 통과하고, 모든 계획 주문이 성공하며, 성공 주문 결과와 당일 `TradeHistory` fill 집계의 종목/방향/수량이 계획과 일치하고, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 `pilot_paper`로 수집됩니다. 같은 날짜 evidence가 이미 있으면 기존 canonical record도 `pilot_paper`/authorized/target-weight complete/execution-market-session-allowed/liquidity-complete/pre-trade-risk-complete/fill-complete 조건을 통과해야 재사용합니다. 중복 실행은 기본 차단하며 운영자가 명시적으로 재시도해야 할 때만 `--allow-rerun`을 사용합니다. 부분 실행/중단/중복 실행/주문 결과 불일치/체결 기록 불일치/기존 evidence 검증 실패/실행일 불일치/장 시간 외 실행/실행 전 포지션 드리프트/유동성 preflight 실패/pre-trade risk 실패/실행 후 포지션 불일치는 execution-backed 승격 증거에서 제외됩니다.

Target-weight 60영업일 pilot manifest: `--readiness-audit`는 운영 리포트와 함께 `target_weight_paper_experiment_manifest_*.json`을 생성해 후보 snapshot, 추천 cap, 차단 사유, 실행 명령, 승격 증거 조건을 고정합니다. `--daily-ops-summary`는 readiness, risk check, 실행일 일치 여부, 장 시간 주문 가능 여부, verified pilot day 진행률, 다음 실행 명령을 `target_weight_daily_ops_summary_*.json/.md`로 묶어 매일 운영 판단을 한 번에 확인하게 합니다. 공식 `reports/paper_experiment_manifest.json`도 기존 scoring 60영업일 실험과 target-weight capped paper pilot을 분리해 기록합니다.

Generic paper entry도 동일한 실행 경계 원칙을 따른다. `main.py --mode paper`, scheduler auto-entry, fixed-quantity paper BUY 모두 preflight status와 runtime state를 주문 생성 전에 확인하고, 확인 실패는 fail-closed로 차단한다. blocked runtime에서 pilot authorization이 활성화되어도 `check_pilot_entry()`를 다시 통과해야 하며, SELL/exit 경로는 포지션 정리를 위해 계속 허용한다. pilot evidence freshness는 달력일이 아니라 주말과 한국장 휴장일을 제외한 영업일 기준으로 계산합니다.

Target-weight pilot 승인/재시도 보강: `tools/paper_pilot_control.py --enable`은 `target_weight_*` 후보의 pilot auth를 쓰기 전에 target-weight readiness audit을 다시 실행해 운영자가 요청한 cap이 현재 plan, launch readiness, 유동성 preflight, 비용 반영 pre-trade risk를 만족하는지 검증합니다. 승인 auth에는 당시 plan의 `trade_day`, `as_of_date`, `params_hash`, targets, 시작/목표 수량 snapshot을 함께 저장해 cap 승인과 실행 계획을 묶습니다. 유동성 diagnostics가 없으면 fail-closed로 차단하고, 이미 주문이 완료된 same-candidate/trade-day 세션은 `--allow-rerun`을 줘도 재실행하지 않습니다. `--allow-rerun`은 부분 실행이나 중단된 세션 복구용으로만 사용합니다.

Target-weight 실행 차단 기록: `--execute`가 pilot cap validation에서 막히면 주문·체결·증거 수집 없이 session JSON artifact에 차단 사유를 남깁니다. runtime pilot session은 쓰지 않아 cap을 고친 뒤 같은 거래일 계획을 다시 점검할 수 있습니다. 또한 `plan.trade_day`와 KST 기준 실제 실행일이 다르거나, 현재 시간이 KRX 정규장 주문 가능 시간이 아니거나, 활성 pilot auth의 승인 snapshot이 현재 plan의 `params_hash`/trade day/targets/수량 장부와 다르면 주문, idempotency, 포지션 조회, 체결 대조, session 저장, 승격 증거 수집 전에 fail-closed로 차단합니다. no-order `--readiness-audit`와 `--daily-ops-summary`도 실행일 check, 장 시간 check, 승인 snapshot check를 blocker로 표시해 오래되었거나 장 외 실행 명령을 READY 상태로 노출하지 않습니다.

Target-weight 가격 최신성 guard: 목표비중 plan 생성 시 종목별 마지막 실제 종가 날짜(`price_last_dates`)와 벤치마크 최신 날짜(`benchmark_last_date`)를 diagnostics에 남깁니다. 운영 주문 계획에서는 ffill로 보정된 낡은 종목 가격이 `trade_day` 최신 가격처럼 쓰이면 `target_weight_stale_price_data`로 차단하고, `benchmark_excess` 점수나 benchmark risk overlay에 필요한 벤치마크 가격이 score day보다 오래되면 `target_weight_benchmark_price_stale`로 plan 생성 자체를 중단합니다. research target-weight backtest는 원본 close/open panel을 보존하되, score day 실제 종가가 없는 종목은 후보에서 제외하고 `stale_score_symbols_*`로 기록합니다. 보유 종목의 일별 stale 평가는 `held_stale_valuation_*`로 남기며, 보유 종목의 리밸런싱일 시가가 없으면 거래 없이 `missing_held_open_*`/`skipped_rebalance_missing_held_open_count`로 기록합니다. 벤치마크 stale과 신규 top-N 매수 후보의 시가 누락은 계속 fail-closed로 중단해 stale 가격이나 당일 종가 체결 착시가 pilot 흐름에 섞이지 않게 합니다.

Target-weight 승격 증거 보강: target-weight 계열 전략은 일반 `execution_backed=True` paper record만으로 promotion evidence day를 채우지 않습니다. `pilot_paper`/authorized record가 target-weight plan과 execution proof를 포함하고, record date와 plan trade day가 일치하며, `execution_trade_day_allowed=True`, `execution_market_session_allowed=True`, `pilot_authorization_snapshot_allowed=True`, liquidity/pre-trade risk/order result/fill/position reconciliation complete 및 plan/execution params hash 일치를 만족한 날만 승격 카운트에 들어갑니다. 60영업일 전체 verified pilot evidence는 하나의 params hash로 고정되어야 하며, live gate도 canonical metadata의 params hash와 paper evidence params hash가 다르면 target-weight live 전환을 차단합니다.

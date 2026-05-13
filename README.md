# QUANT TRADER

국내 주식 자동매매를 공부하고 실험보려고 만든 개인 프로젝트입니다.  
지표·펀더멘털 기반으로 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

실전 주문과 잔고 조회는 KIS API를 사용합니다.  
데이터 수집, 리스크 관리, 알림, 대시보드, 리밸런싱 기능도 함께 붙여가며 확장하고 있습니다.

> **현재 상태 (2026-05-13)**:
> - GitHub 원격 브랜치 정리 완료: 완료 브랜치 삭제, 활성 PR 브랜치만 유지
> - 60영업일 Paper 실험 freeze pack 병합: `reports/experiment_freeze_pack.md`, 일/주간 ops checklist, stop condition 문서 추가
> - Paper Evidence 런타임: v2 일별 자동 수집 → benchmark finalization → 날짜순 canonical evidence → promotion package → launch readiness
> - Paper Runtime State Machine: normal/degraded/frozen/blocked_insufficient_evidence 상태 자동 전환 + allowed_actions 제어
> - Paper Pilot Authorization: blocked 상태에서도 제한적 real paper 가능 (수동 승인 + 리스크 캡 + fail-closed/audited entry guard)
> - Paper 신규 진입 실행 경계 fail-closed: preflight 상태 누락/손상 또는 runtime 조회 실패 시 BUY 제출 전 차단, SELL 청산은 유지
> - `QUANT_AUTO_ENTRY` 해석 단일화: YAML hash와 resolved hash를 분리해 실험 설정 drift 감지
> - Research sweep: 기존 top-20 all-family 후보 재검증도 `NO_ALPHA_CANDIDATE`; `pullback`, benchmark-relative momentum, risk-budget, cash-switch, benchmark-aware rotation, target-weight top-N rotation/score-floor 후보군과 exposure-matched benchmark 진단을 research-only로 추가
> - target-weight 리스크 완화 top-200 sweep: 최상위 tolerance 후보도 수익/초과수익은 개선됐지만 MDD·회전율 게이트 미통과로 전 후보 `paper_only`
> - target-weight 저회전 top-200 sweep: 격월/분기 후보가 회전율은 낮췄지만 benchmark excess Sharpe와 MDD 게이트 미통과로 `NO_ALPHA_CANDIDATE`
> - target-weight 변동성 타깃 top-200 sweep: 최상위 후보는 초과수익이 양수였지만 전 후보가 MDD 게이트 미통과로 `KEEP_RESEARCH_ONLY`
> - target-weight 리스크 페널티 랭킹 top-200 sweep: 수익·초과수익은 개선됐지만 MDD·회전율 게이트 미통과로 `KEEP_RESEARCH_ONLY`
> - target-weight 손실방어 top-200 sweep: pdd8/floor25/cooldown1 후보가 MDD -19.56%, turnover/year 296.4%로 개선되어 research sweep 기준 `RUN_CANONICAL_EVALUATION`
> - target-weight 손실방어 canonical 평가: pdd8 후보는 benchmark excess -6.46%p, exp75 원본/tol3 후보는 turnover/year 1026.3%/1009.0%, exp75 tol4 후보는 turnover/year 986.5%로 회전율은 통과했지만 MDD -20.25%라 모두 `paper_only`; tol4 guard-only, rank-risk 강화, sector cap 2, corrcap85, corrpen 후보도 canonical 신규 승격 없음. 손실 후 재진입 제한 후보는 top-200에서 MDD 악화로 canonical 제외. 기존 risk-overlay 후보만 provisional 유지
> - target-weight 변동성 예산 canonical 평가: top-200에서는 후보 2개가 통과했지만 canonical top-20에서는 MDD -20.82%/-20.89%로 `paper_only`; 초과수익·회전율은 개선됐으나 live/paper 신규 전환은 금지
> - target-weight 포지션 손실 감산 canonical 평가: `tol5+sectorcap2+posloss8` 후보가 return +198.15%, MDD -17.18%, turnover/year 993.9%로 신규 `provisional_paper_candidate`; pilot plan은 rank penalty/sector cap/포지션 손실 감산과 paper evidence 기반 portfolio drawdown guard 상태를 재현하며, 명시 상태가 없으면 fail-closed 차단
> - scoring: **paper_only** (관찰 가능하지만 Sharpe/PF/WF 안정성 미달)
> - rotation: **provisional_paper_candidate** (risk-adjusted 기준 통과, live alpha는 미확인)
> - target-weight risk overlay 후보: canonical bundle 기준 **provisional_paper_candidate** + 전용 paper/pilot adapter/shadow proof, 유동성/비용 pre-trade/pilot 승인/실행일/장 시간/가격 최신성 guard 추가. 리서치 백테스트는 직전 거래일 점수 → 다음 거래일 시가 체결 → 종가 평가 기준으로 보수화했으며, 기존 target-weight research artifact는 execution price mode 확인 또는 재생성 후 사용 (live 미연결)
> - target-weight capped pilot readiness: audit 시작 시 paper preflight를 먼저 갱신하고, Discord webhook 누락 또는 notifier 비정상 상태는 주문 전 `BLOCKED`로 확정
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
- target-weight capped paper pilot은 Discord webhook이 필수입니다. `.env`의 `DISCORD_WEBHOOK_URL`을 채우고 `config/settings.yaml`의 `discord.enabled: true`를 유지하세요.
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

# Target-weight capped pilot preflight / readiness audit
python tools/paper_preflight.py --strategy target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1 --with-pilot-check
python tools/target_weight_rotation_pilot.py --candidate-id target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1 --readiness-audit --allow-rerun

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

Target-weight capped pilot의 `--readiness-audit`는 주문 가능 여부를 판정하기 전에 `paper_preflight`를 먼저 갱신하고 그 결과를 `preflight_refresh` artifact에 남깁니다. `notifier: Discord webhook 미설정` 또는 notifier health 비정상 상태가 나오면 pilot authorization이나 cap이 맞아도 실행 전 `BLOCKED`로 유지되므로, `.env`의 `DISCORD_WEBHOOK_URL` 설정 후 preflight와 readiness audit을 다시 돌려야 합니다.

실전 매매는 `ENABLE_LIVE_TRADING=true` + `--confirm-live` + 전략 상태 `live_candidate` + 현재 commit/config와 일치하는 canonical promotion bundle + 내부 `strategy`가 현재 전략명과 정확히 일치하는 `ELIGIBLE` paper evidence package가 모두 필요합니다. promotion engine과 live gate는 same-universe/cash-adjusted paper excess가 모두 양수인지 확인하며, live gate는 `promotion_result.json` 표기를 그대로 믿지 않고 같은 산출물과 evidence를 승격 엔진으로 재계산해 현재 규칙에서도 `live_candidate`인지 다시 확인합니다. live 스케줄러 시작 전 KIS 연결 검증과 KIS↔DB 잔고 동기화도 반드시 통과해야 하며, 실패하면 경고로 넘기지 않고 시작을 차단합니다.
현재 모든 전략은 `provisional_paper_candidate` 또는 `disabled` 상태이며, **live 모드는 차단**되어 있습니다.  
`reports/approved_strategies.json`와 오래된 `validation_walkforward_*.json` 파일은 더 이상 live 근거가 아닙니다. `--force-live` 플래그는 제거되었으며, 어떤 조합으로도 hard gate를 우회할 수 없습니다.

## 리스크 관리

기본적인 안전장치는 넣어두었습니다.

- look-ahead 완화 백테스트
- 포지션 수 / 자금 비중 제한
- 미체결 / 중복 주문 방지 (live 미체결 조회 실패 시 주문 보류)
- live 주문 체결 확인 전 DB 거래·포지션 반영 보류
- live 재시작 시 브로커 미체결 목록에서 사라진 보류 주문 상태 대조 완료 처리
- live 시작 전 KIS 연결 / 잔고 동기화 실패 시 스케줄러 시작 차단
- live 긴급 청산 전 KIS-only 보유 포지션을 DB에 먼저 반영
- HTTP 긴급 청산 트리거 live 실행은 별도 확인 환경변수 없이는 차단
- HTTP 긴급 청산은 개별 매도 실패가 있으면 실패 응답으로 노출
- HTTP 긴급 청산 실행은 POST 전용이며 query token 인증은 기본 비활성
- HTTP 긴급 청산 서버는 기본적으로 127.0.0.1에만 바인드
- HTTP 긴급 청산 토큰은 기본 최소 16자와 placeholder 차단 검증 적용
- 긴급 청산 결과는 성공/실패 summary를 통합 알림으로 전파
- paper/live 현금 정산은 실제 체결가 기준이며 슬리피지를 현금 흐름에서 중복 차감하지 않음
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

승격 규칙 v3 — `core/promotion_engine.py`에서 metrics 기반 자동 판정. `tools/evaluate_and_promote.py --canonical`로 재현하며, canonical 평가 산출물에는 종목군 구성, 데이터 범위, 수집 오류를 바탕으로 만든 `data_snapshot_hash`를 남긴다. live gate와 승격 산출물 로더는 이 해시, 데이터 범위, 수집 오류, 평가 실패 상태를 다시 검증하고, live 진입 직전에 promotion engine 판정을 재계산해 손상되거나 현재 규칙과 어긋난 산출물 사용을 차단한다.
Research candidate sweep — `tools/research_candidate_sweep.py --quick --candidate-family all`로 promotion과 분리된 rotation/momentum/breakout/pullback/benchmark-relative/risk-budget/cash-switch/benchmark-aware rotation/target-weight top-N rotation 후보 랭킹 artifact를 생성. Raw EW B&H gate는 유지하되, defensive/cash-heavy 후보 해석을 위해 평균 노출률과 exposure-matched B&H excess도 진단값으로 기록합니다. 벤치마크 universe 일부 종목이라도 OHLCV 수집·기간 검증에 실패하면 `INSUFFICIENT_BENCHMARK_DATA`로 fail-closed 차단하고, 결측 종목과 커버리지 비율을 artifact/Markdown에 남깁니다. `--top-n 200`처럼 넓은 유니버스 검증을 요청하면 기본 후보 스캔 범위를 `max(100, top_n*2)`로 넓히며, 필요 시 `--universe-scan-limit`으로 명시 조정할 수 있습니다. 기존 best 후보만 빠르게 재검증할 때는 `--candidate-id`를 반복 또는 쉼표 구분으로 넘겨 선택한 후보만 평가합니다. top-200 follow-up처럼 MDD/회전율 완화 후보만 비교할 때는 `--candidate-family target_weight_risk_relief`로 risk-off/tolerance/exposure 축소 후보군을 바로 실행하고, 리밸런싱 빈도 완화 후보만 볼 때는 `--candidate-family target_weight_turnover_relief`로 격월/분기 후보군을 실행합니다. 손실 구간 노출을 더 직접 낮추는 후보는 `--candidate-family target_weight_volatility_target`로 benchmark 실현 변동성 타깃과 낙폭 floor를 적용해 검증합니다. 종목 선택 자체를 보수화하는 후속 비교는 `--candidate-family target_weight_downside_rank_relief`로 낙폭·하방변동성 페널티를 랭킹 점수에 반영해 검증합니다. 랭킹 페널티 기반 후보의 교체 빈도를 줄이는 후속 비교는 `--candidate-family target_weight_churn_relief`로 격월 리밸런싱과 리밸런싱당 신규 편입 수 상한(`max_new_targets_per_rebalance`)을 결합해 검증합니다. 포트폴리오 손실 구간 자체를 줄이는 후속 비교는 `--candidate-family target_weight_drawdown_guard`로 직전 평가 NAV 기반 drawdown guard와 재진입 cooldown을 결합해 검증합니다. 선정 종목의 목표 금액 자체를 변동성 예산형으로 조정하는 후속 비교는 `--candidate-family target_weight_volatility_budget`로 역변동성 sleeve weighting을 적용합니다. target-weight 후보 평가는 같은 sweep 안에서 OHLCV fetch cache를 공유해 동일 종목·기간 반복 수집을 줄이고, cache hit/unique fetch 진단값을 artifact와 Markdown에 남깁니다. sweep artifact와 Markdown에는 선택 방식, 요청 top-N, 스캔 한도, 후보 필터, 유동성 필터 전후 종목 수, 후보별 탈락 사유와 반복 병목 요약을 남겨 실제 검증 범위와 게이트 병목을 확인합니다. target-weight 후보는 `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금으로 남기고, `hold_rank_buffer`와 `max_new_targets_per_rebalance`로 랭킹 흔들림에 따른 불필요한 교체와 신규 편입 수를 줄이며, `target_allocation_mode=inverse_volatility`로 선택 종목별 목표 금액을 rolling 변동성의 역수 기준으로 나누고, `portfolio_drawdown_guard_trigger_pct`/`portfolio_drawdown_guard_exposure`/`portfolio_drawdown_guard_cooldown_rebalances`로 포트폴리오 낙폭 후 목표 노출과 재진입 속도를 제한하고, `market_exposure_mode=benchmark_risk`로 KS11 SMA/낙폭/변동성 risk-off 구간의 부분 노출 축소를 검증합니다. canonical risk-overlay 후보에는 `target_tolerance_pct=3/5` turnover-aware 변형도 포함해 작은 리밸런싱 생략이 비용과 성과에 주는 영향을 비교합니다. target-weight 리서치 백테스트는 직전 거래일 점수로 다음 거래일 시가에 리밸런싱하고 일말 평가는 종가로 하며, 전일 기준 20일 평균 거래량을 비용 계산에 넘겨 동적 슬리피지와 participation 진단값을 남깁니다.
Paper Evidence 체계 — `core/paper_evidence.py` v2 일별 22개 지표 자동 수집, `core/paper_runtime.py` entry gate, `core/paper_pilot.py` launch readiness/pilot auth 판정. scheduler는 v2 collector만 canonical 증거로 기록하며, legacy `core/evidence_collector.py`는 import 호환용 deprecated no-op입니다. 승격 패키지는 `execution_backed=True`와 `real_paper`/`pilot_paper` 출처가 명시된 기록만 승격 증거로 인정하고, 패키지 내부 `strategy`가 현재 전략명과 정확히 일치하지 않으면 승격/라이브 증거로 쓰지 않아 예전 형식·수작업 기록 오염을 차단한다.

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

2026-05-12 follow-up: 단일/포트폴리오 백테스트 리포트에 비용 전/후 성과 비교를 자동 노출합니다. `backtest.cost_impact`가 수수료·세금·슬리피지를 표준 집계해 비용 차감 전 추정 수익률, 비용 드래그(bp), 비용/순손익, cost impact status를 남기며, 비용 때문에 gross profit이 net loss로 뒤집히거나 비용이 순이익을 초과하면 운영 검토 신호로 표시합니다.

2026-05-08 follow-up: `tools/research_candidate_sweep.py`의 target-weight 리서치 백테스트도 OHLCV `volume`으로 종목별 20일 평균 거래량을 계산해 매수·매도 비용에 전달합니다. target-weight trade와 metrics에는 `avg_daily_volume`, `participation_rate`, `slippage_multiplier`, `slippage_cost_total`이 남아 high-turnover 후보의 비용 과소추정을 더 빨리 확인할 수 있습니다.

2026-05-12 follow-up: target-weight research sweep에 canonical risk-overlay 후보의 `target_tolerance_pct=3/5` 변형을 추가했습니다. 리서치 metrics에는 `rebalance_tolerance_pct`, `rebalance_tolerance_skipped_trades`, `rebalance_tolerance_skipped_notional`을 남겨 넓은 tolerance가 실제로 turnover를 줄였는지 artifact에서 바로 확인할 수 있습니다.

2026-05-08 follow-up: `research_candidate_sweep`의 EW B&H 벤치마크가 일부 종목 결측 상태에서 전체 capital 대비 낮게 계산되어 후보 초과수익이 과대평가되는 경로를 차단했습니다. 벤치마크 입력 universe 전체가 수집·검증되지 않으면 excess gate와 decision action은 `INSUFFICIENT_BENCHMARK_DATA`로 고정됩니다.

2026-05-08 follow-up: `OrderExecutor` live BUY/SELL은 주문 ACK 이후 체결가·체결수량 확인이 되지 않거나 부분체결만 확인되면 더 이상 예상가 기준 전량 FILLED로 처리하지 않습니다. 이때 `success=False`는 브로커 주문 부재가 아니라 `order_pending=True`/`requires_reconcile=True`인 장부 반영 보류 상태이며, KIS 잔고 대조 전 DB 포지션·거래 기록 오염을 막습니다.

2026-05-11 follow-up: paper BUY 수량·손절·익절·트레일링 기준을 예상 체결가로 보수화하고, paper SELL도 매수처럼 모델 슬리피지를 체결가에 반영합니다. `TradeHistory.price`는 실제 체결가로 보고, `get_trade_cash_summary()`는 슬리피지를 진단값으로 집계하되 현금 흐름에서는 수수료·세금만 별도 차감해 체결가에 이미 들어간 비용이 중복 반영되지 않게 했습니다.

2026-05-08 follow-up: KIS 미체결 조회 실패를 더 이상 “미체결 없음”으로 해석하지 않습니다. live BUY/SELL은 주문 전 미체결 조회가 실패하거나 응답 형식이 불명확하면 `live_unfilled_check.checked=False`로 주문을 보류하고, 재시작 복구에서도 KIS 미체결 조회 실패를 별도 critical 알림으로 드러냅니다.

2026-05-08 follow-up: target-weight 리서치 백테스트의 리밸런싱 체결 기준을 당일 종가에서 다음 거래일 시가로 보수화했습니다. 랭킹과 risk-off 판단은 직전 거래일 종가까지만 사용하고, 리밸런싱 체결은 해당 거래일 원본 `open`, 일말 평가는 `close`로 분리합니다. 신규 top-N 매수 후보의 리밸런싱일 `open`이 누락되면 `target_weight_research_execution_price_missing`으로 중단하고, 이미 보유한 종목의 `open`이 없으면 해당 리밸런싱을 거래 없이 skip 진단으로 남깁니다. 동적 슬리피지의 20일 평균 거래량도 당일 거래량을 포함하지 않도록 1거래일 지연합니다.

2026-04-30 benchmark-aware rotation smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`benchmark_aware_rotation_60_120_balanced` return=+21.65%, Sharpe=0.50, avg exposure=24.1%였지만 raw excess=-151.98%p, exposure-matched excess=-16.05%p라 promotion 미진행. fast `40_100_dense`는 exposure-matched excess=+2.04%p였으나 raw excess=-163.35%p라 다음 연구 힌트로만 기록합니다. 다음 방향은 sparse BUY/SELL 신호를 넘어 monthly top-N 목표비중 리밸런싱을 별도 백테스터로 검증하는 것입니다.

2026-04-30 target-weight top-N rotation smoke sweep: 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`target_weight_rotation_top3_40_100_excess` return=+128.44%, Sharpe=1.13, avg exposure=85.3%로 노출 부족 문제는 해결했지만 raw excess=-45.19%p, exposure-matched excess=-14.82%p라 promotion 미진행. 이 결과는 sparse 신호가 병목이었음을 확인했지만, 동일 유니버스 B&H를 이기는 alpha는 아직 아니라서 다음 연구는 더 넓은 유니버스와 부분 hedge/상대강도 필터 개선입니다.

2026-04-30 canonical top-20 target-weight full sweep: 기존 `target_weight_rotation_top5_60_120_floor0_hold3`는 return=+278.57%, raw excess=+129.18%p, exposure-matched excess=+150.88%p, Sharpe=1.65, WF positive/Sh+ 100%, turnover/year=807.8%였지만 MDD=-28.25%로 `paper_only`. benchmark-risk overlay 추가 후 best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`는 return=+210.24%, raw excess=+60.85%p, exposure-matched excess=+130.96%p, Sharpe=1.60, PF=5.73, MDD=-19.24%, turnover/year=858.0%, WF positive/Sh+ 100%, risk-off rebalance=38.9%로 처음 `provisional_paper_candidate`에 도달했습니다. 후속으로 `tools/evaluate_and_promote.py --canonical`이 이 후보를 canonical promotion bundle에 재현하도록 연결했고, `promotion_result.json`에서도 동일 후보가 `provisional_paper_candidate`로 로드됩니다. 추가로 `tools/target_weight_rotation_pilot.py`가 portfolio-level 목표비중 plan을 만들고 pilot cap을 검증한 뒤 paper-only exact-quantity 주문을 낼 수 있게 했습니다. dry-run은 `--record-shadow-evidence`로 non-promotable `shadow_bootstrap` evidence를 남기고, 같은 실행에서 launch readiness JSON/MD와 pilot runbook을 생성합니다. session artifact에는 기본 cap preview, plan 기반 최소/추천 pilot cap, enable 명령, launch artifact 경로가 함께 기록됩니다. live 자동운영은 여전히 금지입니다.

2026-05-11 follow-up: target-weight canonical을 next-open 체결과 결측 진단 기준으로 재검증했습니다. `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`는 return=+171.20%, PF=4.24, Sharpe=1.41, MDD=-19.90%, WF positive=100%, WF Sharpe+=83.3%로 `provisional_paper_candidate`를 유지했습니다. 이번 산출물은 stale score 후보 2개 제외, held stale valuation 21일, 보유 종목 시가 누락에 따른 리밸런싱 skip 1회를 metrics에 남기므로 paper pilot에서는 체결 가능성과 stale valuation 빈도를 함께 감시해야 합니다.

2026-05-12 top-200 target-weight follow-up: `--top-n 200 --candidate-id target_weight_rotation_top5_60_120_floor0_hold3_risk60_35` full sweep을 실행했습니다. canonical liquidity 200개 중 유동성 필터 통과 164개, benchmark coverage 100%에서 return=+110.39%, raw excess=+78.50%p, exposure-matched excess=+90.25%p, Sharpe=0.85, PF=2.06, WF positive=83.3%, WF Sharpe+=100%였지만 MDD=-25.79%, turnover/year=1097.1%로 provisional 게이트를 넘지 못해 `paper_only`입니다. 이 결과를 기준으로 alpha 존재 여부보다 drawdown과 turnover를 동시에 낮추는 리스크 완화 후보군 검증으로 이어갔습니다.

2026-05-12 리스크 완화 top-200 follow-up: `--candidate-family target_weight_risk_relief --top-n 200` full sweep에서 10개 후보를 비교했습니다. 최상위 후보 `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5`는 return=+125.61%, raw excess=+93.72%p, exposure-matched excess=+104.53%p, avg exposure=68.5%, Sharpe=0.91, PF=2.19, WF positive=83.3%, WF Sharpe+=100%로 기존 단일 후보보다 수익성은 개선됐습니다. 다만 전체 후보가 MDD -25.27%~-32.14%, turnover/year 1027.2%~1344.3% 구간에 있어 `mdd < -20`와 `turnover_per_year >= 1000` 병목을 넘지 못했고, 판정은 `KEEP_RESEARCH_ONLY`입니다. sweep data fetch cache는 unique_fetches=1155, cache_hits=10395로 동작을 확인했습니다. 다음 연구는 리밸런싱 빈도 자체를 낮추는 격월/분기 리밸런싱, 변동성 타깃, 낙폭 차단, 회전율 패널티 랭킹을 우선합니다.

2026-05-12 저회전 top-200 follow-up: `--candidate-family target_weight_turnover_relief --top-n 200` full sweep에서 격월/분기 리밸런싱 후보 6개를 검증했습니다. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk90_35_bimonthly`는 return=+85.72%, raw excess=+53.83%p, exposure-matched excess=+68.63%p, Sharpe=0.73, PF=1.86, turnover/year=616.7%였습니다. 분기 후보의 turnover/year는 456.1~476.9%까지 내려갔지만, 전 후보가 benchmark excess Sharpe<=0 및 MDD -25.35%~-35.12%로 막혀 판정은 `NO_ALPHA_CANDIDATE`입니다. 즉 단순 리밸런싱 빈도 축소는 회전율 병목은 완화하지만 위험조정 alpha와 낙폭 문제를 해결하지 못했고, 이 판단을 바탕으로 변동성 타깃과 낙폭 차단을 실제 포지션 크기 산식에 넣는 후보군을 이어서 검증했습니다.

2026-05-12 변동성 타깃 top-200 follow-up: `--candidate-family target_weight_volatility_target --top-n 200` full sweep에서 benchmark 실현 변동성 타깃과 drawdown floor 후보 6개를 검증했습니다. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_vol16_dd8_floor35`는 return=+105.18%, raw excess=+73.29%p, exposure-matched excess=+91.35%p, avg exposure=71.7%, Sharpe=0.81, PF=2.03, turnover/year=958.0%였습니다. 다만 전 후보가 MDD -24.26%~-31.04%로 provisional MDD 게이트를 통과하지 못했고, 5개 후보는 benchmark excess Sharpe도 0 이하라 판정은 `KEEP_RESEARCH_ONLY`입니다. 변동성 기반 노출 축소만으로는 낙폭 병목이 충분히 풀리지 않았으므로 다음 연구는 목표 노출 산식보다 종목 선별 랭킹에 낙폭·하방변동성·상관/업종 집중 페널티를 넣는 방향으로 전환합니다.

2026-05-12 리스크 페널티 랭킹 top-200 follow-up: `--candidate-family target_weight_downside_rank_relief --top-n 200` full sweep에서 낙폭·하방변동성 페널티 후보 5개를 검증했습니다. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60`는 return=+132.51%, raw excess=+100.62%p, exposure-matched excess=+111.43%p, Sharpe=0.94, PF=2.21, WF positive/Sh+=100%로 기존 리스크 완화 후보보다 수익성은 개선됐습니다. 다만 전 후보가 MDD -25.20%~-33.07%, turnover/year 1143.8%~1366.0%로 막혀 판정은 `KEEP_RESEARCH_ONLY`입니다. 즉 downside rank penalty는 alpha 후보성을 강화했지만, 종목 교체 빈도를 낮추지 못했습니다. 다음 연구는 이 랭킹 페널티를 격월 리밸런싱, 더 넓은 tolerance, 월별 신규 편입 수 제한 같은 churn control과 결합하는 방향입니다.

2026-05-12 follow-up: `target_weight_churn_relief` 후보군을 추가했습니다. 직전 best였던 downside rank penalty 후보를 기준으로 `max_new_targets_per_rebalance` 신규 편입 상한, 격월 리밸런싱, 넓은 tolerance 조합을 별도 family로 분리해 top-200 full sweep에서 turnover/year와 MDD 병목이 실제로 완화되는지 확인할 수 있게 했습니다.

2026-05-12 churn control top-200 follow-up: `--candidate-family target_weight_churn_relief --top-n 200` full sweep에서 후보 5개를 검증했고 판정은 `KEEP_RESEARCH_ONLY`입니다. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew2`는 return=+118.89%, raw excess=+87.00%p, exposure-matched excess=+97.61%p, Sharpe=0.89, PF=2.04, WF positive/Sh+=100%였지만 MDD=-26.95%, turnover/year=1034.2%로 provisional 게이트를 넘지 못했습니다. `maxnew1`과 격월 후보는 turnover/year를 423.0~674.4%까지 낮췄지만 benchmark excess Sharpe와 MDD가 악화됐습니다. 다음 연구는 단순 교체 제한보다 포트폴리오 drawdown guard, 재진입 cooldown, 상관/업종 집중도 페널티처럼 손실 구간 자체를 줄이는 구조가 우선입니다.

2026-05-12 follow-up: `target_weight_drawdown_guard` 후보군을 추가했습니다. 직전 평가 NAV 기준 포트폴리오 낙폭이 임계값을 넘으면 다음 리밸런싱 목표 노출을 floor까지 낮추고, 회복 직후에도 지정 리밸런싱 횟수만큼 cooldown을 유지합니다. 다음 검증은 `--candidate-family target_weight_drawdown_guard --top-n 200` full sweep으로 MDD가 실제로 낮아지는지 확인합니다.

2026-05-12 손실방어 top-200 follow-up: `--candidate-family target_weight_drawdown_guard --top-n 200` full sweep에서 후보 5개를 검증했고 판정은 `RUN_CANONICAL_EVALUATION`입니다. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_pdd8_floor25_cd1`는 return=+99.53%, raw excess=+67.64%p, exposure-matched excess=+83.07%p, avg exposure=56.5%, Sharpe=1.02, PF=4.88, MDD=-19.56%, turnover/year=296.4%, WF positive/Sh+=66.7%였습니다. 포트폴리오 guard는 trigger 17회, guard rebalance 20회(74.1%)로 동작했습니다. 두 번째 후보 `target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_pdd10_floor40_cd1`도 provisional 조건을 통과했지만 turnover/year=893.4%로 비용 민감도가 더 큽니다. 나머지 3개는 MDD -20% 미만 또는 benchmark excess Sharpe<=0로 `paper_only`입니다. 다음 단계는 eligible 2개 후보를 canonical promotion evaluation에 올려 같은 기준에서 재현성을 확인하는 것이며, 이 artifact만으로 paper/live 전환은 하지 않습니다.

2026-05-13 손실방어 canonical follow-up: `tools/evaluate_and_promote.py --canonical`에 eligible 2개 손실방어 후보를 포함해 재평가했습니다. 기존 `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`는 return=+171.20%, benchmark excess=+21.81%p, Sharpe=1.41, PF=4.24, MDD=-19.90%, turnover/year=794.0%, WF positive=100%, WF Sharpe+=83.3%로 `provisional_paper_candidate`를 유지했습니다. pdd8/floor25 후보는 return=+142.93%, Sharpe=1.43, PF=7.69, MDD=-15.89%, turnover/year=530.8%로 방어력은 좋아졌지만 canonical benchmark excess=-6.46%p라 `paper_only`입니다. exp75/rankrisk90/pdd10/floor40 후보는 return=+180.40%, benchmark excess=+31.01%p, Sharpe=1.49, PF=4.85, MDD=-19.24%, WF positive/Sh+=100%였지만 turnover/year=1026.3%로 게이트를 넘겨 `paper_only`입니다. 다음 연구는 exp75 계열에 tolerance 또는 신규 편입 제한을 결합해 초과수익과 WF 안정성은 유지하면서 turnover/year를 1000% 아래로 낮추는 방향입니다.

2026-05-13 exp75 회전율 완화 follow-up: `target_weight_drawdown_guard` family에 exp75/rankrisk90/pdd10/floor40 기반 `tol3`, `tol4`, `tol5`, `maxnew2`, `tol3_maxnew2` 변형을 추가했습니다. `--candidate-family target_weight_drawdown_guard --top-n 200` 재검증에서는 후보 10개 중 4개가 canonical 평가 대상으로 남았고, 새 `tol4` 후보는 return=+81.19%, raw excess=+49.30%p, exposure-matched excess=+66.89%p, MDD=-19.33%, turnover/year=886.7%, WF positive/Sh+=100%로 research 기준을 통과했습니다. canonical 재평가에서는 `target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1`가 return=+183.02%, benchmark excess=+33.63%p, Sharpe=1.50, PF=4.82, turnover/year=986.5%로 회전율 게이트는 통과했지만 MDD=-20.25%가 -20% 제한을 0.25%p 초과해 `paper_only`입니다. tol3는 turnover/year=1009.0%, 원본은 1026.3%로 각각 회전율 미달입니다. 다음 연구는 tol4의 낮아진 회전율을 유지하면서 drawdown floor/cooldown 또는 ranking risk penalty를 더 보수화해 MDD를 -20% 안쪽으로 되돌리는 방향이며, 현재 live/paper 전환 대상은 기존 risk-overlay 후보만 유지합니다.

2026-05-13 tol4 낙폭 완화 follow-up: tol4 기반으로 `pdd10_floor35_cd1`, `pdd8_floor40_cd1`, `pdd8_floor35_cd1`, `pdd10_floor40_cd2` 네 변형을 추가해 `target_weight_drawdown_guard --top-n 200` full sweep을 다시 실행했습니다. 후보 14개 중 신규 변형은 canonical eligible에 추가되지 않았습니다. `pdd10_floor35`는 turnover/year=829.8%로 낮아졌지만 MDD=-20.20%, `pdd8_floor35`도 MDD=-20.20%라 실패했고, `pdd8_floor40`과 `cooldown2`는 MDD/turnover는 통과권이었지만 benchmark excess Sharpe<=0으로 제외됐습니다. 결론적으로 guard trigger/floor/cooldown만 더 조이는 방식은 alpha 또는 MDD 중 하나를 잃기 쉬우므로, 다음 후보는 종목 선택 단계의 downside rank penalty/상관·업종 집중도 페널티나 비용 민감도 기준을 함께 조정하는 쪽이 우선입니다.

2026-05-13 rank-risk 보강 follow-up: exp75 계열에 `rankrisk90_dd75`, `rankrisk120`을 추가하고 tol4+pdd10/floor40 guard와 결합했습니다. `target_weight_drawdown_guard --top-n 200` 후보 16개 재검증에서는 `rankrisk120_tol4`가 return=+84.41%, raw excess=+52.52%p, MDD=-19.30%, turnover/year=919.4%, `rankrisk90_dd75_tol4`가 return=+82.56%, raw excess=+50.67%p, MDD=-19.54%, turnover/year=896.0%로 기존 tol4보다 좋아져 canonical 평가 대상에 포함됐습니다. 하지만 canonical top-20 재평가에서는 각각 return=+125.69%/+125.99%, turnover/year=927.9%/947.6%였지만 MDD=-20.74%/-20.76%와 benchmark excess=-23.70%p/-23.40%p로 `paper_only`입니다. 패널티 강도만 키우면 top-200에서는 좋아져도 canonical 대형주 구간 alpha가 꺾이므로, 다음은 sector/correlation cap처럼 분산 제약을 선택 단계에 직접 넣는 쪽을 우선합니다.

2026-05-13 sector cap follow-up: FDR `KRX` 목록에 업종 컬럼이 없는 환경에서도 `get_sector_map()`이 KRX KIND 상장법인 목록(2,766개 업종 매핑)으로 fallback하도록 보강하고, `target_weight_drawdown_guard --top-n 200`에 `sectorcap1/2` 후보를 추가했습니다. `sectorcap2`는 실제 섹터 맵 적용 기준으로 `rankrisk120_tol4_sectorcap2` return=+85.49%, raw excess=+53.60%p, MDD=-19.94%, turnover/year=941.5%, `rankrisk90_tol4_sectorcap2` return=+82.57%, raw excess=+50.68%p, MDD=-19.59%, turnover/year=909.3%로 canonical 평가 대상에 포함됐습니다. canonical top-20에서는 `rankrisk90_tol4_sectorcap2`가 return=+185.39%, benchmark excess=+36.00%p, turnover/year=981.0%로 원본 tol4보다 좋아졌지만 MDD=-20.22%라 `paper_only`입니다. `rankrisk120_tol4_sectorcap2`는 sector cap 적용 후에도 canonical 결과가 기존과 같아 benchmark excess=-23.70%p, MDD=-20.74%로 제외했습니다. 다음 후보는 섹터 수량 cap만으로 부족한 낙폭 0.22%p를 줄이기 위해 상관도 cap, 월별 손실 이후 재진입 제한, 변동성 예산형 target selection을 결합하는 쪽입니다.

2026-05-13 correlation cap follow-up: target selection 단계에 `max_pairwise_correlation`, `correlation_lookback_days`, `correlation_min_periods`를 추가해 이미 선택된 종목과의 과거 수익률 상관이 임계값을 넘는 후보를 건너뛰도록 했습니다. `target_weight_drawdown_guard --top-n 200`에서는 `corrcap85`와 `sectorcap2_corrcap85`가 canonical 평가 대상에 포함됐고 관측 최대 선택 상관도는 0.8147로 제한됐지만, return/MDD/turnover는 각각 기존 tol4 및 sectorcap2와 동일했습니다. `corrcap80`은 관측 상관도를 0.7675까지 낮췄지만 return=+70.22%, raw excess=+38.33%, MDD=-19.33%, turnover/year=908.3%에서 benchmark excess Sharpe<=0으로 제외됐습니다. canonical top-20에서도 `corrcap85`는 MDD=-20.25%, `sectorcap2_corrcap85`는 MDD=-20.22%로 기존 후보와 같은 `paper_only`입니다. 결론적으로 0.85 단순 pairwise cap은 너무 느슨하고, 0.80 단독 cap은 alpha를 깎으므로 다음은 상관도 penalty를 랭킹 점수에 직접 섞거나 변동성 예산/손실 후 재진입 제한과 결합해야 합니다.

2026-05-13 correlation rank penalty follow-up: 단순 hard cap 대신 리밸런싱 시점의 `score_row`에 평균 양의 상관도 기반 `correlation_rank_penalty_weight`를 차감하는 후보를 추가했습니다. top-200 `target_weight_drawdown_guard`에서는 `sectorcap2_corrpen05`가 return=+84.22%, raw excess=+52.33%p, MDD=-19.59%, turnover/year=912.5%, `corrpen10`이 return=+83.50%, raw excess=+51.61%p, MDD=-19.96%, turnover/year=892.3%, `corrpen05`가 return=+82.82%, raw excess=+50.93%p, MDD=-19.33%, turnover/year=889.9%로 모두 canonical 후보에 포함됐습니다. 하지만 canonical top-20에서는 `corrpen05` return=+141.09%, benchmark excess=-8.30%p, MDD=-20.25%, `corrpen10` return=+128.43%, benchmark excess=-20.96%p, MDD=-20.25%, `sectorcap2_corrpen05` return=+143.11%, benchmark excess=-6.28%p, MDD=-20.22%로 모두 `paper_only`입니다. 결론적으로 평균 상관도 점수 차감은 top-200에서는 분산 개선 신호가 있었지만 대형주 canonical alpha를 크게 깎아 운영 승격에는 부적합합니다. 다음은 점수 차감보다 손실 후 재진입 제한이나 변동성 예산형 target selection처럼 낙폭을 직접 겨냥하는 제약을 우선합니다.

2026-05-13 loss re-entry guard follow-up: 직전 실행 리밸런스 후 평가 NAV 대비 손실률이 `loss_reentry_guard_trigger_pct`를 넘으면 지정 cooldown 동안 신규 편입 수를 `loss_reentry_guard_max_new_targets`로 낮추는 옵션을 추가했습니다. `target_weight_drawdown_guard --top-n 200`에 `reentry4_maxnew0_cd1`, `reentry3_maxnew0_cd1`, `sectorcap2_reentry4_maxnew0_cd1` 후보를 추가했지만, 손실 직후 새 종목 편입을 막는 방식은 회전율만 낮추고 MDD를 악화시켰습니다. `sectorcap2_reentry4`는 return=+84.68%, raw excess=+52.79%p, turnover/year=793.8%였지만 MDD=-22.34%, `reentry4`는 return=+57.71%, MDD=-27.29%, `reentry3`은 return=+28.93%, MDD=-35.30%로 모두 `paper_only`라 canonical 평가 대상에서 제외했습니다. 다음은 보유 종목 고착을 만드는 신규 편입 차단보다, 종목별 변동성 예산이나 포지션별 손절/감산처럼 손실 원인을 직접 줄이는 제약이 우선입니다.

2026-05-13 position loss reduction follow-up: 신규 편입을 막아 손실 종목을 고착시키는 대신, 직전 확정 종가 기준 보유 종목 손실률이 `position_loss_reduce_trigger_pct`를 넘으면 다음 리밸런싱 목표 금액을 `position_loss_reduce_target_fraction`까지 감산하는 옵션을 추가했습니다. 감산 매도는 기존 비용/PnL/turnover 경로를 그대로 타며, 리밸런싱 tolerance 때문에 위험 감산이 생략되지 않도록 분리했습니다. 실데이터 `target_weight_drawdown_guard --top-n 200` full sweep에서는 `sectorcap2_posloss8` 후보가 return=+105.79%, raw excess=+73.90%p, MDD=-18.96%, Sharpe=1.00으로 통과했지만 canonical에서는 turnover/year=1006.2%로 `paper_only`였습니다. 후속으로 `tol5`를 결합하자 `target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1`가 top-200 return=+112.16%, raw excess=+80.27%p, MDD=-19.23%, Sharpe=1.04로 통과했고, canonical top-20에서도 return=+198.15%, benchmark excess=+48.76%p, Sharpe=1.57, PF=5.58, MDD=-17.18%, turnover/year=993.9%, WF positive/Sh+=100%로 신규 `provisional_paper_candidate`가 됐습니다. 단 live는 여전히 60일 paper evidence와 target-weight verified proof가 없어 차단됩니다. `core.target_weight_rotation.build_target_weight_plan()`은 paper/pilot plan에서도 `rank_penalty_mode=downside_risk`, `max_targets_per_sector`, `position_loss_reduce_*`, `portfolio_drawdown_guard_*`를 적용하고, sector map 누락 또는 drawdown guard 명시 상태 누락은 fail-closed로 막습니다. pilot adapter는 기존 paper evidence의 total NAV, peak, guard cooldown을 상태로 복원하며 evidence snapshot에도 guard 결과를 남겨 다음 리밸런싱의 cooldown을 이어갑니다.

2026-05-13 volatility budget target selection follow-up: 선택 종목은 유지하되 목표 금액을 동일가중이 아니라 rolling 실현 변동성의 역수로 배분하는 `target_allocation_mode=inverse_volatility`를 target-weight research backtester에 추가했습니다. `allocation_vol_lookback_days`, `allocation_vol_min_periods`, `allocation_vol_floor_pct`, `allocation_max_sleeve_weight_pct`를 artifact metrics에 남깁니다. 실데이터 `target_weight_volatility_budget --top-n 200` full sweep에서는 canonical liquidity 200개 중 유동성 통과 164개, benchmark coverage 100%에서 best=`target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35`가 return=+78.77%, raw excess=+46.88%p, exposure-matched excess=+63.79%p, avg exposure=55.2%, Sharpe=0.81, PF=1.96, MDD=-18.96%, trades=107로 `provisional_paper_candidate`가 됐습니다. sectorcap2+volbudget60 후보도 return=+77.18%, raw excess=+45.29%p, MDD=-19.19%로 통과했고, volbudget90 후보는 benchmark excess Sharpe<=0 병목이라 제외했습니다. 하지만 `tools/evaluate_and_promote.py --canonical` 재평가에서는 volbudget60 후보가 return=+170.73%, benchmark excess=+21.34%p, Sharpe=1.44, PF=4.53, turnover/year=982.6%, WF positive/Sh+=100%였지만 MDD=-20.82%로 `paper_only`입니다. sectorcap2+volbudget60도 return=+173.80%, benchmark excess=+24.41%p, Sharpe=1.46, PF=4.68, turnover/year=978.1%, WF positive/Sh+=100%였지만 MDD=-20.89%라 `paper_only`입니다. 변동성 예산은 회전율과 초과수익을 유지했지만 대형주 canonical MDD가 더 악화됐으므로 paper/live 전환 대상은 아니며, research-only 배분 로직이므로 향후 통과 후보가 다시 생기면 paper pilot 전 `core.target_weight_rotation` plan builder에 같은 배분 산식을 이식하고 params_hash 일치까지 검증해야 합니다. 다음 연구는 목표비중 배분보다 포지션별 손실 감산 또는 더 보수적인 drawdown guard를 결합해 MDD를 -20% 안쪽으로 안정화하는 방향입니다.

2026-05-13 research empty-universe guard: canonical universe 목록 조회 실패, 데이터 수집 실패, strict 유동성 필터 때문에 research universe가 0개가 되면 후보별 0% 성과를 만들지 않고 benchmark/candidate 평가를 건너뛰도록 보강했습니다. artifact와 Markdown에는 `INSUFFICIENT_BENCHMARK_DATA`, `empty_universe_reason`, `selection_error`, `skipped_due_to_data`가 남아 데이터 문제를 전략 성과로 오해하지 않게 합니다.

| 전략 | 상태 | Ret% | PF | WF P% | WF Sh+% | Paper Status |
|------|------|------|-----|-------|---------|--------------|
| relative_strength_rotation | **provisional_paper_candidate** | +18.09 | 1.62 | 100 | 83.3 | — |
| target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1 | **provisional_paper_candidate** (default capped pilot) | +198.15 | 5.58 | 100 | 100 | 전용 paper/pilot adapter 기본 후보, 60영업일 verified pilot evidence 필요 |
| target_weight_rotation_top5_60_120_floor0_hold3_risk60_35 | **provisional_paper_candidate** (previous default) | +171.20 | 4.24 | 100 | 83.3 | 이전 기본 후보, 관찰 유지 |
| scoring | **paper_only** | +11.22 | 1.07 | 83.3 | 50.0 | risk-adjusted alpha 미달 |
| breakout_volume | disabled (research_only) | -13.31 | 0.79 | 0 | 0 | — |
| mean_reversion | disabled (research_only) | -8.36 | 0.85 | 33.3 | 0 | — |
| trend_following | disabled (research_only) | -6.94 | 0.67 | 16.7 | 0 | — |
| ensemble | disabled (research_only) | — | — | 0 | 0 | — |

## 주의

실전 투입 전에는 백테스트, 검증, 모의투자를 충분히 거친 뒤 사용하는 것을 권장합니다.  
현재 scoring은 관찰용 paper_only로 강등되었습니다. 신규 우선 실험은 risk-adjusted 기준을 통과한 후보만 대상으로 하며, Paper Evidence 체계로 승격/강등 근거를 자동 수집합니다.
Paper 운영 도구: `tools/run_paper_evidence_pipeline.py` (backfill/finalize/package), `tools/paper_preflight.py`, `tools/paper_launch_readiness.py`, `tools/paper_pilot_control.py`, `tools/research_candidate_sweep.py`, `tools/target_weight_rotation_pilot.py`. target-weight 후보는 capped pilot 승인 전 `tools/target_weight_rotation_pilot.py --record-shadow-evidence`, `--shadow-days 3`, 또는 `--shadow-start-date YYYY-MM-DD --shadow-end-date YYYY-MM-DD`로 dry-run plan artifact, shadow readiness evidence, launch readiness artifact, plan 기반 cap 추천과 enable 명령이 포함된 pilot runbook을 먼저 누적합니다. 이후 `--readiness-audit`로 주문 제출이나 shadow/pilot evidence 기록 없이 clean shadow, launch readiness, active pilot caps, 중복 세션, 실행일/장 시간, 실행 전 포지션 드리프트, 종목/벤치마크 가격 최신성, 추천 cap 충족 여부, 최근 평균 거래대금 대비 주문 비율, 비용 반영 후 현금/투자비중 한도를 JSON artifact와 Markdown 운영 리포트로 점검합니다. 유동성 preflight는 기본적으로 주문별 20일 평균 거래대금의 5% 초과를 차단하며 `--max-order-adv-pct`로 조정할 수 있습니다. pre-trade risk 검증은 주문별 수수료/세금/동적 슬리피지를 반영한 예상 체결가로 현금 부족, 최소 현금비중, 총투자비중, 종목별 비중, 최대 보유 종목 수 위반을 실행 전에 차단합니다. Markdown 리포트에는 shadow 수집, audit 재실행, 추천 cap 승인, capped paper 실행 명령이 함께 기록됩니다. `--shadow-days N`은 휴장/데이터 공백으로 같은 거래일에 매핑되는 경우 과거 평일을 추가 스캔해 N개 고유 resolved trade_day 충족을 목표로 하며, 목표 미달이나 날짜별 실패가 있으면 CLI가 non-zero로 종료해 자동화가 불완전한 증거를 성공으로 처리하지 못하게 합니다. 실행형 pilot evidence는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, `run_pilot` 점검과 `execute_plan` 주문 제출 직전 재확인에서 실제 paper position이 계획 입력 장부 `position_quantities_before`와 모두 일치하며, `plan.trade_day`가 KST 실행일과 일치하고 KRX 정규장 주문 가능 시간인 경우에만 진행됩니다. 이후 유동성 preflight와 pre-trade risk 검증을 통과하고, 모든 계획 주문이 성공하며, 성공 주문 결과와 당일 `TradeHistory` fill 집계의 종목/방향/수량이 계획과 일치하고, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 `pilot_paper`로 수집됩니다. 같은 날짜 evidence가 이미 있으면 최신 canonical record도 `pilot_paper`/authorized/target-weight complete/execution-market-session-allowed/liquidity-complete/pre-trade-risk-complete/fill-complete 조건을 통과해야 재사용합니다. 중복 실행은 기본 차단하며 운영자가 명시적으로 재시도해야 할 때만 `--allow-rerun`을 사용합니다. 부분 실행/중단/중복 실행/주문 결과 불일치/체결 기록 불일치/기존 evidence 검증 실패/실행일 불일치/장 시간 외 실행/실행 전 포지션 드리프트/유동성 preflight 실패/pre-trade risk 실패/실행 후 포지션 불일치는 execution-backed 승격 증거에서 제외됩니다.

Target-weight 60영업일 pilot manifest: `--readiness-audit`는 운영 리포트와 함께 `target_weight_paper_experiment_manifest_*.json`을 생성해 후보 snapshot, 추천 cap, 차단 사유, 실행 명령, 데이터 품질 진단, 승격 증거 조건을 고정합니다. `--daily-ops-summary`는 readiness, risk check, 데이터 품질, 실행일 일치 여부, 장 시간 주문 가능 여부, verified pilot day 진행률, 다음 실행 명령을 `target_weight_daily_ops_summary_*.json/.md`로 묶어 매일 운영 판단을 한 번에 확인하게 합니다. `READY_TO_EXECUTE`와 `WAITING_FOR_MARKET_SESSION`은 실행일/장 시간 점검과 pilot authorization snapshot이 실제 `checked=True`로 통과한 경우에만 표시하며, 미점검 항목은 `NOT CHECKED`로 남겨 cap 승인 준비와 실행 준비를 분리합니다. 공식 `reports/paper_experiment_manifest.json`도 기존 scoring 60영업일 실험과 target-weight capped paper pilot을 분리해 기록합니다.

Generic paper entry도 동일한 실행 경계 원칙을 따른다. `main.py --mode paper`, scheduler auto-entry, fixed-quantity paper BUY 모두 preflight status와 runtime state를 주문 생성 전에 확인하고, 확인 실패는 fail-closed로 차단한다. blocked runtime에서 pilot authorization이 활성화되어도 `check_pilot_entry()`를 다시 통과해야 하며, SELL/exit 경로는 포지션 정리를 위해 계속 허용한다. 운영 손실 한도도 같은 원칙을 따른다. `risk_params.yaml:drawdown.max_portfolio_mdd` 또는 `max_daily_loss`에 닿으면 신규 BUY만 fail-closed로 차단하고, 손절·트레일링·수동 청산 SELL은 계속 열어 둔다. pilot evidence freshness는 달력일이 아니라 주말과 한국장 휴장일을 제외한 영업일 기준으로 계산합니다.

Target-weight pilot 승인/재시도 보강: `tools/paper_pilot_control.py --enable`은 `reports/promotion/run_metadata.json`의 canonical `strategy_specs[].base_strategy=target_weight_rotation`을 우선 기준으로 target-weight 후보를 식별하고, 기존 `target_weight_*` 접두어는 호환 fallback으로만 사용합니다. pilot auth를 쓰기 전 target-weight readiness audit을 다시 실행해 운영자가 요청한 cap이 현재 plan, launch readiness, 유동성 preflight, 비용 반영 pre-trade risk를 만족하는지 검증합니다. 승인 auth에는 당시 plan의 `trade_day`, `as_of_date`, `params_hash`, targets, 시작/목표 수량 snapshot을 함께 저장해 cap 승인과 실행 계획을 묶습니다. 유동성 diagnostics가 없으면 fail-closed로 차단하고, 이미 주문이 완료된 same-candidate/trade-day 세션은 `--allow-rerun`을 줘도 재실행하지 않습니다. `--allow-rerun`은 부분 실행이나 중단된 세션 복구용으로만 사용합니다.

Target-weight 실행 차단 기록: `--execute`가 pilot cap validation에서 막히면 주문·체결·증거 수집 없이 session JSON artifact에 차단 사유를 남깁니다. runtime pilot session은 쓰지 않아 cap을 고친 뒤 같은 거래일 계획을 다시 점검할 수 있습니다. 또한 `plan.trade_day`와 KST 기준 실제 실행일이 다르거나, 현재 시간이 KRX 정규장 주문 가능 시간이 아니거나, 활성 pilot auth의 승인 snapshot이 현재 plan의 `params_hash`/trade day/targets/수량 장부와 다르면 주문, idempotency, 포지션 조회, 체결 대조, session 저장, 승격 증거 수집 전에 fail-closed로 차단합니다. target-weight 전용 실행 어댑터는 후보명 접두어와 무관하게 승인 snapshot을 요구합니다. no-order `--readiness-audit`와 `--daily-ops-summary`도 실행일 check, 장 시간 check, 승인 snapshot check를 blocker로 표시해 오래되었거나 장 외 실행 명령을 READY 상태로 노출하지 않습니다. pilot entry가 막혀 아직 승인 snapshot이나 장 시간 검사가 실행되지 않은 경우 Markdown과 JSON risk snapshot은 `NOT CHECKED`로 표시해 미검사를 통과로 오해하지 않게 합니다.

Target-weight 가격 최신성 guard: 목표비중 plan 생성 시 종목별 마지막 실제 종가 날짜(`price_last_dates`)와 벤치마크 최신 날짜(`benchmark_last_date`)를 diagnostics에 남깁니다. 운영 주문 계획에서는 ffill로 보정된 낡은 종목 가격이 `trade_day` 최신 가격처럼 쓰이면 `target_weight_stale_price_data`로 차단하고, `benchmark_excess` 점수나 benchmark risk overlay에 필요한 벤치마크 가격이 score day보다 오래되면 `target_weight_benchmark_price_stale`로 plan 생성 자체를 중단합니다. `--readiness-audit`와 `--daily-ops-summary`는 같은 diagnostics를 `data_quality_check`/Data Quality 섹션으로 다시 노출해 가격 최신성 진단 누락, stale 종목, 벤치마크 지연을 READY 상태로 숨기지 않습니다. research target-weight backtest는 원본 close/open panel을 보존하되, score day 실제 종가가 없는 종목은 후보에서 제외하고 `stale_score_symbols_*`로 기록합니다. 보유 종목의 일별 stale 평가는 `held_stale_valuation_*`로 남기며, 보유 종목의 리밸런싱일 시가가 없으면 거래 없이 `missing_held_open_*`/`skipped_rebalance_missing_held_open_count`로 기록합니다. 벤치마크 stale과 신규 top-N 매수 후보의 시가 누락은 계속 fail-closed로 중단해 stale 가격이나 당일 종가 체결 착시가 pilot 흐름에 섞이지 않게 합니다.

Target-weight 승격 증거 보강: target-weight 계열 전략은 일반 `execution_backed=True` paper record만으로 promotion evidence day를 채우지 않습니다. `pilot_paper`/authorized record가 target-weight plan과 execution proof를 포함하고, record date와 plan trade day가 일치하며, `execution_trade_day_allowed=True`, `execution_market_session_allowed=True`, `pilot_authorization_snapshot_allowed=True`, liquidity/pre-trade risk/order result/fill/position reconciliation complete 및 plan/execution params hash 일치를 만족한 날만 승격 카운트에 들어갑니다. 60영업일 전체 verified pilot evidence는 하나의 params hash로 고정되어야 하며, promotion package 생성과 promotion/live gate는 canonical `strategy_specs`의 `base_strategy=target_weight_rotation`을 우선 기준으로 target-weight 후보를 식별합니다. paper evidence package도 canonical params hash와 pilot proof가 맞지 않으면 `BLOCKED`로 남기며, live gate도 canonical metadata의 params hash와 paper evidence params hash가 다르면 target-weight live 전환을 차단합니다.

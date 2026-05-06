# 🏗️ QUANT TRADER - 자동 주식 매매 시스템 설계서

> **문서 버전**: v5.2
> **작성일**: 2026-03-11
> **최종 수정**: 2026-04-30
> **목적**: 데이터 기반 알고리즘 트레이딩 시스템의 전체 아키텍처, **실제 파일/구조/알고리즘**, 구현 가이드 및 **시스템 상태 진단·개선 로드맵**

---

## 목차

1. [시스템 개요](#1-시스템-개요) — 1.3 **현재 시스템 상태 진단** (필독)
2. [기술 스택](#2-기술-스택)
3. [핵심 기술 지표](#3-핵심-기술-지표)
4. [매매 전략 로직](#4-매매-전략-로직) — 4.5 **전략별 수익 가능성 진단**, 4.6 **손실 시나리오**, 4.7 **구조적 한계**
5. [리스크 관리](#5-리스크-관리) — 5.13 히스터리시스, 5.14 최소 보유, **5.15~5.17 갭·국면 적응·장중 동적 손절/재스캔**(v3.0)
6. [시스템 아키텍처 및 프로젝트 구조](#6-시스템-아키텍처-및-프로젝트-구조)
7. [실행 모드 및 CLI](#7-실행-모드-및-cli)
8. [백테스팅 & 검증](#8-백테스팅--검증) — 8.5 **멀티종목 포트폴리오 백테스트**(v3.0)
9. [예외 처리 및 안정성](#9-예외-처리-및-안정성) — 9.1 **운영 안정성 개선 필요 사항**
10. [개발 로드맵 & 우선순위별 액션 아이템](#10-개발-로드맵--우선순위별-액션-아이템)
11. [주의사항](#11-주의사항)
12. [부록: 용어 정리](#부록-용어-정리)

---

## 1. 시스템 개요

자동 주식 매매 시스템(알고리즘 트레이딩)은 **사람의 감정 없이** 데이터와 수학적 로직으로 매매 결정을 내리는 프로그램입니다.

### 1.1 핵심 처리 흐름

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ 01       │   │ 02       │   │ 03       │   │ 04       │   │ 05       │   │ 06       │
│ 데이터   │──▸│ 지표     │──▸│ 신호     │──▸│ 리스크   │──▸│ 주문     │──▸│ 모니터링 │
│ 수집     │   │ 계산     │   │ 생성     │   │ 관리     │   │ 실행     │   │          │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
  실시간 주가     기술적 지표로    매수/매도       손절/익절      증권사 API로    실시간 성과
  거래량, 뉴스    시장 상태 분석   신호 판단       라인 설정      자동 주문       추적 및 로깅
```

### 1.2 시스템 목표

| 항목 | 목표 |
|------|------|
| **자동화** | 24시간 무인 매매 가능 (장 시간 내 자동 실행) |
| **감정 제거** | 공포/탐욕 없는 규칙 기반 매매 |
| **리스크 제한** | 최대 낙폭(MDD) 15% 이내 제어 |
| **수익 목표** | 검증된 전략으로 벤치마크(코스피 지수) 대비 초과 수익 달성. **가중치 최적화·워크포워드 검증·paper 1개월 운영을 모두 통과한 후에만 기대할 수 있는 목표**이며, 검증 없이 "연 20%" 같은 수치를 설정하는 것은 위험 |
| **대응 속도** | 실시간 시세 반영 (1초 이내 분석·주문) |

### 1.3 현재 시스템 상태 진단 — 반드시 읽으세요 (v5.2 업데이트)

> **핵심 판단: live 자동매매는 코드 레벨 hard gate로 차단. `--force-live` 제거됨. 주문 상태기계 도입.**

인프라(리스크 관리, 장애 복구, 로깅, 알림 이중화, 블랙스완 대응, OrderRecord 상태기계, Paper Evidence 수집)는 프로덕션 수준입니다. **전략 신호는 debiased 평가에서 same-universe B&H 대비 음수 excess이므로** live 전환 불가.

**전략 상태 레지스트리** (v5.2 — `core/promotion_engine.py`에서 metrics 기반 자동 판정):

| 전략 | 상태 | 허용 모드 | Ret% | PF | WF P%/Sh+% | 사유 |
|------|------|-----------|------|-----|-----------|------|
| **relative_strength_rotation** | `provisional_paper_candidate` | backtest, paper | +18.09 | 1.62 | 100/83.3 | debiased WF 통과. 경제적 alpha 미확인 |
| **scoring** | `paper_only` | backtest, paper | +11.22 | 1.07 | 83.3/50.0 | Sharpe -0.02, PF 1.07로 risk-adjusted 후보 기준 미달 |
| **breakout_volume** | `disabled` | backtest only | -13.31 | 0.79 | 0/0 | PF<1. BV50/R50 Paper Sleeve A (운영 사실, merit와 무관) |
| **mean_reversion** | `disabled` | backtest only | -8.36 | 0.85 | 33.3/0 | 유니버스 의존적 |
| **trend_following** | `disabled` | backtest only | -6.94 | 0.67 | 16.7/0 | PF<1. 한국 시장 구조적 비유효 |
| **ensemble** | `disabled` | backtest only | — | — | 0/0 | 구성 전략 disabled + 독립성 부족 |

**승격 규칙 v3** (`core/promotion_engine.py`):
- `research_only` → `paper_only` → `provisional_paper_candidate` → `live_candidate`
- 각 단계 정량 기준: ret>0, PF≥1.0, WF P≥50%, provisional은 Sharpe≥0.45, PF≥1.2, WF P/Sh+≥60%, MDD>-20%, live는 Paper 60일 + eligible evidence + benchmark excess>0
- metrics 기반 자동 판정. 수동 override 불가. CI 테스트로 registry-engine 일치 강제

**Live 진입 Hard Gate** (우회 불가 — `--force-live` 제거됨):
1. 현재 commit/config와 일치하는 `reports/promotion/` canonical bundle
2. `promotion_result.json`에서 해당 전략이 `live_candidate`
3. 전략별 benchmark excess return/Sharpe 모두 양수
4. `promotion_evidence_{strategy}.json` recommendation `ELIGIBLE`
5. execution-backed Paper 60영업일, benchmark_final_ratio 80% 이상, frozen day 0
6. 데이터 소스 health check

**주문 상태기계** (v5.0, `core/order_state.py`):
- 9개 상태: NEW → SUBMITTED → ACKED → FILLED / PARTIAL_FILLED / REJECTED / CANCELLED / EXPIRED → RECONCILED
- **FILLED assert 통과 후에만** position/trade DB 반영 (phantom position 방지)
- Paper: simulated broker event로 동일 전이. Live: broker callback + reconcile

**Paper 운영 현황** (v5.2):
- scoring 60영업일 Paper 실험 freeze pack: 2026-03-27 ~ 2026-06-19, `reports/experiment_freeze_pack.md`와 `reports/paper_experiment_manifest.json` 기준
- YAML hash와 resolved hash를 분리해 파일 변경과 환경변수 변경(`QUANT_AUTO_ENTRY`)을 별도로 감지
- Paper Evidence 수집 체계 (`core/paper_evidence.py`): 일별 22개 지표 + 6개 anomaly rule + 9개 approval gate
- Paper Runtime State Machine (`core/paper_runtime.py`): 5개 상태(normal/degraded/frozen/blocked_insufficient_evidence/research_disabled), schema quarantine, allowed_actions 제어
- Paper Pilot Authorization (`core/paper_pilot.py`): launch readiness 판정 + pilot auth + 리스크 캡. scoring: clean_final_days=3 달성, infra_ready=true (2026-04-09)
- Paper Preflight (`core/paper_preflight.py`): 세션 전 운영 준비 상태 점검
- Strategy Universe (`core/strategy_universe.py`): paper 대상 전략 canonical 목록
- Zero-return semantics: blocked/cash-only day에서 daily_return=0.0 추론 (deadlock 해소)
- Paper 운영 도구: `tools/run_paper_evidence_pipeline.py` (backfill/finalize/package), `tools/paper_preflight.py`, `tools/paper_launch_readiness.py`, `tools/paper_pilot_control.py`, `tools/target_weight_rotation_pilot.py`
- Research candidate sweep: `tools/research_candidate_sweep.py`가 promotion/live artifact와 분리된 rotation/momentum/breakout/pullback/benchmark-relative/risk-budget/cash-switch/benchmark-aware rotation/target-weight top-N rotation 후보 랭킹과 decision action을 생성하고 raw benchmark excess 음수 후보를 상위 alpha 후보에서 배제. defensive/cash-heavy 후보 해석을 위해 exposure-matched B&H 진단값도 기록. target-weight 후보는 `min_score_floor_pct` score-floor와 `hold_rank_buffer` rank-hysteresis 변형을 지원
- Latest research decision (2026-04-29): 5종목 all-family quick sweep에서 후보 14개 모두 benchmark excess return/Sharpe 미달. decision=`NO_ALPHA_CANDIDATE`; canonical promotion은 진행하지 않고 유니버스 확장 또는 새 후보군 설계를 우선
- Latest research decision (2026-04-30): canonical liquidity top-20 all-family quick sweep에서도 `NO_ALPHA_CANDIDATE`. best=`momentum_factor_120d`는 +118.56%였지만 benchmark excess=-30.83%p, MDD=-40.08%; promotion 미진행
- Follow-up research implementation (2026-04-30): 외부 재무 데이터 의존이 없는 `trend_pullback` 기반 `pullback` 후보군 4개를 추가해 다음 benchmark-aware sweep 대상으로 지정
- Follow-up research implementation (2026-04-30): `momentum_factor`에 KS11 대비 초과 모멘텀/변동성 게이트 옵션을 추가하고 `benchmark_relative` 후보군 3개를 추가
- Follow-up smoke result (2026-04-30): 5종목 `benchmark_relative`/`pullback` quick sweep 모두 `NO_ALPHA_CANDIDATE`; 신규 후보도 promotion 미진행
- Follow-up research implementation (2026-04-30): `CandidateSpec.diversification`을 추가하고 `risk_budget` 후보군 5개를 추가해 집중형/균형형/방어형 exposure 구조를 비교 가능하게 함
- Follow-up smoke result (2026-04-30): `risk_budget` 5종목 quick sweep도 `NO_ALPHA_CANDIDATE`. 방어형 rotation은 MDD=-6.41%로 개선됐지만 excess=-162.72%p라 alpha 없음
- Follow-up research implementation (2026-04-30): `relative_strength_rotation.market_filter_exit`와 `cash_switch` 후보군 3개를 추가해 KS11 이동평균 하회 시 현금화 구조를 검증 가능하게 함
- Follow-up smoke result (2026-04-30): `cash_switch` 5종목 quick sweep도 `NO_ALPHA_CANDIDATE`. best=`cash_switch_rotation_slow_defensive` return=+1.87%, excess=-171.76%p, MDD=-11.78%; alpha 없음
- Follow-up diagnostics (2026-04-30): research sweep에 exposure-matched benchmark 진단 추가. cash-switch 평균 노출은 8.4~10.0%, exposure-matched excess=-7.87%p~-0.36%p로 낮은 노출을 보정해도 신호 edge가 아직 없음
- Follow-up research implementation (2026-04-30): `relative_strength_rotation`에 KS11 대비 복합 모멘텀 차감 랭킹(`score_mode=benchmark_excess`), dense monthly entry, score-floor rebalance exit를 추가하고 `benchmark_aware_rotation` 후보군 4개로 노출 유지형 alpha를 검증 가능하게 함
- Follow-up smoke result (2026-04-30): `benchmark_aware_rotation` 5종목 quick sweep도 `NO_ALPHA_CANDIDATE`. best=`benchmark_aware_rotation_60_120_balanced` return=+21.65%, Sharpe=0.50, avg exposure=24.1%였지만 raw excess=-151.98%p; fast 40/100은 exposure-matched excess=+2.04%p라 다음 top-N 목표비중 리밸런싱 연구 후보
- Follow-up research implementation (2026-04-30): `target_weight_rotation` research-only evaluator를 추가. 월간 직전 거래일 점수 기준 top-N을 목표비중으로 보유/교체하고, delta 리밸런싱 거래비용과 일별 cash/value/n_positions 노출을 기록
- Follow-up smoke result (2026-04-30): `target_weight_rotation` 5종목 quick sweep도 `NO_ALPHA_CANDIDATE`. best=`target_weight_rotation_top3_40_100_excess` return=+128.44%, Sharpe=1.13, avg exposure=85.3%였지만 raw excess=-45.19%p라 promotion 미진행
- Follow-up full result (2026-04-30): canonical liquidity top-20 `target_weight_rotation` full sweep은 alpha 후보를 확인. best 기존 후보=`target_weight_rotation_top3_40_100_excess`, return=+212.21%, raw excess=+62.82%p, exposure-matched excess=+83.66%p. 다만 `promotion_status=paper_only`와 turnover/year=1412.1%로 decision=`KEEP_RESEARCH_ONLY`
- Follow-up research implementation/result (2026-04-30): target-weight score-floor 후보 3개 추가. best=`target_weight_rotation_top5_60_120_floor0`, return=+210.21%, Sharpe=1.41, WF positive=100%, raw excess=+60.82%p였지만 turnover/year=1081.5%라 승격 금지. 다음은 turnover-aware rebalance/tolerance/correlation 필터가 우선
- Follow-up research implementation/result (2026-04-30): target-weight rank-hysteresis 후보 추가. best=`target_weight_rotation_top5_60_120_floor0_hold3`, return=+278.57%, raw excess=+129.18%p, exposure-matched excess=+150.88%p, Sharpe=1.65, WF positive/Sh+ 100%, turnover/year=807.8%. turnover 병목은 해소했지만 MDD=-28.25%라 다음 병목은 drawdown-aware exposure/market-risk overlay
- Follow-up research implementation/result (2026-04-30): target-weight benchmark-risk overlay 후보 추가. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`, return=+210.24%, raw excess=+60.85%p, exposure-matched excess=+130.96%p, Sharpe=1.60, PF=5.73, MDD=-19.24%, turnover/year=858.0%, WF positive/Sh+ 100%, risk-off rebalance=38.9%로 research sweep 기준 `provisional_paper_candidate` 도달
- Follow-up canonical bridge (2026-04-30): `tools/evaluate_and_promote.py --canonical`이 위 target-weight 후보를 동일 candidate id/params hash로 `reports/promotion/*` canonical bundle에 포함. `promotion_result.json`과 `--check-only`에서 `provisional_paper_candidate` 재현 확인.
- Follow-up paper/pilot adapter (2026-04-30): `core/target_weight_rotation.py`가 직전 거래일 score 기반 portfolio-level 목표비중 plan과 pilot cap 검증을 담당하고, `tools/target_weight_rotation_pilot.py`가 dry-run/제한 paper 실행 artifact를 생성한다. `OrderExecutor.execute_buy_quantity()`는 paper-only exact quantity 매수를 지원하며 live 모드는 계속 거부한다.
- Follow-up liquidity preflight (2026-05-06): target-weight plan이 주문 종목별 최근 20일 평균 거래대금 진단을 포함하고, pilot adapter가 주문 notional이 평균 거래대금의 기본 5%를 넘으면 readiness/execute를 fail-closed 차단한다. 기준은 `--max-order-adv-pct`로 조정한다.
- Follow-up pre-trade risk validation (2026-05-06): target-weight pilot adapter가 주문 제출 전에 수수료/세금/동적 슬리피지 예상 체결가를 반영해 projected cash, cash ratio, investment ratio, position ratio, max positions를 검증한다. 위반 시 `target_weight_pre_trade_risk_failed`로 readiness/execute/evidence 재사용을 fail-closed 차단한다.
- 운영 체크리스트: `reports/daily_ops_checklist.md`, `reports/weekly_ops_checklist.md`, `reports/experiment_stop_conditions.md`
- 60일 종료 시 `generate_promotion_package()` 자동 승격 패키지 생성

**지속적 손실이 발생할 수 있는 구체적 시나리오** (§4.6 참고):
- **시나리오 A**: 과매매로 인한 수수료 손실 (월 10회 왕복 시 수수료만 2.3%)
- **시나리오 B**: 생존자 편향으로 과대평가된 백테스트 (실전 수익률이 수십 %p 하락)
- **시나리오 C**: 파라미터 과적합 (같은 시대의 OOS도 간접 과적합 가능)
- **시나리오 D**: 슬리피지 과소 추정 (저유동 종목 실전 0.3~0.5% 이상)
- **시나리오 E**: 앙상블 허구적 다각화 (실질 1개 신호를 3번 확인하는 것에 불과)

---

## 2. 기술 스택

### 2.1 언어 & 런타임

| 기술 | 선정 사유 |
|------|----------|
| **Python 3.11~3.12** | 금융 라이브러리 생태계 풍부. pandas, numpy, pandas-ta 등 핵심 패키지 완비 (`pyproject.toml`: `>=3.11,<3.13`) |
| **asyncio** | 비동기 처리로 실시간 데이터 스트리밍과 주문 처리를 동시 수행 |

### 2.2 데이터 수집

| 기술 | 선정 사유 |
|------|----------|
| **KIS Developers API** | 한국투자증권 공식 API — 국내주식 실시간 시세 및 주문 실행 |
| **yfinance** | 미국/한국 주식 무료 데이터 (백테스팅·일봉 보조, auto_adjust=True 로 수정주가) |
| **FinanceDataReader** | 한국 주식 무료 데이터 (KRX 전 종목, watchlist 자동 선정). **수정주가 기본 제공** — 백테스트·실전 동일 소스 권장 |
| **websocket-client** | KIS 실시간 호가/체결가 스트리밍 수신 |

**⚠️ 데이터 소스·수정주가 일관성 (§2.2)**

- **문제**: 한국 일봉은 **FinanceDataReader → yfinance → KIS** 순으로 fallback합니다. 세 소스의 **수정주가(배당·액면분할 반영)** 처리 방식이 다릅니다. FDR·yfinance는 수정주가를 기본/옵션으로 제공하지만, **KIS API는 비수정(원시) 데이터**를 반환하는 경우가 많습니다. 백테스트에는 수정주가를 썼는데 실전 신호 계산에 비수정 데이터를 쓰면 **지표값이 완전히 달라집니다**.

| 소스 | 수정주가 | 비고 |
|------|----------|------|
| **FinanceDataReader** | ✅ 기본 제공 | 한국 주식 **우선 권장** |
| **yfinance** | ✅ auto_adjust=True | 한국 종목 `.KS` 지원, 폴백용 |
| **KIS API** | ❌ 비수정 가능 | 주문 실행 전용 권장, 일봉 폴백은 위험 |

- **대응**:
  1. **소스 추적**: `DataCollector`가 매 수집 시 사용 소스와 수정주가 여부를 기록합니다 (`_last_source`, `_last_adjusted`, `_source_history`). 수집 로그에 `소스=FinanceDataReader, 수정주가=Yes` 형태로 명시합니다.
  2. **KIS 폴백 차단 옵션**: `settings.yaml`의 `data_source.allow_kis_fallback: false`로 설정하면 FDR/yfinance 모두 실패 시 **KIS 폴백을 차단**합니다. 수정주가 불일치를 원천 방지합니다.
  3. **소스 불일치 자동 감지**: `Scheduler` 장전 분석 후 `check_source_consistency()`로 FinanceDataReader 이외 소스를 사용한 종목을 감지하고, **경고 로그 + 디스코드 critical 알림**을 발송합니다.
  4. **우선 소스 지정**: `data_source.preferred: "fdr"` 로 설정하면 FinanceDataReader만 사용하고 다른 소스로 폴백하지 않습니다.
- **설정**: `config/settings.yaml` → `data_source` (preferred, allow_kis_fallback, warn_on_source_mismatch).
- **권장**: FDR 설치·우선 사용. KIS는 **주문 실행 전용**으로 두고, 일봉 수집에는 FDR 고정.

### 2.3 데이터 처리 & 분석

| 기술 | 선정 사유 |
|------|----------|
| **pandas** | 시계열 OHLCV 데이터프레임 처리 |
| **numpy** | 수치 계산 가속화 |
| **pandas-ta** | 기술적 지표 계산 (RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV) |

### 2.4 백테스팅 & 검증

| 기술 | 선정 사유 |
|------|----------|
| **자체 Backtester** | `backtest/backtester.py` — 수수료·세금·슬리피지·손절/익절/트레일링 스탑 반영, **strict-lookahead 기본**. 성과 지표: 샤프·**소르티노**·MDD·**MDD 회복 기간**·**VaR/CVaR(일 95%)**·**최대 연속 손실 거래 수** 등 |
| **PortfolioBacktester** | `backtest/portfolio_backtester.py` — **멀티종목** 동시 운용 시뮬레이션, 분산 제한·최대 포지션 수·투자비중 상한 반영, 종목별 성과 요약 (`--mode portfolio_backtest`) |
| **strategy_validator** | 최소 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중) 비교, in/out-of-sample 분리 검증, **손익비 자동 경고(추세 추종 ≥ 2.0) + 디스코드 알림** |
| **param_optimizer** | Grid Search / Bayesian(scikit-optimize) 파라미터 최적화 |

### 2.5 데이터베이스

| 기술 | 선정 사유 |
|------|----------|
| **SQLite** | 기본. WAL + busy_timeout(30s) + scoped_session + @with_retry(읽기·쓰기 전체) + Online Backup API. 실전 안정화 후 PostgreSQL 전환 권장 |
| **SQLAlchemy** | ORM — DB 전환(PostgreSQL 등) 시 마이그레이션 용이 |

**ORM 모델** (`database/models.py`):

| 모델 | 설명 |
|------|------|
| **StockPrice** | 종목별 OHLCV 시계열 저장 |
| **TradeHistory** | 매매 기록 (종목, 방향, 수량, 가격, 수수료, 전략, 사유) |
| **Position** | 현재 보유 포지션 (종목, 수량, 평균가, 손절/익절/트레일링) |
| **PortfolioSnapshot** | 일별 포트폴리오 스냅샷 (총자산, 수익률, MDD) |
| **DailyReport** | 일일 리포트 데이터 |
| **FailedOrder** | 주문 실패 Dead-letter 큐 (재처리 지원, status: pending/retried/cancelled) |

### 2.6 모니터링 & 알림

| 기술 | 선정 사유 |
|------|----------|
| **Discord Webhook** | 매수/매도·일일 리포트·블랙스완·동기화 불일치 알림 |
| **loguru** | 구조화된 로그 (파일 로테이션·콘솔 출력) |
| **웹 대시보드** | aiohttp 기반 실시간 포트폴리오·스냅샷 (기본 8080) |

---

## 3. 핵심 기술 지표

구현 위치: **`core/indicator_engine.py`** (pandas-ta 기반, 설정: `config/strategies.yaml` → `indicators`)

### 3.1 RSI (상대강도지수)

- **설명**: 가격 모멘텀. 0~100. 과매도/과매수 구간 판별.
- **공식**: `RSI = 100 - (100 / (1 + RS))`, RS = 평균 상승폭 / 평균 하락폭
- **설정**: `indicators.rsi.period` (기본 14), `oversold` 30, `overbought` 70
- **신호**: RSI < 30 → 과매도(매수 후보), RSI > 70 → 과매수(매도 후보)

### 3.2 MACD (이동평균 수렴·확산)

- **설명**: 추세 방향·강도. 골든크로스/데드크로스.
- **설정**: `fast_period` 12, `slow_period` 26, `signal_period` 9
- **신호**: MACD선이 Signal선 상향 돌파 → 매수, 하향 돌파 → 매도

### 3.3 볼린저 밴드

- **설명**: 변동성 밴드. 하단 터치 후 반등 → 매수, 상단 터치 후 하락 → 매도.
- **설정**: `period` 20, `std_dev` 2.0

### 3.4 이동평균 (MA)

- **설명**: 추세 방향. 단기/장기 골든크로스·데드크로스.
- **설정**: `short_period` 5, `mid_period` 20, `long_period` 60, `trend_period` 200

### 3.5 거래량 & OBV

- **설정**: `volume.avg_period` 20, `surge_ratio` 1.5 (평균 대비 거래량 급증 기준)
- **OBV**: 상승일 +거래량, 하락일 -거래량 누적. `indicator_engine.add_obv`, `add_volume_ratio`

### 3.6 스토캐스틱

- **설정**: `k_period` 5, `d_period` 3, `smooth` 3, `oversold` 20, `overbought` 80

### 3.7 ADX (평균 방향 지수)

- **설명**: 추세 **강도** (방향 아님). ADX < 20 횡보, > 25 추세 강함.
- **설정**: `period` 14, `trend_threshold` 25

### 3.8 ATR (평균 실질 범위)

- **설명**: 변동성 크기. 손절/트레일링 스탑 배수 설정에 사용.
- **설정**: `period` 14. 리스크: `risk_params.yaml` → `stop_loss.atr_multiplier`, `trailing_stop.atr_multiplier`

---

## 4. 매매 전략 로직

### ⚠️ 시장 비효율성과 전략의 이론적 근거 (근본 원칙)

퀀트 전략이 **지속적으로 수익**을 내려면 **시장 비효율성(Market Inefficiency)** 을 이용해야 합니다. "왜 이 전략이 돈을 벌 수 있는가"에 대한 **이론적·실증적 근거**가 있어야 하며, 현재 설계에서 **각 전략이 이용하려는 비효율성이 무엇인지** 명시하는 것이 중요합니다.

- **학술적으로 검증된 팩터 예**: (1) **단기 과반응 후 되돌림** → 평균 회귀 전략, (2) **모멘텀 효과**(좋은 주식이 일정 기간 계속 좋음) → 추세 추종 전략, (3) 요일/월 효과, 실적 발표 전후 패턴 등. 이런 **명시된 비효율성**을 기반으로 전략을 설계하면 "왜 돈이 될 수 있는가"에 대한 근거가 생깁니다.
- **현재 한계**: "RSI가 30 이하면 반등할 것 같다", "볼린저 하단이면 매수" 같은 **직관 수준**에 머물러 있으면, **어떤 시장 비효율성을 이용하는지** 불명확합니다. 아래 각 전략에 **이용(가정)하는 비효율성**을 적어 두었으므로, 전략 선택·개선 시 참고하세요.

---

### 4.1 멀티 지표 스코어링 전략 (초급 ⭐)

- **구현**: `strategies/scoring_strategy.py`, `core/signal_generator.py`
- **설정**: `config/strategies.yaml` → `scoring` (buy_threshold, sell_threshold, weights)
- **이용(가정)하는 시장 비효율성**: **명시되지 않음**. 여러 기술지표(RSI, MACD, 볼린저, MA, 거래량)를 조합해 신호를 내는 구조이며, "RSI 30 이하면 반등할 것 같다"는 **직관**에 가깝고, **학술적으로 검증된 단일 팩터(비효율성)** 에 기반을 두지 않습니다. 노이즈 완화·다수 지표 합의를 노리는 **실용적 조합** 수준이므로, 실전 사용 시 **어떤 비효율성을 노리는지** 별도 가정을 두거나, 평균 회귀·모멘텀 등 명시된 팩터와 결합하는 것을 권장합니다.

**⚠️ 지표 간 다중공선성 (Multicollinearity)**

- **문제**: 현재 스코어링에 RSI, MACD, 볼린저, MA, 거래량이 들어가 있으며, 스토캐스틱(계산되지만 스코어링에는 미사용)을 추가하면 RSI와 동일한 오실레이터 성격으로 **높은 상관관계**를 가집니다. 이 지표들 대부분은 **가격과 이동평균의 변형**입니다. RSI·스토캐스틱은 둘 다 "과매수/과매도" 오실레이터로 정보가 중복되고, MACD와 MA 골든크로스도 **같은 정보(가격 추세)**를 다르게 표현할 뿐입니다. 결과적으로 **스코어의 대부분이 "가격이 최근 올랐냐 내렸냐" 한 가지 정보를 여러 번 세는 형태**가 되어, 실질적으로 1~2개 지표에 스코어가 지배당할 수 있습니다.
- **필수 조치**: `--mode check_correlation`을 **반드시** 실행하고 리포트를 확인하세요. 상관계수 |r| ≥ 0.7인 쌍은 **둘 중 하나 제거**(가중치 0) 또는 가중치 축소를 적용해야 합니다.
- **권장 구성**: 실질적으로 **독립적인 정보**는 다음 **3그룹**으로 나눌 수 있습니다. **그룹당 대표 지표 하나씩만** 남기는 것을 권장합니다.

| 그룹 | 대표 지표 (택 1) | 비고 |
|------|------------------|------|
| **가격 모멘텀** | MACD (권장) 또는 MA | 같은 추세 정보. RSI도 가격 변형. 둘 이상 쓰면 다중공선성 |
| **변동성** | 볼린저 (또는 ATR) | 밴드/범위 정보. 스코어링에는 현재 볼린저만 사용 |
| **거래량** | volume_surge (OBV/volume_ratio) | 가격 외 독립 정보 |

**다중공선성 완화 모드 (`collinearity_mode`)**

`strategies.yaml`의 `scoring.collinearity_mode` 설정으로 두 가지 모드를 제공합니다:

| 모드 | 동작 | 완화 수준 | 권장 대상 |
|------|------|-----------|-----------|
| `max_per_direction` | 가격 그룹(RSI/MACD/볼린저/MA) 점수를 방향별 최대 1개만 반영. 매수=양수 max, 매도=음수 min | 중간 | 기존 호환 필요 시 |
| `representative_only` **(권장)** | 3그룹에서 **대표 지표 1개**씩만 사용 (MACD + 볼린저 + 거래량). 나머지 점수를 0 강제 | 강력 | 신규 설정, 실전 투입 전 |

`representative_only` 모드에서의 스코어 구성:
```
total_score = score_macd + score_bollinger + score_volume
               (가격모멘텀)   (변동성)          (거래량)
```
RSI·MA 점수는 **계산은 되지만** 총점에 반영되지 않습니다 (check_correlation 등 분석 용도로 유지).

**⚠️ 런타임 경고**: `SignalGenerator` 초기화 시 가격 모멘텀 그룹에서 **2개 이상 지표가 활성 가중치**를 가지면 경고 로그가 자동 출력됩니다. `--mode optimize --auto-correlation`으로 자동 정리하거나 `collinearity_mode: representative_only`로 설정하세요.

**스코어링 가중치 (weights):**

| 조건 | 가중치 키 | 예시 점수 | 비고 |
|------|-----------|----------|------|
| RSI 과매도 | rsi_oversold | +2 | 예시값, 검증 없음 |
| RSI 과매수 | rsi_overbought | -2 | 예시값, 검증 없음 |
| MACD 골든크로스 (당일) | macd_golden_cross | +2 | 크로스 당일 풀 점수 |
| MACD > Signal 유지 중 | — | +1 | 상승 추세 지속 시 반점수 (buy_weight × 0.5) |
| MACD 데드크로스 (당일) | macd_dead_cross | -2 | 크로스 당일 풀 점수 |
| MACD < Signal 유지 중 | — | -1 | 하락 추세 지속 시 반점수 (sell_weight × 0.5) |
| MACD 히스토그램 방향 전환 | — | ±0.5 | 크로스 아닌 날에만 적용 |
| 볼린저 하단 이탈 후 반등 | bollinger_lower | +1 | 예시값, 검증 없음 |
| 볼린저 상단 이탈 후 하락 | bollinger_upper | -1 | 예시값, 검증 없음 |
| 거래량 급증 | volume_surge | +1 | 예시값, 검증 없음 |
| 5일선 > 20일선 (골든크로스) | ma_golden_cross | +1 | 예시값, 검증 없음 |
| 5일선 < 20일선 (데드크로스) | ma_dead_cross | -1 | 예시값, 검증 없음 |

**⚠️ 가중치 설정 유의사항**

- **근거**: 위 점수는 **직관·예시용**이며, 한국 주식 시장 데이터로 검증된 값이 **아닙니다**. RSI에 +2, 볼린저에 +1인 이유에 대한 통계적·실증적 근거는 없습니다. **이 가중치로 실제 거래를 발생시키면 신호가 노이즈에 가까울 수 있습니다**.
- **영향**: 가중치를 바꾸면 신호 빈도·방향이 달라지므로, "현재 값이 최적"이라는 보장이 없습니다. 아무리 리스크 관리·시장 국면 필터 등 인프라가 잘 되어 있어도 **신호 자체가 노이즈라면 결과는 무작위 또는 손실**입니다.
- **최적화 시 오버피팅**: `--mode optimize --include-weights`로 가중치를 과거 데이터로 탐색하면 **과적합** 가능성이 있습니다. 반드시 **OOS 샤프 ≥ 1.0 게이트**(자동 적용)를 통과해야 하며, walk-forward 추가 검증이 필수입니다.

**가중치 최적화 파이프라인 (필수 3단계)**

실전 투입 전 아래 순서를 반드시 따르세요:

```
┌─────────────────────────────────┐
│ STEP 1: 지표 독립성 검증         │
│ --mode check_correlation        │
│ → |r| ≥ 0.7 쌍 확인·제거 결정    │
└────────────┬────────────────────┘
             ▼
┌─────────────────────────────────┐
│ STEP 2: 가중치+임계값 최적화     │
│ --mode optimize --include-weights│
│ → 대칭 Grid Search              │
│ → OOS 샤프 ≥ 1.0 게이트 통과?   │
│   YES → YAML 스니펫 채택         │
│   NO  → 채택 불가 (과적합)       │
└────────────┬────────────────────┘
             ▼
┌─────────────────────────────────┐
│ STEP 3: 워크포워드 안정성 검증   │
│ --mode validate --walk-forward  │
│ → 여러 기간에서 안정적?          │
│   YES → 실전 투입 고려           │
│   NO  → 다시 STEP 1로           │
└─────────────────────────────────┘
```

**STEP 1** — 지표 독립성 검증:
```bash
python main.py --mode check_correlation --symbol 005930 --validation-years 5
```
`core/indicator_correlation.py`가 스코어 시리즈 상관계수 행렬을 계산하고, |r| ≥ 0.7인 쌍에 대해 **하나 제거 또는 가중치 축소**를 권고합니다. 리포트는 `reports/indicator_correlation_*.txt`에 저장됩니다. RSI와 스토캐스틱은 높은 확률로 중복됩니다. 리포트 하단에 **자동 비활성화 대상 가중치 키**와 **다음 단계 CLI 명령어**가 출력됩니다.

**STEP 2** — 가중치+임계값 최적화:
```bash
# 방법 A: 원스텝 (상관 분석 + 자동 비활성화 + 최적화를 한 번에)
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation --symbol 005930

# 방법 B: 수동 (STEP 1 결과를 보고 직접 지정)
python main.py --mode optimize --strategy scoring --include-weights --disable-weights w_rsi,w_ma --symbol 005930
```
`--auto-correlation` 사용 시 STEP 1의 상관 분석이 자동 실행되어 고상관 지표(가격 모멘텀 그룹에서 MACD를 대표로 남기고 RSI·MA 비활성화)를 자동으로 `disabled_weights`에 추가합니다. `backtest/param_optimizer.py`의 `grid_search_scoring_weights()`가 가중치(대칭: 매수=+w, 매도=-w) × 임계값 조합을 탐색합니다. Train 70%에서 최적화한 뒤 OOS 30%에서 **샤프 ≥ 1.0 게이트**를 자동 검증합니다. 게이트 통과 시 `strategies.yaml`에 붙여넣을 YAML 스니펫을 출력합니다.

**STEP 3** — 워크포워드 안정성 검증:
```bash
python main.py --mode validate --walk-forward --strategy scoring --symbol 005930 --validation-years 5
```
STEP 2에서 찾은 가중치를 `strategies.yaml`에 반영한 뒤 실행합니다. 대부분의 창(80% 이상)에서 통과해야 실전 투입을 고려할 수 있습니다.

**실행 기준**: 총점 ≥ `buy_threshold`(다중공선성 완화 후 권장 2~3) → 매수, 총점 ≤ `sell_threshold`(권장 -2~-3) → 매도.  
(임계값 근처에서 신호가 자주 바뀌면 **과매매** 위험 → 거래 빈도·수수료 §8.3 참고.)

**⚠️ 매수/매도 임계값 대칭**

- **권장**: `buy_threshold`와 `sell_threshold`는 **절댓값을 같게** 두는 것을 권장합니다 (현재 기본값: 2와 -2). 비대칭(예: 매수 5점, 매도 -4점)이 의도된 것이 아니라면, 대칭으로 설정해 두는 것이 안전합니다.
- **비대칭 시 문제**: 매도 쪽 임계값이 완화되면(예: -4만 있어도 매도) 매도가 **늦어져** 수익을 반납하기 쉽고, 반대로 매도 임계값이 엄격하면(예: -6 이상일 때만 매도) 매도가 **너무 일찍** 나와 보유 기간이 짧아질 수 있습니다. 의도 없는 비대칭은 진입·청산 타이밍이 한쪽으로 치우친 패턴을 만듭니다.
- **설정**: `strategies.yaml`의 `buy_threshold`, `sell_threshold`를 동일 절댓값으로 맞추고, `--mode optimize` 사용 시에도 대칭 쌍만 탐색하도록 하는 것을 권장합니다.

### 4.2 평균 회귀 전략 (중급 ⭐⭐)

- **구현**: `strategies/mean_reversion.py`, `core/fundamental_loader.py` (펀더멘털 필터)
- **설정**: `strategies.yaml` → `mean_reversion` (z_score_buy, z_score_sell, lookback_period, adx_filter, **exclude_52w_low_near**, **max_drawdown_from_52w_high**, **near_52w_low_pct**, window_52w, **restrict_to_kospi200**, **fundamental_filter**)
- **이용(가정)하는 시장 비효율성**: **단기 과반응 후 되돌림(Short-term overreaction then reversal)**. 가격이 단기적으로 평균에서 크게 이탈했다가 다시 평균으로 돌아오는 현상을 이용합니다. 학술적으로 **평균 회귀·되돌림(mean reversion)** 효과로 알려진 팩터에 해당하며, **한국 시장**에서는 펀더멘털 악화로 인한 하락이 많아 해당 비효율성이 제한적으로만 성립할 수 있습니다(아래 "한국 시장 한계" 참고).

**로직**: Z-Score = (현재가 - 평균) / 표준편차. Z < -2 매수, Z > 2 매도. ADX < adx_filter 일 때만 활성화(횡보장 강조).

**52주 고점/저점 이중 필터**: 실전 적용 시 장기 하락 구간 종목은 매수 제외하는 것을 권장합니다. `exclude_52w_low_near: true` 시 두 가지 조건을 검사합니다:
1. **52주 고점 대비 하락률**: `max_drawdown_from_52w_high`(기본 0.30) — 52주 고점에서 30% 이상 하락한 종목은 매수 제외. 이것이 주된 필터이며, "깊은 하락 = 실적 악화 가능성"을 간접적으로 포착합니다.
2. **52주 저점 근방**: `near_52w_low_pct`(기본 0.05) — 현재가가 52주 저점 대비 5% 이내이면 신저가 구간으로 판단하여 매수 제외.
`window_52w`(기본 252 거래일)로 52주 기간을 조정할 수 있습니다.

**코스피200 대형주 제한**: `restrict_to_kospi200: true` 시 **코스피200 구성 종목만** 평균 회귀 매수 가능합니다. 대형주는 실적 악화로 인한 영구 하락이 소형주보다 적어, 평균 회귀 가정이 상대적으로 잘 성립합니다. `pykrx`가 필요하며, 로드 실패 시 제한이 비활성화됩니다 (로그 경고).

**펀더멘털 필터**: Z-Score 매수 조건이 충족되어도, **매수 전** 해당 종목의 기본 재무 지표가 정상 범위인지 확인합니다. `mean_reversion.fundamental_filter.enabled: true` 시 **PER**(적자 제외·상한 설정 가능), **부채비율(%)** 상한을 검사하며, 범위를 벗어나면 매수 신호를 HOLD로 보류합니다. 데이터는 **pykrx(우선) → yfinance(폴백)** 순서로 조회합니다. pykrx는 한국 종목 PER 정확도가 높고, yfinance는 부채비율 등 추가 항목을 보충합니다. `per_min`·`per_max`·`debt_ratio_max`는 `strategies.yaml`에서 설정할 수 있으며, 백테스트 시 symbol이 전달되지 않으면 펀더멘털 필터를 수행하지 않습니다.

**⚠️ "평균"의 정의와 lookback_period**

- **핵심**: Z-Score에서 쓰는 **"평균"**은 **최근 lookback_period 일의 종가 이동평균**입니다. 표준편차도 같은 구간으로 계산됩니다. 즉 "어느 기간 기준으로 벗어났는가"를 정하는 것이 lookback_period 입니다.
- **영향**: 이 기간을 **20일**로 하느냐 **60일**로 하느냐에 따라 신호가 **완전히** 달라집니다. 20일은 단기 이탈, 60일은 중기 추세 이탈에 가깝습니다. 현재 설정은 **최적화·실증 없이 쓰는 고정값(기본 20일)** 이므로, 종목·시장에 맞게 조정하거나 `--mode optimize` 로 탐색하는 것을 권장합니다.
- **최적화**: `param_optimizer` 의 mean_reversion 검색 공간에 lookback_period가 포함되어 있습니다 (Grid: 15/20/25 등, Bayesian: 10~40). 다른 기간(예: 60)을 쓰려면 `strategies.yaml` 에서 직접 설정하거나, 검색 공간을 확장해 사용하세요.

**⚠️ 한국 주식 시장에서의 한계**

- **가정과 현실**: 평균 회귀는 "많이 떨어진 주가는 결국 평균으로 돌아온다"는 가정에 기반합니다. 그러나 **한국 시장**에서는 크게 하락한 종목 상당수가 **실적 악화, 분식회계, 대주주 지분 매도** 등 **펀더멘털 이유**로 하락하며, 이런 종목은 평균으로 회귀하지 않고 **추가 하락**하는 경우가 많습니다.
- **Z-Score만으로는 구분 불가**: Z-Score < -2 조건만으로는 **"기술적 과매도(일시적 반등 가능)"**와 **"펀더멘털 악화로 망해가는 기업"**을 구분할 수 없습니다.
- **ADX 필터의 불완전성**: ADX < adx_filter 로 "횡보장만 매수"하려 해도, **실적 악화 등으로 꾸준히 우하향하는 구간**에서도 ADX가 낮게 나올 수 있어, **하락 추세를 횡보로 오판**할 수 있습니다. 즉 필터만으로는 "진짜 횡보"와 "하락 추세의 일부 구간"을 완전히 나누기 어렵습니다.
- **권장 (실전 적용 시)**:
  1. **52주 고점/저점 이중 필터**: `exclude_52w_low_near: true` + `max_drawdown_from_52w_high: 0.30` + `near_52w_low_pct: 0.05`. 52주 고점 대비 30% 이상 하락 또는 저점 대비 5% 이내인 종목을 매수에서 제외합니다.
  2. **코스피200 제한**: `restrict_to_kospi200: true` — 대형주만 매수 허용. 소형주의 영구 하락 위험 회피.
  3. **펀더멘털 데이터**: `fundamental_loader.py`가 **pykrx(우선) → yfinance(폴백)** 순서로 자동 조회합니다. pykrx는 한국 종목 PER 정확도가 높고, yfinance는 부채비율 등 추가 항목 보충.
  4. **손절·포지션 사이징**: 위 필터만으로도 리스크가 남으므로 **손절·포지션 사이징을 엄격히** 적용하세요.

### 4.3 추세 추종 전략 (중급 ⭐⭐)

- **구현**: `strategies/trend_following.py`
- **설정**: `trend_following` (adx_threshold, trend_ma_period, atr_stop_multiplier, trailing_atr_multiplier)
- **이용(가정)하는 시장 비효율성**: **모멘텀 효과(Momentum)** — "좋은 주식이 일정 기간 계속 좋다"는 현상. 상대적으로 강한 추세가 지속되는 구간에서 추세를 따라가는 방식으로, 미국(나스닥) 등에서 **모멘텀 팩터**로 실증된 비효율성에 기반합니다. 한국 시장에서는 추세 지속성이 약해 해당 비효율성이 weaker할 수 있습니다(아래 "한국 시장 추세 지속성" 참고).

**로직**: ADX > adx_threshold, 가격 > trend_ma(200일), MACD 골든크로스(히스토그램 양수 전환) 시 매수. ATR 기반 손절·트레일링 스탑.

**⚠️ 늦은 진입 구조와 수익 구조**

- **진입이 늦는 이유**: 세 조건이 **동시에** 충족되는 시점은 (1) ADX > threshold → 이미 추세가 강해진 뒤, (2) 가격 > 200일선 → 이미 장기 상승 구간, (3) MACD 양수 전환 → 이미 단기 상승이 확인된 뒤입니다. 즉 **상당히 올라간 이후**에야 매수 신호가 나옵니다.
- **반복 패턴**: 뒤늦게 진입 → 조정 시 ATR 손절로 매도 → 다시 상승 시 또 늦게 진입. 그래서 **손실 거래가 잦고 승률이 40% 이하**로 낮을 수 있습니다. 이 자체가 나쁜 것은 아니지만, **수익 거래가 크게 나와야** 전략이 유효합니다.
- **손익비(Profit Factor) 검증 필수**: 이런 구조에서는 **손익비(Profit Factor) ≥ 2.0** 이어야 전략이 의미 있습니다. 백테스트·검증(`--mode backtest`, `--mode validate`) 결과에서 **손익비가 실제로 2.0 이상인지 반드시 확인**하세요. 달성되지 않으면 진입 조건 완화(예: 200일선 근접 허용) 또는 다른 전략 검토를 권장합니다.

**⚠️ 한국 시장의 추세 지속성**

- **미국 vs 한국**: 추세 추종 전략은 **미국 주식(특히 나스닥)** 시장에서 잘 동작한다는 실증·문헌이 많습니다. 반면 **코스피/코스닥**은 **박스권 등락이 길고**, 추세가 **빠르게 꺾이는** 특성이 있어, 추세 추종이 한국 시장에서도 동일하게 유효하다는 **실증 근거는 상대적으로 약합니다**.
- **권장**: 한국 시장에 이 전략을 적용할 때는 (1) **백테스트·검증으로 해당 종목/기간에서의 성과를 반드시 확인**하고, (2) 미국 시장용 파라미터(예: 200일선, ADX 25)를 그대로 쓰지 말고 **기간·임계값을 조정**하거나, (3) 앙상블에서 비중을 낮추는 것을 고려하세요.

### 4.3a 펀더멘털 팩터 전략 (중급 ⭐⭐)

- **구현**: `strategies/fundamental_factor.py`
- **설정**: `strategies.yaml` → `fundamental_factor` (PER 상대·ROE·부채비율·영업이익 성장·캐시 TTL 등)
- **CLI**: `--strategy fundamental_factor` (전략 레지스트리에 등록됨)
- **이용(가정)하는 시장 비효율성**: **가격(OHLC)과 독립인 재무 지표**로 저평가·건전성·성장을 가늠하려는 접근. pykrx(우선) → yfinance(폴백)로 PER·ROE 등을 조회하며, 백테스트 시 `df.attrs['symbol']`에 종목코드를 넣어야 종목별 펀더멘털 조회가 안정적으로 동작합니다.
- **한계**: 재무 데이터 공시 시차·최신성, 한국 종목 yfinance 커버리지, 캐시 정책에 따라 신호가 드물거나 HOLD에 머물 수 있습니다. 반드시 백테스트·검증으로 해당 유니버스에서의 유효성을 확인하세요.

### 4.4 전략 앙상블 (정보 소스 분리)

- **구현**: `core/strategy_ensemble.py`
- **설정**: `strategies.yaml` → `ensemble` (`components` 권장), `momentum_factor`, `volatility_condition`, `fundamental_factor`
- **사용**: `--strategy ensemble` 시 **설정된 구성 전략** 신호를 통합 (기본 예시: technical + momentum_factor + volatility_condition + **선택 시 fundamental_factor**).

앙상블은 **기술적 지표 / 모멘텀 팩터 / 변동성 조건 / (선택) 펀더멘털**로 정보 소스를 나누어, 다수결·가중합·보수적 모드가 독립적인 근거의 합의에 가깝게 동작하도록 구성할 수 있습니다. `ensemble.components`에서 각 구성의 `enabled`·`weight`를 조정합니다. `fundamental_factor`는 데이터 부재 시 집계에서 제외될 수 있습니다(`ensemble_skip`).

| 구성 전략 | 정보 소스 | 구현 | 설명 |
|-----------|-----------|------|------|
| **technical** | 기술적 지표 | ScoringStrategy | RSI, MACD, 볼린저, 거래량, 이동평균 스코어링 |
| **momentum_factor** | 가격 수익률 | MomentumFactorStrategy | N일 수익률만 사용. 모멘텀 효과(과거 수익률 지속) 기반 |
| **volatility_condition** | 실현변동성 | VolatilityConditionStrategy | N일 실현변동성(연율화)만 사용. 저변동성=매수, 고변동성=매도 |
| **fundamental_factor** (선택) | 재무 지표 | FundamentalFactorStrategy | PER(섹터 상대 등), ROE, 부채비율, 영업이익 성장 등. 가격과 독립 |

- **모드**: `majority_vote`(다수결), `weighted_sum`(전략별 가중 후 임계값), `conservative`(**집계에 참여한** 구성 전략이 모두 동일 신호일 때만 매매).
- **추가 개선 여지**: `fundamental_factor`로 재무 축은 일부 보강되었으나, 뉴스/센티먼트·매크로 등 **가격·재무와 다른 축**이 더해지면 앙상블 독립성이 더 높아질 수 있습니다.

**⚠️ 앙상블의 실질적 독립성 문제**

- **문제**: technical(스코어링)은 RSI·MACD·MA 등을 포함하고, momentum_factor는 N일 수익률을 사용합니다. **N일 수익률이 좋은 구간은 이동평균 골든크로스도 발생했을 가능성이 높아**, technical과 momentum_factor가 **같은 상황에서 동시에 BUY**를 내는 경향이 있습니다. 그러면 다수결 의미가 퇴색합니다.
- **대응 (3단계 자동 방어)**:
  1. **런타임 자동 검사**: `StrategyEnsemble.analyze()` 첫 호출 시 **활성 구성 전략** 신호 쌍의 Pearson 상관계수를 계산. **|r| ≥ `independence_threshold`** (기본 0.6)인 쌍이 있으면 경고 로그 출력.
  2. **자동 모드 다운그레이드**: `auto_downgrade: true`(기본) + 고상관 감지 시, `majority_vote`/`weighted_sum` → `conservative`로 **자동 전환**. 집계 참여 전략이 모두 동의해야만 BUY/SELL 실행. `auto_downgrade: false`로 비활성화 가능.
  3. **validate 모드 통합**: `--mode validate --strategy ensemble`로 검증 시, 검증 완료 후 **앙상블 독립성 리포트**가 자동 생성·출력. 고상관 감지 시 Discord 알림.
- **수동 검증**: `python main.py --mode check_ensemble_correlation --symbol 005930 --validation-years 5`
  `core/ensemble_correlation.py`가 앙상블 analyze 결과에서 `signal_technical`, `signal_momentum_factor`, `signal_volatility_condition` 을 수치화해 일별 상관계수 행렬을 계산합니다. 기준값은 `--ensemble-correlation-threshold` 로 변경 가능(기본 0.6).
- **BUY/SELL 동시 발생률**: Pearson 상관계수 외에 "두 전략이 같은 날 BUY한 비율"도 리포트에 포함. 상관계수는 직관적이지 않으므로 동시 발생률로 실전 위험을 체감할 수 있습니다.
- **구체적 대안 전략 권고**: 고상관 쌍이 감지되면 정보 소스가 겹치지 않는 **대안 전략**(예: technical-momentum_factor 고상관 시 → momentum_factor를 mean_reversion 또는 fundamental_factor로 교체)을 리포트에 제시합니다.
- **설정**: `strategies.yaml` → `ensemble`:
  - `auto_downgrade: true` — 고상관 시 conservative 자동 전환
  - `independence_threshold: 0.6` — 상관계수 기준
- **권고**: **0.6 이상**인 쌍이 있으면 다수결만으로는 독립성이 보장되지 않습니다. conservative 전환은 응급 조치이며, **근본적으로는 전략 구성을 재검토**하세요.

### 4.4b 거래량 동반 돌파 전략 — breakout_volume (C-4)

**가설**: 전고점 돌파 + 거래량 급증이 동반되면 한국 대형주에서 유효한 모멘텀 시그널.

**Entry** (edge-trigger, long only):
1. `breakout_ref = rolling_max(high, breakout_period).shift(1)` — 현재 봉 제외
2. `avg_vol_ref = rolling_mean(volume, breakout_period).shift(1)` — 현재 봉 제외
3. `close > breakout_ref AND volume > avg_vol_ref * surge_ratio AND ADX > adx_min`
4. 전일 조건 미충족 → 당일 충족 시에만 BUY (edge-trigger)

**Exit**: `close < breakout_ref` (최소 실패 신호). 나머지 손절/트레일링은 ATR 2.5 risk layer 위임.

**Frozen params**: `breakout_period=10, surge_ratio=1.5, adx_min=20`

| 검증 | 유니버스 | 기간 | return | Sharpe | MDD |
|------|----------|------|--------|--------|-----|
| OOS 포트폴리오 | 005930,000660,035720,051910 | 2024-2025 | +2.70% | -0.70 | -2.07% |
| DEV 포트폴리오 | 〃 | 2021-2023 | -2.94% | -2.83 | -4.65% |

**병목**: SIGNAL_SPARSE — 486일 중 BUY 있는 날 53일(11%). 동시 BUY 충돌 2년간 2회.

**구현**: `strategies/breakout_volume.py`, `config/strategies.yaml:breakout_volume`

### 4.4c 상대강도 회전 전략 — relative_strength_rotation (C-5)

**목표**: breakout_volume의 SIGNAL_SPARSE 구간을 보완하는 높은 days_in_market 확보.

**랭킹**: `composite = 0.6 × ret_60d + 0.4 × ret_120d`
**추세 필터**: `close > SMA(60)`
**리밸런싱**: 월간 (매월 첫 거래일). max_positions=2.
**Exit**: 리밸런싱일 모멘텀 음수/SMA 하회, 비리밸런싱일 SMA60 하향 이탈 edge-trigger.

#### Exit 최적화 (v4.0)

**Trailing stop 제거**: 승률 18-29%, negative EV → `disable_trailing_stop: true`
- DEV return: -4.99% → -0.96%, OOS: 6.18% → 4.25%
- Capture rate: 71% → 79% (DEV), 78% → 83% (OOS)

**TP 8% → 7%**: per-strategy override (`strategies.yaml: take_profit_rate: 0.07`)
- DEV: -0.96% → -0.19%, OOS: 4.25% → 4.71%

**per-strategy TP override**: `portfolio_backtester.py`에 `tp_rate_override` 파라미터 추가.

#### Entry filter 탐색 (v4.0, 모두 불채택)

| 필터 | 결과 | 판정 |
|------|------|------|
| KS11 SMA200 시장 필터 (`market_filter_sma200`) | 개선 미미 | NO_MEANINGFUL_IMPROVEMENT |
| ret_120d > 0 절대 모멘텀 (`abs_momentum_filter`) | 개선 미미 | NO_MEANINGFUL_IMPROVEMENT |
| ret_60d > 0 AND ret_120d > 0 | 개선 미미 | NO_MEANINGFUL_IMPROVEMENT |
| min_hold_days=5 냉각 기간 | 성과 악화 | ADVERSE_EFFECT (0으로 복원) |

#### 인프라 추가 (v4.0)

- `min_hold_days` 파라미터 (테스트 후 0 유지)
- `market_filter_sma200` 코드 (false로 비활성)
- `abs_momentum_filter` 코드 (none으로 비활성)
- signal/executed/skipped 카운터 (`portfolio_backtester.py`)

#### 검증 결과 (TS OFF, TP 7%)

| 검증 | 유니버스 | 기간 | return | MDD |
|------|----------|------|--------|-----|
| OOS 단독 | 005930,000660,035720,051910 | 2024-2025 | +4.71% | -3.06% |
| DEV 단독 | 〃 | 2021-2023 | -0.19% | -7.90% |

**2-sleeve 비중 스윕 결과** (TS OFF, TP 7%):

| 비중 (BV/R) | OOS return | OOS MDD | concentration |
|-------------|-----------|---------|---------------|
| BV50/R50 | +2.87% | -1.71% | 46.7% |
| BV75/R25 | +3.11% | -1.37% | 53.0% |
| BV100 | +2.70% | -2.07% | -- |

**Rolling walk-forward** (10 windows × 12mo, 6mo step):

| 비중 | positive window | median ret | worst |
|------|----------------|------------|-------|
| BV50/R50 | 60% | +0.45% | -2.05% |
| BV75/R25 | 50% | +0.07% | -1.87% |
| BV100 | 30% | -- | -2.14% |

**Sharpe sanity check** (BV50/R50):
- Full period all-days Sharpe: -1.43 (64% cash days drag)
- Position-only Sharpe: +0.07
- CMA-adjusted Sharpe (2.5% cash return assumed): +0.87

**판정**: PAPER_READY_WITH_GUARDRAILS — BV50/R50, Rotation TP=7%, TS=OFF.

**Guardrails**: monthly -3% warn, MDD -5% warn, MDD -8% halt, 3-month consecutive loss → downgrade.
**Monthly report**: `scripts/c5_paper_monthly_report.py`
**Fill rate monitoring**: signal/executed/skipped counts.

**구현**: `strategies/relative_strength_rotation.py`, `scripts/c5_sleeve_backtest.py`, `scripts/c5_weight_sweep.py`, `scripts/c5_rotation_no_ts_test.py`, `scripts/c5_rotation_tp_sweep.py`, `scripts/c5_sleeve_sweep_nots.py`, `scripts/c5_rolling_walkforward.py`, `scripts/c5_rotation_filter_test.py`, `scripts/c5_rotation_absmom_test.py`, `scripts/c5_rotation_trade_diagnostic.py`, `scripts/c5_rotation_cooling_test.py`, `scripts/c5_tp_override_verify.py`, `scripts/c5_paper_monthly_report.py`

### 4.5 전략별 수익 가능성 진단

> **이 섹션은 각 전략이 실제로 수익을 낼 수 있는지에 대한 솔직한 평가입니다.**

#### 4.5.1 스코어링 전략의 수익 가능성 — 낮음

수익을 내려면 두 가지 조건이 동시에 충족되어야 합니다.

**첫째, 지표 조합이 미래 수익률을 예측해야 합니다.** RSI, MACD, 볼린저, 이동평균은 모두 과거 가격의 변형입니다. 같은 정보를 다르게 표현할 뿐이며, 이것들을 합산한다고 예측력이 올라가지는 않습니다. 스코어의 대부분이 "가격이 최근 올랐냐 내렸냐" 한 가지 정보를 여러 번 세는 형태입니다.

**둘째, 예측력이 있더라도 거래비용을 넘는 수익을 내야 합니다.** 왕복 수수료+세금이 약 0.23%인데, 10분마다 신호를 확인하는 구조에서 신호가 자주 바뀌면 이 비용이 빠르게 누적됩니다. 백테스트에서 수익이 났더라도 실전에서는 슬리피지가 추가되어 손실로 전환될 수 있습니다.

**결론**: 스코어링 전략 단독으로 안정적인 수익을 낼 가능성은 낮습니다. 반드시 가중치 최적화 파이프라인(`check_correlation` → `optimize` → `validate --walk-forward`)을 완전히 통과한 뒤에만 사용해야 합니다.

#### 4.5.2 평균 회귀 전략의 수익 가능성 — 한국 시장에서 구조적 불리

한국 시장에서 크게 하락하는 종목의 상당수는 실적 악화나 회계 문제 등 펀더멘털 이유로 하락하며, 이런 종목은 평균으로 회귀하지 않고 계속 하락합니다. 52주 이중 필터, 코스피200 제한, 펀더멘털 필터 등을 두고 있지만 이것들이 이 문제를 완전히 해결하지는 못합니다. **Z-Score < -2인 종목이 "일시적 과매도인지 vs 망해가는 기업인지"를 기술지표만으로 구분하는 것은 원리적으로 불가능**합니다.

#### 4.5.3 추세 추종 전략의 수익 가능성 — 한국 시장에서 제한적

한국 코스피는 역사적으로 박스권 등락이 길고 추세가 빠르게 꺾이는 특성이 있습니다. 미국 나스닥에서 잘 동작하는 추세 추종 전략이 한국 시장에서 동일하게 유효하다는 실증 근거가 약합니다. ADX > 25, 200일선 상단, MACD 골든크로스가 동시에 충족되는 시점은 이미 많이 오른 이후라 진입이 늦고, 이후 조정에서 손절당하는 패턴이 반복될 수 있습니다. **손익비 2.0 이상을 반드시 확인해야 하며, 한국 시장에서 이 조건이 달성되지 않을 가능성이 높습니다.**

#### 4.5.4 앙상블의 실질적 다각화 부족

앙상블의 technical(스코어링)과 momentum_factor(N일 수익률)는 실질적으로 같은 정보를 담고 있습니다. 최근 N일 수익률이 좋으면 이동평균 골든크로스도 발생했을 가능성이 높으므로 두 전략이 같은 날 동시에 BUY를 내는 경향이 있습니다. 다수결이 의미 없어지고, **실제로는 한 가지 신호를 세 번 확인하는 것에 불과**할 수 있습니다. `auto_downgrade`가 conservative로 전환해 주지만, 그러면 거래 빈도가 너무 줄어 기회를 놓칩니다.

진정한 다각화를 위해서는 가격과 독립적인 정보(펀더멘털, 뉴스 센티먼트, 매크로 지표)를 하나 이상 추가해야 합니다.

### 4.6 지속적 손실이 발생할 수 있는 구체적 시나리오

#### 시나리오 A — 과매매로 인한 수수료 손실

10분마다 신호를 확인하고 스코어링 임계값 근처에서 신호가 BUY ↔ HOLD ↔ SELL로 자주 바뀌면, 왕복 수수료 0.23%가 빠르게 누적됩니다. **한 종목에서 한 달에 10번 왕복 매매가 발생하면 수수료만 2.3%입니다.** 전략이 수익을 내더라도 수수료가 그 이상이면 결국 손실입니다. **대응**: 신호 히스터리시스(§5.13, 구현 완료)와 최소 보유 기간(§5.14, 현재 3일, 구현 완료)이 적용되어 있으나, 직관값 가중치 상태에서는 여전히 과매매 위험이 존재할 수 있습니다.

#### 시나리오 B — 생존자 편향으로 과대평가된 백테스트

`backtest_universe.mode`가 `current`(현재 상장 종목)로 설정된 상태에서 백테스트를 하면, 기간 중 상장폐지된 종목들이 제외됩니다. 실전에서는 그 종목에도 투자했을 것이므로 **백테스트 수익률이 실전보다 수십 %p 과대평가**될 수 있습니다. 반드시 `historical` 모드로 설정하세요 (§8.2.1).

#### 시나리오 C — 파라미터 과적합

가중치 최적화를 했더라도 훈련 구간과 테스트 구간이 같은 시대(같은 시장 환경)에 속하면 OOS 성과도 높게 나올 수 있습니다. 그러나 실전에서 시장 국면이 바뀌면 최적화된 파라미터가 전혀 다르게 동작합니다. walk-forward를 통과했더라도 이 위험은 완전히 제거되지 않습니다.

#### 시나리오 D — 슬리피지 과소 추정

백테스트에서 슬리피지를 0.05%로 가정하지만, **거래량이 적은 종목에서는 실전 슬리피지가 0.3~0.5% 이상**이 될 수 있습니다. 유동성 필터(20일 평균 거래대금 50억 원 이상)가 있지만 50억 원도 충분히 큰 포지션을 들어갈 때는 슬리피지가 커집니다. 자본 규모 대비 종목별 일평균 거래대금을 반드시 확인하세요.

#### 시나리오 E — 앙상블의 허구적 다각화

위 §4.5.4에서 설명한 대로, 구성 중 둘(특히 technical + momentum_factor)이 실질적으로 동일 정보를 사용하는 경우가 많습니다. conservative 모드로 전환되면 거래 기회가 급감하고, majority_vote에서는 독립적인 검증 없이 동일 신호를 반복 확인하는 구조가 됩니다.

### 4.7 구조적으로 아쉬운 설계 결정

#### 4.7.1 단일 시장(한국)에 최적화되지 않은 전략 파라미터

200일 이동평균, ADX 25, RSI 30/70 등의 파라미터는 미국 주식 시장을 기준으로 널리 알려진 값들입니다. 한국 시장의 특성(박스권 등락, 빠른 추세 전환, 외국인·기관의 수급 영향)에 맞게 **파라미터를 한국 데이터로 최적화**해야 합니다. 현재 `--mode optimize`로 파라미터 탐색이 가능하나, 기본 검색 공간도 미국 시장 기준값 중심으로 설정되어 있습니다.

#### 4.7.2 전략이 이용하는 비효율성이 불명확한 채로 구현됨

스코어링 전략에 대해 본 문서 §4.1이 직접 "이용하는 시장 비효율성이 명시되지 않음"이라고 적고 있습니다. **전략을 만들기 전에 "왜 이 조건에서 수익이 날 수 있는가"라는 가설이 먼저 있어야 하고, 그 가설을 검증하는 방향으로 전략을 설계**해야 합니다. 현재는 가설 없이 지표를 조합한 뒤 백테스트로 수익이 나는지 확인하는 방식이며, 이는 과적합과 노이즈 트레이딩의 전형적인 패턴입니다.

---

### 4.8 팩터 기반 종목 선정 (워치리스트)

- **구현**: `core/watchlist_manager.py`
- **설정**: `config/settings.yaml` → `watchlist.mode`, `watchlist.market`, `watchlist.top_n`, **`watchlist.rebalance_interval_days`**

**학술적으로 검증된 팩터**를 이용해 관심 종목 리스트를 구성할 수 있습니다. 기존 `manual` / `top_market_cap` / `kospi200` 외에 아래 모드를 사용하면 **12개월 수익률(모멘텀)** 및 **저변동성** 팩터로 종목을 선정한 뒤, 기존 전략(scoring, mean_reversion, trend_following)으로 매매 신호를 생성합니다.

| mode | 설명 | 이용 팩터 |
|------|------|-----------|
| **momentum_top** | 12개월 수익률 상위 종목 매수 | 모멘텀(12개월 수익률) |
| **low_vol_top** | 60일 실현변동성 하위 = 저변동성 상위 종목 | 저변동성(60일 실현변동성, 연율화) |
| **momentum_lowvol** | 저변동성 필터 통과 종목 중 12개월 수익률 상위 | 모멘텀 + 저변동성 복합 |

- **모멘텀 팩터**: 과거 12개월(약 252 거래일) 수익률이 높은 종목을 선정. "좋은 주식이 일정 기간 계속 좋다"는 모멘텀 효과에 기반합니다.
- **저변동성 팩터**: 최근 60일 일일 수익률의 표준편차를 연율화(×√252)한 **60일 실현변동성**이 낮은 종목을 선정. 저변동성 주식의 위험 대비 수익이 높다는 실증 연구에 기반합니다.
- **momentum_lowvol**: 후보 풀에서 60일 변동성 중앙값 이하만 남긴 뒤, 12개월 수익률 순으로 상위 `top_n`개를 반환합니다.

**사용 예**: `watchlist.mode: momentum_top`, `watchlist.top_n: 20` 으로 설정하면 시가총액 상위 풀(기본 80여 종목)에서 12개월 수익률을 계산해 상위 20종목을 관심 종목으로 사용합니다. paper/live 모드에서 이 리스트에 대해 기존 전략으로 신호를 생성·실행합니다.

**⚠️ 리밸런싱 주기**

- **문제**: 팩터 기반 모드(momentum_top, low_vol_top, momentum_lowvol)는 매일 재계산하면 **종목 교체가 잦아져 불필요한 거래비용**이 발생합니다. 반대로 너무 드물게 갱신하면 **팩터 효과가 희석**됩니다.
- **대응**: Jegadeesh & Titman(1993) 등 모멘텀 팩터 학술 연구에 따르면 **월 1회 리밸런싱**이 일반적입니다. `watchlist.rebalance_interval_days`(기본 20)를 설정하면, `WatchlistManager`가 마지막 갱신 날짜를 `data/watchlist_cache.json`에 기록하고 **주기가 되었을 때만 재계산**합니다. 주기 내에는 캐시된 종목 리스트를 그대로 사용합니다.
- **설정**: `settings.yaml` → `watchlist.rebalance_interval_days: 20` (캘린더 일수 기준, 20일 ≈ 1개월 거래일). manual / top_market_cap / kospi200 모드에는 적용되지 않습니다.
- **캐시 강제 갱신**: `data/watchlist_cache.json` 파일을 삭제하면 다음 `resolve()` 호출 시 즉시 재계산됩니다.

**⚠️ 유동성 필터 (저유동 종목 진입 제외)**

- **문제**: 시가총액 필터만 있으면 **일평균 거래대금**이 매우 낮은 종목(예: 하루 거래량 1억 원 미만)이 watchlist에 포함될 수 있습니다. 이런 종목은 실전에서 포지션 진입/청산 시 **슬리피지**가 백테스트 가정(0.05%)보다 훨씬 커져, **백테스트 수익이 실전에서 손실**로 바뀌는 대표 원인입니다. `dynamic_slippage`로 일부 보정은 가능하지만, 아예 **진입 대상에서 제외**하는 것이 더 안전합니다.
- **대응 (2단계 필터)**:
  1. **Watchlist 구축 시점**: `WatchlistManager.resolve()` 시 20일 평균 거래대금(`close × volume`) 하한 미만 종목을 제외합니다.
     - **strict 모드** (기본 true): 거래대금 데이터를 조회할 수 없는 종목도 제외. 데이터 없는 종목이 자동 포함되는 위험을 방지합니다.
     - strict=false: 데이터 없으면 통과 (수동 watchlist에서 직접 지정한 종목 유지 용도).
  2. **주문 직전 재검증**: `OrderExecutor._execute_buy_impl()`에서 매수 직전에 `avg_daily_volume × price`로 추정 일평균 거래대금을 재확인합니다. watchlist 구축 이후 유동성이 변했을 수 있으므로, 하한 미만이면 매수를 거부합니다. `check_on_entry: true`(기본)로 활성화.
- **설정**: `config/risk_params.yaml` → `liquidity_filter`:
  - `enabled: true` (권장)
  - `min_avg_trading_value_20d_krw: 5e9` (50억 원)
  - `strict: true` (데이터 없는 종목도 제외)
  - `check_on_entry: true` (매수 직전 재검증)
- **보조**: `dynamic_slippage`가 주문 비중(일평균 거래량의 1%/3%) 기준으로 슬리피지를 동적 상향합니다. 유동성 필터와 함께 사용하면 이중 안전장치.

---

## 5. 리스크 관리

구현: **`core/risk_manager.py`**, 설정: **`config/risk_params.yaml`**

### 5.1 포지션 사이징 — 1% 룰 + 신호 강도 스케일링 (v3.0)

- **규칙**: 1회 거래 최대 손실 = 자본의 1%
- **설정**: `position_sizing.max_risk_per_trade: 0.01`, `initial_capital`
- **신호 강도 스케일링** (`position_sizing.signal_scaling`, 기본 `enabled: true`): `calculate_position_size(..., signal_score=)` 에서 스코어 절댓값이 클수록 기본 수량에 **선형 보간 배수** 적용(기본 `min_scale` 0.5 ~ `max_scale` 1.5, `score_range` 예: [2, 5]). **OrderExecutor** 매수 시 `signal_score` 전달.

### 5.2 손절매 (Stop Loss)

- **타입**: `fixed`(고정 비율) 또는 `atr`(변동성 기반). `stop_loss.type`, `fixed_rate`, `atr_multiplier`
- **시장 국면 배수 (v3.0)**: `calculate_stop_loss(..., regime_multiplier=1.0)` — **OrderExecutor**가 `get_regime_adjusted_params()`의 `stop_loss_multiplier`를 곱해 하락장에서 손절을 타이트하게 조정. `strategies.yaml` → `regime_adaptive` 참고.

### 5.3 익절매 (Take Profit)

- **설정**: `take_profit.fixed_rate`, `partial_exit`, `partial_ratio`, `partial_target` (부분 익절)
- **현재 기본값**: 전량 익절 `fixed_rate: 0.08` (8%), 부분 익절 `partial_target: 0.04` (4%), `partial_ratio: 0.5` (50%)
- **시장 국면 배수 (v3.0)**: `calculate_take_profit(..., regime_multiplier=1.0)` — bearish/caution 시 익절 목표를 낮춰 빠른 실현.

### 5.4 트레일링 스탑

- **설정**: `trailing_stop.enabled`, `type`, `fixed_rate`, `atr_multiplier`
- **현재 기본값**: `fixed_rate: 0.05` (5%) — 한국 시장 일간 변동성 대비 조기 청산을 방지하기 위해 3%에서 상향 조정

### 5.5 분산 투자 (업종별 비중 제한 포함)

- **설정**: `diversification.max_position_ratio`, `max_investment_ratio`, `max_positions`, `min_cash_ratio`, **`max_sector_ratio`**

**⚠️ 포지션 간 상관관계 — 분산 투자가 실제 분산이 아닐 수 있음**

- **문제**: `max_positions`·`max_position_ratio`로 종목 수·비중을 제한해도, momentum_top 등으로 코스피 상위 20종목을 선정하면 **반도체·IT·금융주**가 함께 들어가 시장 하락 시 동시에 급락합니다. 종목이 20개라도 **실질적 리스크는 1~2개 업종에 집중**될 수 있습니다.
- **대응**: `diversification.max_sector_ratio`(기본 0.40 = 40%)를 설정하면, 매수 시 **해당 종목의 업종(KRX Sector)**이 기존 보유 포지션 중 동일 업종 총 투자금과 합산해 총자산 대비 상한을 초과하면 매수를 차단합니다. 업종 정보는 `DataCollector.get_sector_map()`으로 FDR `StockListing('KRX')`의 `Sector` 컬럼을 사용합니다.
- **동작**: `OrderExecutor._execute_buy_impl()` → `RiskManager.check_diversification(symbol=, sector_map=, positions=)` 에서 업종 비중 초과 시 `{"can_buy": False, "reason": "업종 'XXX' 비중 N% > 상한 40%"}` 반환.
- **FDR 미설치·조회 실패**: 업종 매핑이 빈 dict이면 업종 체크는 자동 스킵되어 기존처럼 동작합니다.

**종목 간 가격 수익률 상관관계 (v3.0)**

- **문제**: 업종 분산만으로는 **동일 팩터(예: 반도체)** 에 몰린 고상관 종목 다수 보유를 막기 어렵습니다.
- **설정**: `diversification.correlation_risk` — `enabled`, `lookback_days`, `high_corr_threshold`(기본 0.7), `high_corr_scale`(기본 0.5)
- **동작**: `RiskManager.check_correlation_risk(symbol, existing_symbols)` 가 일봉 수익률 상관을 계산해 고상관 보유 종목이 있으면 **계산된 수량에 scale을 곱해 축소**. `OrderExecutor` 매수 직전 호출.

### 5.6 최대 보유 기간

- **설정**: `position_limits.max_holding_days` (N일 초과 시 강제 매도, 0이면 비활성)

### 5.7 MDD 제한

- **설정**: `drawdown.max_portfolio_mdd`, `max_daily_loss`, `recovery_scale`

### 5.8 전략 성과 열화 감지

- **설정**: `performance_degradation` (recent_trades, min_win_rate). 최근 N거래 승률이 임계값 미만이면 **신규 매수만** 중단.

### 5.9 거래 비용

- **설정**: `transaction_costs` (commission_rate, tax_rate 0.20% 증권거래세+농특세(2026년~), slippage, capital_gains_tax, dynamic_slippage). 백테스트·실거래 일치를 위해 반드시 반영.

### 5.10 블랙스완 대응 (긴급 청산 + 재진입)

- **구현**: `core/blackswan_detector.py`. 급락 감지 시 전량 매도·디스코드 경고·쿨다운 동안 신규 매수 차단.
- **설정 (v3.0 — `config/risk_params.yaml` → `blackswan`)**: 임계값·쿨다운·recovery를 **코드 하드코딩 없이** YAML에서 조정합니다.
  - `single_stock_threshold` (기본 -0.05): 개별 종목 전일 대비 급락
  - `portfolio_threshold` (기본 -0.03): 포트폴리오 일일 급락
  - `consecutive_days`, `consecutive_threshold`: 연속 하락 감지
  - `cooldown_minutes` (기본 60): 쿨다운 기본 길이(반복 발동 시 최대 240분까지 증가)
  - `recovery_minutes` (기본 120), `recovery_scale` (기본 0.5): 쿨다운 해제 후 점진적 재진입
- **참고**: `settings.yaml`의 `trading.blackswan_recovery_minutes` / `blackswan_recovery_scale`은 **과거 문서 호환용**으로 언급되었으나, **현재 구현은 `risk_params.yaml`의 `blackswan` 블록을 우선**합니다. 운영 시 한 곳(`risk_params`)으로 통일하는 것을 권장합니다.

**⚠️ 쿨다운 이후 재진입 로직**

- **문제**: 블랙스완 전량 매도 → 쿨다운 만료 후, 시장이 회복되었을 때 다음 모니터링 사이클까지 대기하면 급락 직후 반등 구간을 놓칠 수 있습니다. 또한 곧바로 100% 사이징으로 재진입하면 하락이 더 이어질 때 추가 손실 위험이 있습니다.
- **대응**:
  1. **즉시 신호 재평가**: 쿨다운이 해제되는 순간 `BlackSwanDetector.consume_cooldown_ended_flag()`가 `True`를 반환하고, `Scheduler._run_monitoring()`이 이를 감지해 **워치리스트 전 종목을 즉시 재스캔**(`_run_post_cooldown_rescan`)합니다. 매수 신호가 나오면 진입 후보에 추가되어 같은 사이클에서 실행됩니다.
  2. **점진적 사이징 복구 (recovery)**: 쿨다운 해제 시 `recovery_minutes`(risk_params `blackswan`) 동안 **recovery 기간**에 진입합니다. 이 기간 중 `get_recovery_scale()`이 `recovery_scale`을 반환하여, 포지션 사이징이 **시장 국면 scale × recovery scale**로 곱연산됩니다. 예: 시장 국면 caution(50%) + recovery(50%) → 사이징 25%.
  3. **recovery 종료 후**: `_recovery_until` 경과 시 자동으로 `1.0` 복귀, 정상 사이징으로 운영됩니다.

### 5.11 실적 발표일(어닝) 필터

- **구현**: `core/earnings_filter.py` → `is_near_earnings(symbol, skip_days)`
- **설정**: `config/settings.yaml` → `trading.skip_earnings_days`(기본 3, 0이면 비활성)

**⚠️ 공시·이벤트 리스크**

- **문제**: 실적 발표일, 유상증자 공시, 주요 계약 공시 등이 발생하면 주가가 단기에 급변합니다. 현재 시스템은 기술적 지표만으로 신호를 내므로, **실적 발표 전날 매수 → 어닝 쇼크로 -10% 갭 하락** 같은 상황에 무방비입니다.
- **대응**: `skip_earnings_days: 3` 으로 설정하면, 매수 주문 실행 전 해당 종목의 다음 실적 발표 예정일을 조회해 **전후 3일 이내**이면 신규 매수를 금지합니다. 기존 포지션의 매도·손절은 정상 동작합니다.
- **데이터 소스 (우선순위)**: (1) yfinance `Ticker.calendar`의 `earningsDate` (2) yfinance에서 없거나 실패 시 **`core/dart_loader.py`의 DART Open API**로 정기공시 접수 이력 기반 차기 실적 시점 추정. `settings.yaml`의 `dart.enabled`·`dart.api_key`(또는 환경변수 `DART_API_KEY`)가 있을 때만 DART 경로가 동작합니다.
- **한계**: DART 연동은 **한국 종목 실적일 보강**을 목표로 한 1차 구현이며, 모든 공시 유형·예정일을 완전 커버하지는 않습니다. 둘 다 없으면 기존과 같이 필터 통과(매수 허용)입니다.
- **동작 위치**: `OrderExecutor._execute_buy_impl()` 에서 분산 투자 체크 직전에 실행됩니다.

### 5.12 시장 국면 필터 (단계적 대응 — 3중 신호)

- **구현**: `core/market_regime.py` → `check_market_regime()` (하위 호환: `allow_new_buys_by_market_regime()`)
- **설정**: `config/settings.yaml` → `trading.market_regime_*`

**200일선 단독의 한계**: 200일선은 정의상 매우 느려서, 시장이 본격 하락한 뒤 한참 지나서야 필터가 작동합니다(예: 2020-03 코로나 급락 시 200일선 이탈은 급락 후 수 주 후). 이를 보완하기 위해 **단기 모멘텀 + 단기 MA 크로스를 병행한 3중 신호 단계적 대응**을 적용합니다.

**3가지 독립 신호**:

| 신호 | 조건 | 반응 속도 | 용도 |
|---|---|---|---|
| **A. 200일선 이탈** | 종가 < MA(200) | 느림 (수 주~수 개월) | 장기 추세 확인 |
| **B. 단기 모멘텀 하락** | N일 수익률 ≤ threshold (기본 -5%) | 중간 (수 일) | 급락 즉시 감지 |
| **C. 단기 MA 데드크로스** | MA(20) < MA(60) | 빠름 (1~2주) | 200일선 이탈 전에 추세 전환 포착 |

**국면 판별 로직** — 신호 개수 기준:

| 충족 신호 수 | 예시 | 결과 |
|---|---|---|
| **2개 이상** | A+B, A+C, B+C, A+B+C | **bearish** — 신규 매수 전면 중단 (position_scale=0.0) |
| **1개** | A만, B만, C만 | **caution** — 포지션 사이징 축소 (기본 50%) |
| **0개** | — | **bullish** — 정상 (position_scale=1.0) |

**왜 3중 신호인가?**: 2020년 3월 코로나 급락 사례에서 신호 C(20/60일선 데드크로스)는 200일선 이탈보다 **2~3주 먼저** 트리거됩니다. 신호 B(20일 수익률 -5%)는 급락 당일~수일 내에 트리거됩니다. 이 두 빠른 신호가 조합되면, 200일선이 아직 이탈하지 않아도 **bearish 판정**이 가능하여 조기 방어가 됩니다.

**파라미터** (`settings.yaml` → `trading`):

| 키 | 기본값 | 설명 |
|---|---|---|
| `market_regime_filter` | true | 필터 활성화 여부 |
| `market_regime_index` | KS11 | 기준 지수 (코스피) |
| `market_regime_ma_days` | 200 | 신호 A: 장기 이동평균 일수 |
| `market_regime_short_momentum_days` | 20 | 신호 B: 단기 모멘텀 산출 기간 (거래일) |
| `market_regime_short_momentum_threshold` | -5.0 | 신호 B: 해당 기간 수익률(%) 이하 시 하락 판단 |
| `market_regime_caution_scale` | 0.5 | caution 국면에서 포지션 사이징 배수 |
| `market_regime_ma_cross_enabled` | true | 신호 C: 단기 MA 크로스 활성화 여부 |
| `market_regime_ma_short` | 20 | 신호 C: 단기 이동평균 일수 |
| `market_regime_ma_mid` | 60 | 신호 C: 중기 이동평균 일수 (short < mid 이면 데드크로스) |

**하위 호환**: 신호 C를 비활성화(`market_regime_ma_cross_enabled: false`)하면 기존 2-신호(A+B) 로직과 동일하게 동작합니다. 기존 포지션의 매도·손절·익절·트레일링 스탑은 국면과 무관하게 그대로 동작합니다. paper/live 모드 및 스케줄러 장전·장중 진입 시 지수 데이터를 조회해 국면을 판별하며, 조회 실패 시 보수적으로 신규 매수를 허용합니다(API 장애로 인한 진입 기회 상실 방지). 비활성화하려면 `market_regime_filter: false` 로 설정하면 됩니다.

### 5.13 신호 히스터리시스 (과매매 방지) — 구현 완료

- **문제**: 스코어가 임계값 근처에서 오락가락할 때 BUY ↔ HOLD ↔ SELL 신호가 자주 전환되어 과매매가 발생합니다.
- **구현**: 상태 전이가 반드시 BUY ↔ HOLD ↔ SELL 순서를 따르며, BUY → SELL 직접 전환을 차단합니다.
  - HOLD → BUY: score >= `buy_threshold` (현재 2)
  - HOLD → SELL: score <= `sell_threshold` (현재 -2)
  - BUY → HOLD: score < `exit_buy_threshold` (현재 0.5)
  - SELL → HOLD: score > `-exit_sell_threshold` (현재 1)
- **효과**: 임계값 근처에서의 불필요한 왕복 거래를 줄여 수수료 비용 절감.
- **설정**: `strategies.yaml` → `scoring.hysteresis` (`enabled`, `exit_sell_threshold`, `exit_buy_threshold`)
- **구현 위치**: `core/signal_generator.py` → `_generate_with_hysteresis()` 메서드. 백테스터에서도 동일하게 적용됨.

### 5.14 최소 보유 기간 — 구현 완료

- **문제**: 매수 후 즉시 매도 신호가 나오면 왕복 수수료만 소모합니다.
- **구현**: 매수 후 최소 N일은 보유하도록 강제. 손절·블랙스완·트레일링 스탑은 예외.
  - 현재 설정: `min_holding_days: 5` (매수 후 5일 미만이면 일반 매도 차단, 3→5일 강화)
- **효과**: 단타 왕복을 줄이고, 전략이 의도한 보유 기간을 확보.
- **설정**: `risk_params.yaml` → `position_limits.min_holding_days` (현재 3, 0 = 비활성)
- **구현 위치**: `core/order_executor.py` → `_execute_sell_impl()` (실전), `backtest/backtester.py` → `_simulate()` (백테스트) 양쪽 모두 적용.

### 5.15 갭 리스크 방어 (`gap_risk`) — v3.0

- **목적**: 전일 종가 대비 시가·현재가가 크게 **갭다운**이면 지정가 손절이 체결되지 않고 손실이 확대될 수 있음. **갭업 추격 매수**는 단기 과열 구간 진입 위험.
- **설정**: `config/risk_params.yaml` → `gap_risk` (`enabled`, `gap_down_threshold` 기본 -3%, `gap_up_entry_block` 기본 +5%)
- **동작**:
  - **스케줄러** `_check_exit_signals`: 전일 대비 현재가가 `gap_down_threshold` 이하이면 해당 포지션 **즉시 매도** 시도·알림.
  - **OrderExecutor** 매수 전: 당일 시가(또는 최근 봉)가 전일 종가 대비 `gap_up_entry_block` 이상이면 **신규 매수 차단**.

### 5.16 시장 국면 적응형 전략 파라미터 (`regime_adaptive`) — v3.0

- **목적**: `check_market_regime()` 결과(bullish / caution / bearish)에 따라 **손절·익절 배수**를 바꿔 하락장에서 손실 속도를 줄이고 익절을 빨리 가져감.
- **설정**: `config/strategies.yaml` → `regime_adaptive` (`enabled`, `bullish` / `caution` / `bearish` 각각 `buy_threshold_offset`, `stop_loss_multiplier`, `take_profit_multiplier`)
- **구현**: `core/market_regime.py` → `get_regime_adjusted_params(config, collector)`  
  **OrderExecutor**가 매수 시 `calculate_stop_loss` / `calculate_take_profit`에 국면 배수 전달.

### 5.17 스케줄러 장중 — 동적 손절 갱신·신호 재스캔 — v3.0

- **동적 손절**: `_update_dynamic_stop_losses()` — 보유 종목별 최신 일봉·ATR로 손절가 재계산. **기존보다 손절가만 높아지는(래칟) 방향**만 DB 반영 (`database.repositories.update_stop_loss_price` — 2026-04-15 현재 `update_position_targets(stop_loss_price=...)`에 위임하는 compat shim).
- **장중 재스캔**: `auto_entry` 가 켜진 경우 `_rescan_for_new_entries()` — 워치리스트에서 미보유 종목을 다시 분석해 BUY 신호 시 `_entry_candidates`에 추가(시장 국면 bearish면 스킵).
- **위치**: `core/scheduler.py` → `_run_monitoring()` 내부, 손절/익절 체크 후 실행.

---

## 6. 시스템 아키텍처 및 프로젝트 구조

### 6.1 계층별 구조

```
┌─────────────────────────────────────────────────────────────────┐
│                      📊 모니터링 레이어                          │
│ 통합 알림(Discord→Telegram→Email) │ 수익률 로깅 │ 웹 대시보드(aiohttp) │
├─────────────────────────────────────────────────────────────────┤
│                      ⚡ 실행 레이어                              │
│ 주문 생성 │ OrderGuard │ 재시도(지수 백오프+지터) │ Dead-letter 큐 │ 바스켓 리밸런서 │
├─────────────────────────────────────────────────────────────────┤
│                      🛡️ 리스크 관리 레이어                       │
│ 손절/익절/트레일링 │ 포지션 사이징·신호스케일 │ 상관축소·갭리스크 │ MDD·성과열화·시장국면·국면적응 │
├─────────────────────────────────────────────────────────────────┤
│                      🎯 전략 레이어                              │
│ 전략 레지스트리(플러그인) │ 스코어링/평균회귀/추세추종/펀더멘털/앙상블 │ generate_signal │
├─────────────────────────────────────────────────────────────────┤
│                      🔬 분석 엔진                                │
│     IndicatorEngine │ SignalGenerator │ strategies.yaml 가중치   │
├─────────────────────────────────────────────────────────────────┤
│                      💾 데이터 레이어                             │
│     DataCollector(FDR/yfinance/KIS, 미국 티커 yfinance) │ SQLAlchemy │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 실제 프로젝트 디렉토리 및 파일 역할

```
quant_trader/
├── main.py                      # CLI 진입점. --mode 로 backtest/backtest_momentum_top/validate/paper/schedule/live/liquidate/compare/optimize/dashboard/check_correlation/check_ensemble_correlation/rebalance 분기
├── test_integration.py          # 통합 검증 스크립트 (설정·DB·지표·백테스트·디스코드 등 일괄 점검, 단일 실행)
├── pyproject.toml               # 프로젝트 메타데이터 (Python >=3.11,<3.13, 패키지 구성, pytest 설정)
├── requirements.txt             # pip 의존성 목록 (pandas, numpy, pandas-ta, pykrx, yfinance, sqlalchemy 등)
├── .env.example                 # 환경변수 템플릿 (KIS API 키, 디스코드, 텔레그램, 이메일, 긴급청산 토큰)
├── .gitignore                   # .env, settings.yaml, data/, logs/, *.db, reports/*, fintics/ 등 제외
├── README.md                    # 프로젝트 소개·빠른 시작·실행 예시
├── quant_trader_design.md       # 전체 아키텍처·지표·전략·리스크 설계서 (본 문서)
├── config/
│   ├── __init__.py
│   ├── config_loader.py         # YAML 통합 로더. settings/strategies/risk_params 로드, .env 덮어쓰기, Config.get() 싱글톤
│   ├── settings.yaml.example    # 설정 예시 (실제 settings.yaml 은 .gitignore)
│   ├── settings.yaml            # KIS API, database, logging, data_source, trading, discord, telegram, dashboard, watchlist
│   ├── strategies.yaml          # indicators, scoring, mean_reversion, trend_following, fundamental_factor, momentum_factor, volatility_condition, ensemble 파라미터
│   ├── risk_params.yaml         # backtest_universe, liquidity_filter, 포지션/손절/익절/트레일링/분산/MDD/성과열화/거래비용
│   ├── baskets.yaml             # 바스켓 포트폴리오 & 리밸런싱 설정 (종목별 목표 비중, drift/weekly/monthly 트리거, 신호 가중 모드)
│   ├── holidays.yaml.example    # 휴장일 예시
│   ├── holidays.yaml            # 한국 휴장일 (--update-holidays 로 pykrx+fallback 자동 갱신)
│   └── us_holidays.yaml         # 미국 휴장일(선택). NYSE 정규장 판별 시 `trading_hours`에서 로드
├── core/
│   ├── __init__.py
│   ├── data_collector.py        # fetch_stock(통합): 미국 티커는 yfinance, 한국은 FDR→yfinance→KIS 폴백. 소스 추적·수정주가. get_krx_stock_list, get_sector_map(), is_us_ticker()
│   ├── watchlist_manager.py     # 관심 종목: manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol + 유동성 필터 + 리밸런싱 주기(캐시) + as_of_date 지원(백테스트 시 과거 유니버스)
│   ├── indicator_engine.py      # pandas-ta: RSI, MACD, 볼린저, MA(SMA/EMA), 스토캐스틱, ADX, ATR, OBV, volume_ratio. calculate_all(df)
│   ├── signal_generator.py      # 멀티 지표 스코어링 신호 (BUY/SELL/HOLD, score, score_details). collinearity_mode(representative_only 권장)
│   ├── risk_manager.py          # 포지션 사이징(1% 룰·신호 강도 스케일), check_diversification(업종), **check_correlation_risk**, check_recent_performance, 손절/익절/트레일링(국면 배수), 거래비용
│   ├── order_executor.py        # 매수/매도. 국면 손절·익절, 상관 축소, **갭업 매수 차단**, 유동성·어닝·분산, Dead-letter
│   ├── portfolio_manager.py     # 보유 포지션·잔고·수익률. sync_with_broker(KIS 잔고↔DB 크로스체크), save_daily_snapshot()
│   ├── basket_rebalancer.py     # 바스켓 리밸런싱: 목표 비중 vs 실제 비중 드리프트 감지, 주문 생성·실행, 신호 가중 모드, 스케줄러 장전 자동 통합
│   ├── scheduler.py             # 장전/장중(10분)/장마감. **갭다운 즉시 청산**, 동적 손절 갱신, auto_entry 시 장중 재스캔, 블랙스완 recovery, 바스켓 리밸런싱, paper 실전 전환 평가
│   ├── runtime_lock.py        # `data/.scheduler.lock` — schedule 모드 단일 인스턴스(중복 실행 방지)
│   ├── trading_hours.py         # 한국 장·휴장일(holidays.yaml → pykrx → fallback). 미국: us_holidays.yaml + 동부 09:30~16:00 (`is_us_trading_day` 등)
│   ├── holidays_updater.py      # 휴장일 YAML 자동 갱신 (pykrx 또는 fallback)
│   ├── blackswan_detector.py    # 급락 감지 — 임계값·쿨다운·recovery는 **risk_params.blackswan**
│   ├── market_regime.py         # 시장 국면 3중 신호 + **get_regime_adjusted_params()** (손절·익절 국면 배수)
│   ├── fundamental_loader.py    # 펀더멘털(PER·부채비율) 조회 — pykrx(우선) → yfinance(폴백). 평균회귀 필터용
│   ├── dart_loader.py           # DART Open API: corp_code 매핑, 정기공시 기반 실적 시점 추정(earnings_filter 폴백)
│   ├── earnings_filter.py       # 실적일 필터: yfinance → (선택) DART 추정. trading.skip_earnings_days
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 분석·고상관 쌍 제거 권고 (check_correlation 모드)
│   ├── ensemble_correlation.py  # 앙상블 전략 신호 상관계수 + BUY 동시 발생률 + 대안 전략 권고 + auto_downgrade
│   ├── strategy_ensemble.py     # 앙상블: ensemble.components (technical·momentum_factor·volatility_condition·fundamental_factor 선택), auto_downgrade
│   ├── data_validator.py        # OHLCV 정합성 검사 (Null, NaN, 음수 주가, 타임스탬프 역전 등)
│   ├── notifier.py              # 통합 알림 이중화 (1차 디스코드 → 2차 텔레그램 → 3차 이메일, critical 시 전채널 동시 발송)
│   ├── strategy_diagnostics.py  # 전략 진단 보조: DiagnosticLine — 전략별 신호·점수 진단 라인 생성
│   ├── position_lock.py         # threading.RLock (포지션/주문 동시 접근 제어)
│   └── order_guard.py           # 동일 종목 TTL(기본 600초) 동안 중복 주문 차단
├── strategies/
│   ├── __init__.py              # 전략 레지스트리(플러그인형): create_strategy(name), get_strategy_names(), register_strategy()
│   ├── base_strategy.py         # 추상 클래스. analyze(df), generate_signal(df, **kwargs)
│   ├── scoring_strategy.py      # IndicatorEngine + SignalGenerator, 멀티 지표 스코어링 전략
│   ├── mean_reversion.py        # Z-Score·ADX·52주 이중 필터·코스피200 제한·펀더멘털 필터 평균 회귀
│   ├── trend_following.py       # ADX·200일선·MACD·ATR 추세 추종
│   ├── momentum_factor.py       # 모멘텀 팩터 (N일 수익률, CLI `--strategy momentum_factor` 등록 + 앙상블 구성용)
│   ├── volatility_condition.py  # 변동성 조건 (앙상블 내부용)
│   └── fundamental_factor.py    # 펀더멘털 팩터 (--strategy fundamental_factor 및 앙상블 구성)
├── api/
│   ├── __init__.py
│   ├── kis_api.py               # KIS REST API: 토큰·시세·주문·잔고·일봉. 이중 Rate Limiter(Token Bucket 초당 + 슬라이딩 윈도우 분당) + 지수 백오프+지터 + SSL/커넥션 에러 핸들러 + 토큰 쿨다운 + 사용량 모니터링 + Circuit Breaker
│   ├── websocket_handler.py     # KIS 웹소켓 실시간 체결/호가 (asyncio, Heartbeat 45초, 자동 재연결)
│   └── circuit_breaker.py       # CLOSED → OPEN → HALF_OPEN. API 연속 5회 실패 시 60초 차단, Notifier 알림
├── backtest/
│   ├── __init__.py
│   ├── backtester.py            # 단일 종목 시뮬. strict_lookahead, 과매매 분석, **Sortino·VaR/CVaR·연속손실·MDD회복기간** 등 메트릭
│   ├── portfolio_backtester.py  # 멀티종목 포트폴리오 시뮬(분산·최대 포지션 등)
│   ├── report_generator.py      # txt·html 리포트 (거래 내역, 성과 지표, 자본 곡선, 과매매 분석)
│   ├── strategy_validator.py    # validate: 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중), in/out-of-sample, 손익비 자동 경고+디스코드
│   ├── momentum_top_portfolio.py # 다종목 동일비중 모멘텀 포트폴리오 백테스트 (리밸런싱·시장 국면 필터·포트폴리오 스탑). run_momentum_top_portfolio_backtest(), print_momentum_top_portfolio_report()
│   ├── paper_compare.py         # 모의투자 vs 백테스트 비교, divergence 경고, 실전 전환 준비 자동 평가(check_live_readiness)
│   └── param_optimizer.py       # Grid / Bayesian(scikit-optimize) 파라미터 최적화, train_ratio·OOS 보고, 가중치 대칭 Grid Search
├── database/
│   ├── __init__.py
│   ├── models.py                # ORM 모델 6종(StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport, FailedOrder). SQLite WAL/PostgreSQL 지원, scoped_session, @with_retry, db_session()
│   ├── repositories.py          # CRUD, **update_stop_loss_price**(래칟 손절 갱신 — `update_position_targets`에 위임하는 compat shim), Dead-letter, 스냅샷 등
│   └── backup.py                # SQLite Online Backup API로 WAL 안전 백업 (실패 시 shutil 폴백 + -wal/-shm 포함), 보관 일수 자동 삭제
├── monitoring/
│   ├── __init__.py
│   ├── logger.py                # loguru 초기화 (파일 로테이션·콘솔 출력), log_trade(), log_signal()
│   ├── discord_bot.py           # 디스코드 웹훅 전송 (매매·일일 리포트·블랙스완·동기화 불일치). Notifier를 통해 호출 권장
│   ├── liquidate_trigger.py     # HTTP POST /liquidate 로 긴급 전량 매도 트리거 (X-Token 또는 ?token= 인증)
│   ├── dashboard.py             # 콘솔 대시보드 (선택, show_summary_line)
│   ├── dashboard_runtime_state.py # 대시보드 런타임 상태 관리 (스케줄러·전략 실행 현황 등 실시간 상태 전달)
│   └── web_dashboard.py         # aiohttp 웹 대시보드 (포트폴리오·스냅샷 JSON/HTML, 10초 폴링)
├── tests/
│   ├── __init__.py
│   ├── test_backtester_strategies.py    # 백테스터 전략별 시뮬레이션 검증
│   ├── test_backtester_trailing_stop.py # 트레일링 스탑 로직 검증
│   ├── test_blackswan_detector.py       # 블랙스완 감지·쿨다운 로직 검증
│   ├── test_discord_bot.py              # 디스코드 알림 모킹·콘솔 fallback
│   ├── test_integration_smoke.py        # 설정·DB·지표·신호 등 연동 스모크 테스트
│   ├── test_kis_websocket_e2e.py        # KIS API·웹소켓 모의 E2E 테스트
│   ├── test_order_executor_paper.py     # OrderExecutor paper 모드 검증
│   ├── test_portfolio_manager.py        # 포트폴리오·sync 검증
│   ├── test_risk_manager.py             # 리스크 매니저 (포지션·손절·동적 슬리피지 등)
│   ├── test_scheduler.py                # 스케줄러 구간·동작 검증
│   ├── test_signal_generator.py         # 신호 생성·스코어링 검증
│   ├── test_strategy_validator.py       # 전략 검증(validate) 로직 검증
│   ├── test_trading_hours.py            # 장 시간·휴장일 검증
│   ├── test_watchlist_manager.py        # watchlist 모드별 resolve 검증
│   ├── test_basket_rebalancer.py       # 바스켓 리밸런서 (설정·비중·드리프트·트리거·주문·실행)
│   └── test_us_market_support.py      # fetch_stock 미국 라우팅·TradingHours 미국 장 판별 등
├── deploy/                      # (선택) Oracle Cloud ARM 서버 상시 구동
│   ├── README.md               # Oracle Cloud Free Tier ARM 배포 가이드
│   ├── setup.sh                # 시스템 셋업 (Python 3.11, venv, pip install)
│   ├── install_service.sh      # systemd 서비스 등록 스크립트
│   ├── quant_trader.service    # systemd 유닛 파일 (schedule 모드, auto-restart)
│   └── logrotate.conf          # 로그 로테이션 정책 (copytruncate)
├── docs/
│   ├── PROJECT_GUIDE.md         # 파일별 역할·실행 모드·데이터 흐름 상세
│   └── BACKTEST_IMPROVEMENT.md  # 백테스트 손익 개선 포인트 (손익비·상승장·손절/익절·가중치 파이프라인)
└── reports/                     # 백테스트 txt/html 출력 (.gitignore로 제외)
```

### 6.3 저장소 관리 (Git)

- **커밋 대상**: Python 소스(`*.py`), 설정 예시(`*.example`), `requirements.txt`, `pyproject.toml`, `README.md`, 문서(`*.md`).
- **커밋 제외 (.gitignore)**:
  - `.env`, `config/settings.yaml` — 비밀·환경 정보
  - `__pycache__/`, `.venv/`, `.pytest_cache/` — Python 런타임
  - `data/`, `logs/`, `*.db` — 데이터·로그
  - `reports/backtest_*.html`, `reports/backtest_*.txt`, `reports/*.md` — 백테스트 산출물
  - `fintics/` — 외부 프로젝트 (본 저장소는 quant_trader 소스만 관리)
  - `.idea/`, `.vscode/` — IDE 설정

### 6.4 핵심 설계 원칙

| 원칙 | 설명 |
|------|------|
| **모듈화** | 계층별 독립 교체·테스트 가능 |
| **설정 외부화** | YAML(config) + .env. 코드 수정 없이 전략·리스크 조정 |
| **Look-Ahead Bias 방지** | 백테스트 strict_lookahead 기본 True (시점별 슬라이싱) |
| **장애 복구** | 재시도, Circuit Breaker, OrderGuard, 미체결 확인, KIS↔DB 크로스체크 |
| **로깅 필수** | 신호 점수·주문 사유·손절 사유 등 상세 로그 |

---

## 7. 실행 모드 및 CLI

진입점: **`main.py`**. 인자: `--mode`, `--strategy`, `--symbol`, `--start`, `--end` 등.

| 모드 | 설명 | 핵심 호출 |
|------|------|-----------|
| **backtest** | 백테스트 실행 (단일 종목) | `run_backtest()` → DataCollector → Backtester.run(strict_lookahead 기본) → ReportGenerator |
| **backtest_momentum_top** | 다종목 동일비중 모멘텀 포트폴리오 백테스트. 리밸런싱·시장 국면 필터·포트폴리오 스탑 지원 | `run_backtest_momentum_top()` → momentum_top_portfolio.run_momentum_top_portfolio_backtest() |
| **validate** | 전략 검증 (3~5년, 샤프·MDD·벤치마크·in/out-of-sample). `--walk-forward` 시 워크포워드 | `run_strategy_validation()` → StrategyValidator.run / run_walk_forward |
| **paper** | 모의투자 1회 순회 (워치리스트 종료 후 프로세스 종료) | `run_paper_trading()` → WatchlistManager, 전략.generate_signal, OrderExecutor(paper) |
| **schedule** | 모의용 무한 스케줄 루프 (systemd 상시 구동용). 기본=signal-only, `QUANT_AUTO_ENTRY=true` 시 full paper. `trading.mode=live`이면 거부. runtime state가 entry만 막아도 exit/finalize/evidence는 계속 허용 | `run_scheduler_loop()` → `runtime_lock` + `Scheduler.run()` |
| **live** | 실전 매매. **4중 보안**: ① `is_strategy_allowed(live)` ② `ENABLE_LIVE_TRADING=true` ③ `--confirm-live` ④ canonical bundle + paper evidence hard gate | `run_live_trading()` → hard gate → KIS 인증 → Scheduler.run() |
| **liquidate** | 긴급 전 종목 매도 | `run_emergency_liquidate()` → DB 포지션 조회 → 종목별 매도 |
| **compare** | 모의투자 vs 백테스트 비교 + **실전 전환 준비 평가** | `run_compare_paper_backtest()` → paper_compare.run_compare + check_live_readiness |
| **optimize** | 전략 파라미터 최적화 (grid / bayesian / 가중치 대칭 Grid) | `run_param_optimize()` → param_optimizer, train_ratio·OOS |
| **dashboard** | 웹 대시보드 기동 | `run_dashboard()` → monitoring.web_dashboard (aiohttp, 기본 8080) |
| **check_correlation** | 스코어링 지표 간 상관계수·독립성 검증 (0.7 이상 쌍 제거/가중치 축소 권고) | `run_check_indicator_correlation()` → core.indicator_correlation |
| **check_ensemble_correlation** | 앙상블 전략 신호 상관계수 + BUY 동시 발생률 검증. 0.6 이상이면 conservative 전환 또는 재구성 권고 | `run_check_ensemble_correlation()` → core.ensemble_correlation |
| **rebalance** | 바스켓 포트폴리오 리밸런싱. `--basket`으로 대상 지정, `--dry-run`으로 미리보기. 미지정 시 enabled=true인 모든 바스켓 실행 | `run_rebalance()` → BasketRebalancer |

**Paper schedule 운영 원칙**:
- `python main.py --mode schedule --strategy scoring`은 기본 signal-only이며, 신호/evidence/finalize를 수집하되 신규 주문은 내지 않는다.
- `QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring`만 full paper로 동작한다. YAML 원본은 유지하고 resolved hash가 달라지는 방식으로 실험 drift를 추적한다.
- live 모드는 `QUANT_AUTO_ENTRY`로 열리지 않는다. live 진입은 `ENABLE_LIVE_TRADING=true`, `--confirm-live`, 전략 상태, 5개 hard gate를 모두 통과해야 한다.

**기타 CLI 옵션**:
- `--update-holidays` → 휴장일 YAML 갱신 후 종료
- `--allow-lookahead` → strict-lookahead 해제 (경고 출력, 권장하지 않음)
- `--include-weights` → optimize 모드에서 스코어링 가중치도 탐색
- `--auto-correlation` → optimize 전 상관 분석 자동 실행, 고상관 지표 자동 비활성화
- `--disable-weights w_rsi,w_ma` → 특정 가중치 키를 0으로 고정
- `--walk-forward` → validate 모드에서 슬라이딩 윈도우 워크포워드 검증
- `--no-benchmark-top50` → validate 모드에서 코스피 Top50 벤치마크 비활성화
- `--confirm-live` → live 모드 진입 시 필수 확인 플래그
- `--force-live` → hard gate 우회 (위험: 검증 미통과 상태에서 live 강제 진입)
- `--basket <name>` → rebalance 모드에서 대상 바스켓 지정 (미지정 시 enabled=true 전체)
- `--dry-run` → rebalance 모드에서 실제 주문 없이 계획만 출력
- `--initial-capital` → backtest / backtest_momentum_top 초기 자본금
- `--top-n` → backtest_momentum_top 상위 N종목 (기본 watchlist.top_n)
- `--rebalance-days` → backtest_momentum_top 리밸런싱 주기 (기본 20)
- `--market-filter` → backtest_momentum_top 시장 국면 필터 적용
- `--cash-buffer` → backtest_momentum_top 현금 보유 비율
- `--portfolio-stop` → backtest_momentum_top 포트폴리오 스탑 로스

---

## 8. 백테스팅 & 검증

### 8.1 핵심 성과 지표 (KPI)

| 지표 | 설명 | 최소 기준 | 비고 |
|------|------|-----------|------|
| 총 수익률 | 누적 수익 | 벤치마크(코스피 지수) 초과 | "연 20%" 같은 절대 목표는 비현실적. 검증 후 달성 가능한 수치로 재설정 |
| 샤프 지수 | 위험 대비 수익 | ≥ 1.0 (OOS 기준) | 1.5 이상이면 우수. **가중치 최적화 전 직관값으로는 달성 어려움** |
| **소르티노 비율** | 하방 변동성 대비 수익 (손실 분산만 위험으로 간주) | 전략별 상이 | 백테스트 리포트에 포함 (v3.0) |
| MDD | 고점 대비 최대 하락 | < 20% | 15% 이내 권장 |
| **MDD 회복 기간** | 고점 대비 최저점까지 갔다가 **다시 고점을 회복**하기까지의 일수 | 짧을수록 유리 | 연속 악재·롱온리 전략 진단에 유용 (v3.0) |
| **VaR / CVaR** | 일별 수익 분포 기준 꼬리 리스크 (예: 95% VaR, CVaR) | — | 손실 분포의 **극단 꼬리** 관점 (v3.0) |
| **최대 연속 손실 일수** | 연속으로 손실이 난 거래일 수 | — | 연속 악재 구간 노출도 파악 (v3.0) |
| 승률 | 수익 거래 비율 | 전략별 상이 | 추세 추종은 40% 이하 가능 (손익비로 보완) |
| 손익비 (Profit Factor) | 평균 수익/평균 손실 | > 1.5 (추세 추종 ≥ 2.0) | < 1.0이면 순손실 구조 |
| 칼마 비율 | 연 수익률/MDD | > 1.0 | — |
| 평균 보유 기간 | 매수→매도 일수 평균 | 전략별 상이 | **3일 미만이면 과매매 의심** → §5.13, §5.14 참고 |
| 총 수수료 | 전체 거래 수수료 합계 | 총 수익의 30% 미만 | **50% 초과 시 전략 수익의 절반 이상이 수수료로 소멸** |
| 연간 왕복 횟수 | 종목당 매수→매도 왕복 | — | **종목당 월 5회 초과 시 과매매 경고** (왕복 비용 1.15%/월) |

### 8.2 검증 절차

- 과거 3~5년 데이터 → 훈련/검증 분리 → 파라미터 최적화 → OOS 검증 → 거래비용·슬리피지 반영 → 페이퍼 트레이딩 → 소액 실전.
- **벤치마크 비교**: 코스피 지수(KS11) 대비 초과 수익 여부에 더해, **코스피 상위 50종목 동일비중 매수·홀딩** 대비 out-of-sample 초과 수익 여부를 검증합니다. Top50 벤치마크는 `--mode validate` 시 기본 사용하며, `--no-benchmark-top50` 으로 비활성화할 수 있습니다. 벤치마크·유니버스 종목 리스트는 **검증 시작일(as_of_date)** 기준으로 가져오며, `risk_params.backtest_universe` 설정에 따라 **생존자 편향**을 완화할 수 있습니다(아래 §8.2.1 참고).
- **Research-only target-weight top-N 검증**: `tools/research_candidate_sweep.py --candidate-family target_weight_rotation`은 live/paper 전략 등록 없이 월간 직전 거래일 점수 기준 top-N을 목표비중으로 보유/교체합니다. 당일 종가 급등을 당일 랭킹에 쓰지 않고, delta 리밸런싱 비용과 일별 cash/value/n_positions를 기록해 평균 노출과 exposure-matched benchmark excess를 함께 봅니다. `min_score_floor_pct`를 주면 benchmark 대비 초과 모멘텀이 약한 슬롯은 현금으로 남기고, `hold_rank_buffer`를 주면 기존 보유 종목이 top-N 밖으로 소폭 밀려도 버퍼 안에서는 유지해 과도한 교체를 줄입니다. `market_exposure_mode=benchmark_risk`는 직전 거래일까지의 KS11 SMA/rolling drawdown/realized volatility만 사용해 risk-off 리밸런싱의 부분 노출 축소와 `risk_off_rebalance_pct`를 기록합니다.
- **Target-weight paper/pilot 실행**: `tools/target_weight_rotation_pilot.py`는 canonical candidate id의 params를 읽어 동일 로직으로 목표 수량을 계산하고, `core.paper_pilot.check_pilot_entry()`와 plan-level cap 검증을 통과해야 paper 주문을 낼 수 있습니다. 추가로 주문별 notional이 최근 20일 평균 거래대금의 기본 5%(`--max-order-adv-pct`)를 넘으면 readiness audit과 실행을 차단합니다. 기본은 dry-run이며 `--execute`를 줘도 `trading.mode=live`에서는 거부합니다.

**⚠️ 생존자 편향 (Survivorship Bias) — §8.2.1**

- **문제**: 현재 상장 종목만으로 백테스트/벤치마크를 구성하면, 기간 중 **상장폐지·관리종목**이 제외되어 수익률이 **과대평가**될 수 있습니다. 코스닥 소형주·top_market_cap·momentum_top 등 자동 선정 모드에서 특히 치명적입니다. 실전에서는 그 망한 종목에도 투자했을 것이므로, 살아남은 종목만의 성과는 허구일 수 있습니다.
- **대응**:
  1. **관리종목 제외**: `risk_params.backtest_universe.exclude_administrative: true`(기본)로 FDR `KRX-ADMINISTRATIVE` 목록을 제외합니다.
  2. **과거 시점 전체 종목 유니버스 (권장)**: `backtest_universe.mode: historical`로 설정하면 백테스트 시작일(as_of_date) 기준으로 **당시 상장되어 있던 KOSPI+KOSDAQ 전체 종목**을 pykrx `get_market_ticker_list(date)` 로 가져옵니다. 상장폐지된 종목도 포함되어 **생존자 편향을 실질적으로 제거**합니다. `WatchlistManager`에 `as_of_date`가 주어지고 mode가 `current`이면 자동으로 `historical`로 전환됩니다.
  3. **코스피200 유니버스**: `backtest_universe.mode: kospi200` 으로 설정하면 해당 일자 **코스피200 구성종목**(pykrx)만 사용합니다. 대형주 위주라 상장폐지가 적어 편향을 줄일 수 있습니다.
  4. **시점 기준 벤치마크**: 전략 검증(`--mode validate`) 시 **검증 시작일(as_of_date)** 기준으로 종목 리스트를 가져와 Top50/벤치마크를 구성합니다.
- **설정**: `config/risk_params.yaml` → `backtest_universe.mode` (`current` | `historical` | `kospi200`), `exclude_administrative` (true 권장). historical/kospi200 사용 시 pykrx 설치 필요.
- **경고**: `mode: current` 상태로 백테스트를 실행하면 콘솔에 생존자 편향 경고가 출력됩니다.

> **🚨 즉시 확인 필요**: `backtest_universe.mode`가 `historical`로 설정되어 있는지 지금 확인하세요. `current` 상태라면 백테스트 수익률이 수십 %p 과대평가되어 있을 수 있습니다. 변경 후 반드시 백테스트를 다시 실행하여 실제 기대 수익률을 재확인해야 합니다. 백테스트에서 연 20% 수익이었는데 실전에서 손실이 나는 원인 중 하나가 바로 이 생존자 편향입니다.

**⚠️ 검증 방법 자체의 한계**

- **기준이 통과해도 실전 수익이 안 날 수 있음**: `--mode validate` 조건(샤프 ≥ 1.0, MDD 기준, 벤치마크 초과 수익)을 만족해도, 아래 상황에서는 **실전에서 손실**이 날 수 있습니다.
  1. **검증 기간(3~5년)이 해당 전략에 유리한 시장 국면이었던 경우**: 그 기간이 우연히 상승장·특정 변동성 구간이었다면, 검증 통과는 **국면 편향**일 수 있습니다. 이후 국면이 바뀌면 성과가 반전될 수 있습니다.
  2. **파라미터 최적화 후 검증한 경우**: 학습 구간에서 최적화한 뒤 OOS로 검증해도, **OOS 구간이 같은 시대(같은 시장 환경)** 이면 OOS에서도 성과가 높게 나오도록 **간접적으로 과적합**되었을 수 있습니다. 진정한 "미래" 구간이 아니므로 실전 이탈 가능성이 남습니다.
- **권장**: 검증 통과를 **필요 조건**으로 두되 **충분 조건으로 해석하지 말 것**. 가능하면 **여러 시장 국면(상승·하락·횡보)** 이 포함된 기간으로 검증하거나, **walk-forward**·롤링 검증을 고려하고, 실전은 **소액·보수적**으로 시작하는 것을 권장합니다.

**손익비(Profit Factor) 자동 경고**

- 설계서에서 추세 추종 전략은 **손익비 ≥ 2.0**을 검증하라고 명시하고 있습니다. `StrategyValidator`가 검증 완료 시 자동으로 확인합니다.
- **추세 추종(`trend_following`)**: FULL 또는 OOS 기간 `profit_factor < 2.0`이면 `WARN: 추세 추종 전략 손익비 미달` 경고 발생.
- **기타 전략**: `profit_factor < 1.0`이면 순손실 구조 경고 발생.
- **워크포워드 검증**: 각 테스트 창별로 손익비가 기준 미달 시 창별 경고 발생.
- 경고는 (1) 콘솔 로그(`loguru.warning`), (2) 리포트 텍스트 파일 하단, (3) **디스코드 알림**으로 자동 전송됩니다.
- 리포트에는 `손익비(Profit Factor): FULL X.XX | OOS X.XX` 행과 `⚠️ 경고` 섹션이 표시됩니다.

**워크포워드(Walk-Forward) 검증**

- **기본 검증**: `--mode validate` (옵션 없음)는 전체 구간을 **한 번만** train(기본 70%) / test(30%) 로 나눕니다.
- **워크포워드 검증**: `--mode validate --walk-forward` 로 **슬라이딩 윈도우** 반복 검증을 수행합니다. `strategy_validator.run_walk_forward()`: train 2년(504일) → test 1년(252일), 1년(252일) 스텝으로 슬라이드해 여러 구간에서 테스트합니다. 예: 2019~2020 훈련 → 2021 테스트, 2020~2021 훈련 → 2022 테스트, … 각 테스트 구간에서 샤프·MDD 기준 통과 여부를 보고, **전체 통과** 또는 **80% 이상 창 통과** 시 검증 성공으로 볼 수 있습니다. 리포트는 `reports/validation_walkforward_*.txt` 에 저장됩니다.
- **권장**: 검증 신뢰도를 높이려면 **워크포워드** (`--walk-forward`) 를 사용하고, 대부분의 창에서 통과하는지 확인하세요.

### 8.3 거래 비용 반영

- 수수료 0.015%, 증권거래세+농특세 0.20%(매도, 2026년~ 코스피·코스닥 동일), 슬리피지(기본 0.05%, 거래량 기반 동적 배수). `risk_params.yaml` → `transaction_costs`.

**⚠️ 거래 빈도와 수수료의 관계 (공통 문제)**

- **왕복 비용**: 매수·매도 합쳐 **약 0.23%**(수수료 0.015%×2 + 증권거래세 0.20%) 수준입니다(2026년 기준). 이를 상회하려면 **매 거래마다 평균 0.23% 이상의 초과 수익**이 나와야 합니다. 일봉 기반 전략에서 매번 달성하기는 쉽지 않습니다.
- **10분마다 신호 확인**: 실전 스케줄러는 장중 **10분 간격**으로 신호를 확인하고 매매를 실행합니다. 신호가 자주 바뀌는 전략은 **과매매(Over-trading)** 가 되어, 수수료만 나가는 상황이 될 수 있습니다.
- **스코어링 전략**: 임계값 근처에서 신호가 BUY ↔ HOLD ↔ SELL 로 자주 바뀌기 쉽습니다. 백테스트에서 **거래 횟수·연간 왕복 수**를 확인하고, 수수료를 감안한 후 **순수익이 양수**인지 반드시 점검하세요. 필요 시 임계값을 완화해 진입/청산 빈도를 낮추는 것을 고려하세요.
- **권장**: 전략별로 "거래 1회당 기대 초과 수익 > 왕복 비용"이 성립하도록 **진입/청산 조건을 보수적으로** 두거나, **최소 보유 기간(§5.14)·신호 히스터리시스(§5.13)** 등을 도입해 불필요한 왕복을 줄이는 설계를 권장합니다.

**과매매 시나리오 — 구체적 수치 예시**:

| 조건 | 수치 |
|------|------|
| 보유 종목 수 | 10개 |
| 종목당 월 왕복 횟수 | 10회 |
| 왕복 비용 | 0.23% |
| 월 총 수수료 비용 | 10종목 × 10회 × 0.23% = **23%** (투자금 대비) |
| 월 기대 수익 | 전략이 월 2% 수익을 낸다 해도 수수료 23%에 의해 **-21% 순손실** |

위 시나리오는 극단적이지만, **직관값 가중치 + 10분 간격 신호 확인** 조합에서 임계값 근처 종목이 많으면 히스터리시스가 활성화되어 있더라도 발생할 수 있습니다. 백테스트 리포트의 "과매매 분석" 항목(평균 보유 기간, 총 수수료, 연간 왕복 수)을 반드시 확인하세요.

### 8.4 Paper → Live 전환 준비 자동 평가

현재 "1~2개월 paper 후 실전" 전환은 수동 판단에 의존합니다. `paper_compare.check_live_readiness()`가 이를 자동화합니다.

**평가 기준** (`risk_params.yaml` → `paper_backtest_compare.live_readiness`):

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `min_direction_agreement_pct` | 70 | paper와 backtest 일별 수익률 방향성 일치율 ≥ 70% |
| `max_return_diff_pct` | 5 | 누적 수익률 차이 ≤ 5%p |
| `min_trading_days` | 20 | 최소 평가 거래일수 (약 1개월) |
| `min_trades` | 5 | 최소 매도 거래 건수 |
| `notify_on_ready` | true | 준비 완료 시 디스코드 알림 |
| `auto_check_in_scheduler` | true | paper 모드 장마감 시 자동 체크 (최근 30일) |

**방향성 일치율**: 동일 날짜의 paper 포트폴리오 일별 수익률과 backtest equity 일별 수익률이 같은 방향(둘 다 +, 둘 다 -)인 비율. 70% 이상이면 전략 실행 로직이 백테스트와 충분히 일치한다고 판단합니다.

**동작 방식**:
1. **수동**: `--mode compare` 실행 시 divergence 비교 후 자동으로 readiness 평가도 수행. 결과를 콘솔에 출력하고, 준비 완료 시 디스코드 Embed 알림 전송.
2. **자동**: paper 모드 Scheduler의 장마감(`_run_post_market`) 시 `_check_live_readiness()`가 최근 30일 기준으로 자동 평가. 준비 완료 시 디스코드 알림.
3. 모든 기준이 충족되면 `"✅ 실전 전환 준비 완료"` 신호가 발생하며, 미달 시 어떤 기준이 부족한지 상세 사유를 제공합니다.

**주의**: 이 신호는 의사결정 보조 도구이며, 최종 실전 전환은 사용자가 직접 판단해야 합니다. 특히 paper 기간이 특정 시장 국면에만 해당하는 경우 실전에서 결과가 달라질 수 있습니다.

### 8.5 멀티종목 포트폴리오 백테스트 (`portfolio_backtest`) — v3.0

- **목적**: 단일 종목 백테스트(`backtest`)와 달리, **여러 종목을 동시에** 보유·매매하며 분산·최대 포지션 한도 등을 반영한 시뮬레이션.
- **실행**: `python main.py --mode portfolio_backtest --symbols 005930,000660,...` (`--start` / `--end` 등 기간 옵션은 `main.py`와 동일 패턴)
- **구현**: `backtest/portfolio_backtester.py` — `Backtester`와 별도 모듈. 리스크 파라미터의 포트폴리오·분산 관련 설정과 정합되도록 설계.
- **활용**: 유니버스 후보 종목 묶음에 대한 **동시 보유 시나리오** 검증, 단일 종목 백테스트와의 성과 비교.

### 8.6 멀티전략 2-Sleeve 포트폴리오 검증 — v4.0

- **목적**: 서로 다른 전략을 **독립 sleeve**로 운용하여 자본을 고정 비율로 분리하고, 각 sleeve의 기여를 독립 측정.
- **구조**: Sleeve A(breakout_volume) + Sleeve B(relative_strength_rotation, TS OFF, TP 7%). 전략 간 total_score 직접 비교 금지, 자본 배분만 고정.
- **검증 스크립트**: `scripts/c5_sleeve_backtest.py`, `scripts/c5_weight_sweep.py`, `scripts/c5_sleeve_sweep_nots.py`, `scripts/c5_rolling_walkforward.py`
- **Exit 최적화**: Rotation trailing stop 제거 + TP 8%→7% per-strategy override. capture rate 71%→79%(DEV), 78%→83%(OOS).
- **Entry filter 탐색**: KS11 SMA200, abs momentum, min_hold_days → 모두 불채택 (NO_MEANINGFUL_IMPROVEMENT / ADVERSE_EFFECT).
- **결과**: §4.4c 참고. BV50/R50 OOS 2.87%, MDD -1.71%. Rolling WF 60% positive window, median +0.45%.
- **판정**: PAPER_READY_WITH_GUARDRAILS.
- **Guardrails**: monthly -3% warn, MDD -5% warn, MDD -8% halt, 3-month consecutive loss → downgrade.
- **Paper monitoring**: `scripts/c5_paper_monthly_report.py`, signal/executed/skipped 카운터.

---

## 9. 예외 처리 및 안정성

- **API**: Circuit Breaker (`api/circuit_breaker.py`), 지수 백오프+지터(thundering-herd 방지) 재시도, SSL/커넥션 에러 전용 핸들러, 토큰 만료 시 60초 쿨다운+알림.
- **주문 실패 Dead-letter**: 모든 재시도 소진 후 `FailedOrder` 테이블에 영구 저장 (`save_failed_order`). `get_pending_failed_orders()`로 미처리 건 조회, `resolve_failed_order()`로 재처리 상태 관리.
- **웹소켓**: 자동 재연결·Heartbeat (구현 시).
- **데이터**: `core/data_validator.py` 로 Null/NaN/음수 주가 필터링.
- **알림 이중화**: `core/notifier.py` — 1차 디스코드 → 2차 텔레그램 Bot API → 3차 이메일(SMTP). `critical=True` 이벤트(블랙스완, 서킷브레이커)는 **가용한 모든 채널에 동시 발송**. 디스코드 웹훅 장애 시에도 텔레그램 또는 이메일로 알림 수신 보장.
- **비밀**: `.env` + `os.environ`, 설정 파일에 하드코딩 금지.
- **주문**: OrderGuard(TTL)·KIS 미체결 조회로 중복 주문 방지; 루프 10분 초과 시 다음 사이클 스킵.

**⚠️ 알림 이중화 — 디스코드 장애 대비**

- **문제**: 디스코드 웹훅은 무료이지만 가끔 장애가 발생합니다. 블랙스완이 발생했는데 알림을 못 받으면 치명적입니다.
- **대응**: `core/notifier.py`의 `Notifier` 클래스가 모든 알림을 관리합니다. `Scheduler`, `CircuitBreaker`, `main.py` 등 주요 모듈은 `DiscordBot` 대신 `Notifier`를 사용합니다.
  - **일반 알림**: 디스코드 발송 → 실패 시 텔레그램 → 실패 시 이메일 순서 fallback.
  - **치명적 알림** (`critical=True`): 블랙스완 발동, 서킷브레이커 오픈, 큰 손절(-5% 이하) 등은 디스코드·텔레그램·이메일 **모두 동시 발송**.
  - **실패 누적 감시**: 알림 실패가 5회 이상 누적되면 "알림 경로 점검 필요" 경고를 이메일로 발송.
- **설정**:
  - 텔레그램: `settings.yaml` → `telegram.enabled`, `bot_token`, `chat_id` (또는 환경변수 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
  - 이메일: 환경변수 `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`
  - 세 채널 모두 선택적이며, 설정된 채널만 사용됩니다.

**⚠️ SQLite 동시성 (실전 다중 접근)**

- **문제**: 실전 모드에서 Scheduler(장중 10분 루프), LiquidateTrigger(HTTP), web_dashboard(aiohttp 10초 폴링)가 **동시에** SQLite에 접근합니다. SQLite는 **파일 단위 write lock**이라 동시 쓰기 시 `database is locked` 오류가 발생할 수 있습니다. PositionLock(threading.RLock)은 Python 스레드 내에서만 보호하며, aiohttp는 별도 이벤트 루프에서 동작할 수 있어 보호 범위 밖입니다.
- **대응 (7단계 방어)**:
  1. **WAL 모드**: `PRAGMA journal_mode=WAL` — 읽기와 쓰기가 동시에 가능. `PRAGMA synchronous=NORMAL` — WAL에서 안전하면서 쓰기 성능 향상.
  2. **busy_timeout=30s**: 다른 연결이 write lock을 잡고 있으면 최대 30초 대기 후 예외.
  3. **scoped_session**: SQLAlchemy `scoped_session`으로 **스레드별 독립 세션** 보장. Scheduler·aiohttp·LiquidateTrigger가 각각 자기 세션을 받아 세션 충돌 방지.
  4. **`@with_retry` 데코레이터**: **읽기·쓰기 전체 함수**에 적용. busy_timeout 초과 시 **최대 3회 지수 백오프 재시도** (1초→2초→4초). WAL 체크포인트 중 일시적 locked에도 읽기 함수가 안전.
  5. **커넥션 풀**: `check_same_thread=False` + `pool_pre_ping=True`로 커넥션 상태를 재사용 전 확인.
  6. **컨텍스트 매니저**: `db_session()` 제공 — `with db_session() as session:` 으로 commit/rollback/close 자동 처리.
  7. **안전 백업**: `backup.py`가 **SQLite Online Backup API** (`sqlite3.Connection.backup()`)를 사용해 WAL 모드에서도 일관된 스냅샷을 보장하는 백업 수행. 실패 시 `-wal`/`-shm` 파일을 포함한 `shutil.copy2` 폴백.
- **초기화 검증**: `init_database()` 시 WAL 모드 활성화 여부를 검증하고, **WAL이 아니면 ERROR 로그**를 출력합니다 (네트워크 드라이브 등에서 WAL이 지원되지 않을 수 있음).
- **중기 검토**: 실전 운영이 안정화되면 **PostgreSQL** 전환 권장. `settings.yaml` → `database.type: "postgresql"` + `postgresql:` 섹션 주석 해제만으로 전환 가능 (SQLAlchemy ORM 동일, `pool_size=5`, `pool_pre_ping=True`).

**⚠️ KIS API 요청 한도 (Rate Limit)**

- **문제**: KIS API는 **초당/분당/일당** 요청 수 제한이 있습니다(예: 초당 20건). momentum_top·kospi200 모드로 20~50종목을 관리할 경우, 장중 10분마다 **종목별 데이터 수집 + 포지션 조회 + 잔고 조회** 등을 한꺼번에 실행하면 한도를 초과할 수 있습니다. 한도 초과로 API 키가 일시 차단되면 Circuit Breaker가 열려 그 시간 동안 모든 주문이 불가능해집니다.
- **대응 (이중 Rate Limiter + 모니터링)**:
  1. **Token Bucket (초당)**: `_wait_for_token()`으로 초당 허용 건수(`max_calls_per_sec`, 기본 10)를 넘지 않도록 버스트 제어.
  2. **슬라이딩 윈도우 (분당)**: `_wait_for_minute_window()`로 최근 60초 내 요청 수가 `max_calls_per_min` (기본 300)을 초과하면 가장 오래된 요청이 윈도우를 벗어날 때까지 대기. Token Bucket만으로는 분당 한도 위반 가능(10건/초 × 60초 = 600건 > 분당 한도).
  3. **429 재시도**: `Retry-After` 헤더만큼 대기 후 자동 재시도. 429 누적 횟수 추적.
  4. **사용량 모니터링**: `get_rate_limit_stats()` — 최근 60초 요청 수, 분당 활용률(%), 누적 요청·429 횟수, 평균 초당 요청.
  5. **Scheduler 사전 예측**: 장전/장중 분석 시작 전 `종목 수 × 2(예상 요청)`을 계산하여 예상 소요 시간과 분당 한도 초과 여부를 로그. 분석 후 실제 사용량 출력.
- **설정**: `settings.yaml` → `kis_api.max_calls_per_sec` (기본 10), `kis_api.max_calls_per_min` (기본 300). 환경변수 `MAX_CALLS_PER_SEC`, `MAX_CALLS_PER_MIN`으로 덮어쓰기 가능.
- **종목 수가 많을 때**: 초당 10건이면 50종목은 약 5~10초, 200종목은 약 20~40초에 걸쳐 자동 분산. 분당 한도 300건 초과 시 자동 대기 발생 후 계속 진행.

### 9.1 운영 안정성 개선 필요 사항

아래는 운영 안정성 관련 구현 현황입니다.

**✅ 시스템 헬스체크 자동화 — 구현 완료**

- 10분 주기로 DB 연결, 디스크 여유 공간, KIS API 토큰 상태, 메모리 사용률을 자동 점검합니다.
- 이상 발견 시 Discord 알림(critical)을 발송합니다.
- **구현 위치**: `core/scheduler.py` → `_maybe_run_healthcheck()`, `_run_healthcheck()`

**✅ 포지션 불일치 자동 보정 — 구현 완료**

- KIS↔DB 크로스체크에서 불일치 감지 시 KIS 실잔고를 정본으로 DB를 자동 동기화합니다.
- KIS에만 있는 포지션 → DB 추가, DB에만 있는 포지션 → DB 삭제, 수량 불일치 → KIS 기준으로 수정.
- 보정 내역 로깅 + critical 알림 발송.
- **설정**: `settings.yaml` → `trading.position_mismatch_auto_correct: false` (기본 비활성, 활성화 시 true)
- **구현 위치**: `core/portfolio_manager.py` → `sync_with_broker(auto_correct=True)`, `_auto_correct_positions()`

**✅ 휴장일 자동 갱신 — 구현 완료**

- Scheduler 메인 루프에서 날짜가 바뀔 때 holidays.yaml의 수정일을 확인합니다.
- 90일 이상 경과 또는 새해(1/1~1/7)에 `holidays_updater`를 자동 호출하여 갱신합니다.
- **구현 위치**: `core/scheduler.py` → `_maybe_update_holidays()`

**⚠️ WebSocket 재연결 시 데이터 갭 — 잠재적 위험 (부분 완화 / 미완)**

- **문제**: KIS 웹소켓이 끊겼다가 재연결될 때 그 사이의 **틱·호가 스트림** 갭 처리가 명확하지 않습니다.
- **v3.0 부분 완화**: 장중 스케줄러는 **REST 기반**으로 주기적으로 시세·포지션을 갱신하며, `risk_params.gap_risk`에 따라 **전일 대비 갭다운**이 임계값 이하이면 즉시 청산을 시도합니다(웹소켓과 별개 경로). 다만 **갭 구간 내부의 초단기 급변**을 웹소켓 없이 포착하는 것은 여전히 한계가 있습니다.
- **부분 구현**: `api/websocket_handler.py`가 갭 보충 후 변동폭을 `BlackSwanDetector.report_websocket_gap_volatility()`에 넘기며, **관측 변동 ≥ 5%** 시 쿨다운을 발동할 수 있음(로그·운영자 점검 위주).
- **남은 과제**: (1) 갭 구간 REST 보충의 **전 종목·전 구간** 커버리지, (2) 갭 중 급변과 **긴급 매도** 정책의 일원화, (3) 실전에서 웹소켓+스케줄러 **이중 경로** 테스트·모니터링 강화.
- **우선순위**: **중기 개선 (3~6개월 내)** (표 §10의 WebSocket 갭 처리 항목과 동일 계열)

**✅ 10분 루프 모니터링 — 구현 완료**

- `LoopMetrics` 클래스가 실행 횟수, 스킵 횟수, 연속 스킵 횟수, 최대 소요 시간을 추적합니다.
- 연속 스킵 3회 이상 시 Discord 경고, 6회마다 지표 로깅, 장마감 리포트에 일일 지표 포함.
- **구현 위치**: `core/scheduler.py` → `LoopMetrics` 클래스

**⚠️ 웹 대시보드 강화 — 현재 기본 수준**

- **문제**: 현재 포트폴리오 요약과 스냅샷만 보여줍니다.
- **필요 사항**: 전략별 신호 발생 현황, 오늘 실행된 주문 목록, 현재 시장 국면 상태, KIS API 사용량(rate limit 잔여), 블랙스완 감지 상태, 루프 실행 시간 추이 등을 실시간으로 표시.
- **우선순위**: **중기 개선 (3~6개월 내)**

---

## 10. 개발 로드맵 & 우선순위별 액션 아이템

### 현재 구현 완료 (인프라)

- [x] Python 프로젝트 구조, Config(YAML+.env), SQLite·SQLAlchemy
- [x] KIS API 인증·시세·주문·잔고, 웹소켓 핸들러, Circuit Breaker, 이중 Rate Limiter + 사용량 모니터링
- [x] DataCollector, WatchlistManager (6가지 모드), IndicatorEngine (8개 지표)
- [x] SignalGenerator, **CLI 등록 전략** scoring / mean_reversion / trend_following / fundamental_factor / **momentum_factor** / ensemble. 변동성 전략 클래스는 **앙상블 내부**에서만 사용
- [x] RiskManager (포지션 사이징, 분산, 성과 열화, 손절/익절/트레일링, 거래 비용)
- [x] Backtester (strict-lookahead, 수수료·세금·동적 슬리피지), StrategyValidator, ParamOptimizer
- [x] OrderExecutor (paper/live), PositionLock, OrderGuard, PortfolioManager, Scheduler
- [x] BlackSwanDetector(**risk_params.blackswan**), MarketRegime(3중 신호·**get_regime_adjusted_params**), EarningsFilter, FundamentalLoader
- [x] 통합 알림 이중화(Notifier), 웹 대시보드, LiquidateTrigger, DB 백업
- [x] 워크포워드 검증, 벤치마크(KS11 + Top50), 과매매 분석
- [x] test_integration.py, pytest 테스트 suite (`tests/` 기준 다수 파일, 미국 시장·스케줄 등 포함)

> **평가**: 인프라는 프로덕션 수준에 가깝습니다. 그러나 **신호 품질이 검증되지 않은 상태**이므로, 아래 액션 아이템을 순서대로 진행해야 합니다.

### 즉시 확인해야 할 사항 (실전 투입 전 필수)

> 아래 4가지를 모두 완료하기 전까지는 실전 투입을 하지 마세요.

| # | 액션 | 상세 | 참고 |
|---|------|------|------|
| 1 | **backtest_universe.mode 확인** | `historical`로 설정 후 백테스트 재실행. `current` 상태라면 수익률이 수십 %p 과대평가 | §8.2.1 |
| 2 | **데이터 소스 고정** | `data_source.preferred: fdr`, `allow_kis_fallback: false` 설정. paper 모드 중 소스 불일치 경고 미발생 확인 | §2.2 |
| 3 | **가중치 최적화 파이프라인 완료** | `check_correlation` → `optimize --include-weights --auto-correlation` → `validate --walk-forward` 3단계 실행. OOS 샤프 ≥ 1.0 달성한 가중치로 strategies.yaml 업데이트. **현재 직관값 가중치 상태로는 실전 투입 금지** | §4.1 |
| 4 | **paper 모드 1개월 운영** | 실제 시세로 paper 모드 최소 1개월 → `check_live_readiness` 통과 (방향성 일치율 ≥ 70%, 수익률 차이 ≤ 5%p) | §8.4 |

### 최근 구현 완료 (과매매 방지 + 운영 안정성)

- [x] 포지션 불일치 자동 보정 — KIS↔DB 불일치 시 KIS 기준 DB 자동 동기화 (§9.1)
- [x] 신호 히스터리시스 — BUY↔HOLD↔SELL 순차 전환 강제, 직접 전환 차단 (§5.13)
- [x] 최소 보유 기간 5일 — 매수 후 5일 미만 매도 차단, 손절·블랙스완 예외 (§5.14, 3→5일 강화)
- [x] 휴장일 자동 갱신 — Scheduler에서 90일 경과 또는 연초 자동 호출 (§9.1)
- [x] 시스템 헬스체크 자동화 — 10분 주기 DB·API·디스크·메모리 점검 (§9.1)
- [x] 10분 루프 모니터링 — LoopMetrics 추적, 연속 스킵 경고, 장마감 리포트 포함 (§9.1)
- [x] MACD 3단계 점수 체계 — 크로스 당일(풀점수) + 유지 중(반점수) + 히스토그램 보너스
- [x] 백테스터에 min/max_holding_days 및 당일 손절 후 재매수 방지 반영
- [x] 리스크 파라미터 현실화 — 트레일링 스톱 3→5%, 익절 6/10→4/8%, 슬리피지 틱 2→1
- [x] KIS 호출 제어 강화 — 지수 백오프+지터, SSL/커넥션 에러 전용 핸들러, 토큰 오류 쿨다운 (§9.1)
- [x] 주문 실패 Dead-letter 큐 — FailedOrder 테이블에 실패 주문 영구 저장, 재처리 지원 (§9.1)
- [x] 전략 등록 레지스트리(플러그인형) — `strategies/__init__.py`에서 `create_strategy(name)` 호출로 전략 동적 로딩 (§4.5)
- [x] 바스켓 포트폴리오 리밸런싱 — `BasketRebalancer`로 종목별 목표 비중 관리, 드리프트/주기 기반 리밸런싱, 신호 가중 모드 지원. `--mode rebalance --basket <name>` CLI 및 스케줄러 장전 단계 자동 통합 (§10)
- [x] **`--mode schedule`** — 모의 매매 전용 무한 스케줄 루프, `core/runtime_lock.py`로 단일 인스턴스 락
- [x] **미국 티커·장시간** — `DataCollector.fetch_stock` 미국 분기, `config/us_holidays.yaml`, `TradingHours` NYSE 구간
- [x] **DART(선택)** — `dart_loader` + `earnings_filter` 폴백, `DART_API_KEY` / `settings.dart`
- [x] **펀더멘털 전략·앙상블 4구성** — `FundamentalFactorStrategy`, `ensemble.components`에 `fundamental_factor` 포함 가능
- [x] **`momentum_factor` CLI 등록** — `--strategy momentum_factor`로 단독 사용 가능 (앙상블 구성도 유지)
- [x] **다종목 모멘텀 포트폴리오 백테스트** — `--mode backtest_momentum_top` 추가. `backtest/momentum_top_portfolio.py`에서 리밸런싱·시장 국면·포트폴리오 스탑 지원
- [x] **전략 진단 보조** — `core/strategy_diagnostics.py` (`DiagnosticLine`), 전략별 신호·점수 진단 라인 생성
- [x] **대시보드 런타임 상태** — `monitoring/dashboard_runtime_state.py`, 스케줄러·전략 실행 현황 실시간 상태 전달

### v3.0 구현 완료 (리스크·백테스트·운영)

- [x] **블랙스완 임계값·쿨다운·recovery** — `config/risk_params.yaml` → `blackswan` (코드 하드코딩 제거)
- [x] **갭 리스크** — `gap_risk`: 스케줄러 갭다운 즉시 청산, OrderExecutor 갭업 추격 매수 차단
- [x] **국면 적응형 손절·익절** — `strategies.yaml` → `regime_adaptive` + `market_regime.get_regime_adjusted_params`
- [x] **스케줄러 장중** — 동적 손절 래칟 갱신(`update_stop_loss_price`), `auto_entry` 시 신호 재스캔
- [x] **CLI** — `--mode portfolio_backtest`, `--symbols`
- [x] **백테스트 메트릭 확장** — 소르티노, VaR/CVaR, MDD 회복일, 최대 연속 손실일

### v4.0 구현 완료 (C-5 Rotation 최적화 + Paper 후보 확정)

- [x] **Rotation trailing stop 제거** — 승률 18-29%, negative EV → `disable_trailing_stop: true`. DEV -4.99%→-0.96%, capture rate 71%→79%
- [x] **Rotation TP 8%→7%** — per-strategy override (`strategies.yaml: take_profit_rate: 0.07`). DEV -0.96%→-0.19%, OOS 4.25%→4.71%
- [x] **per-strategy TP override** — `portfolio_backtester.py`에 `tp_rate_override` 파라미터 추가
- [x] **min_hold_days 인프라** — 코드 추가, 테스트 후 ADVERSE_EFFECT → 0 유지
- [x] **KS11 SMA200 시장 필터** — 코드 추가(`market_filter_sma200`), 테스트 후 NO_MEANINGFUL_IMPROVEMENT → false 유지
- [x] **절대 모멘텀 필터** — 코드 추가(`abs_momentum_filter`), 테스트 후 NO_MEANINGFUL_IMPROVEMENT → none 유지
- [x] **signal/executed/skipped 카운터** — `portfolio_backtester.py`에 모니터링 카운터 추가
- [x] **Rolling walk-forward** — 10 windows × 12mo, 6mo step. BV50/R50 positive 60%, median +0.45%
- [x] **BV50/R50 paper 후보 확정** — PAPER_READY_WITH_GUARDRAILS. guardrail 설정 완료
- [x] **Paper 월간 리포트** — `scripts/c5_paper_monthly_report.py`

### v4.1 Paper 가동 (BV50/R50 Paper Trading 개시)

- [x] **BV50/R50 Paper Trading 개시** — 2026-04-01 시작. 목표 60영업일
- [x] **Frozen manifest** — BV50/R50, Rotation TP=7%, TS=OFF. 파라미터 동결
- [x] **일간 운영 로그** — `paper_log.txt`에 Day별 delta 기록 (평가금액, 신호/체결/스킵, fill rate, 경고 판정)
- [x] **breakout_volume 상태 승격** — `experimental` → `paper_candidate` (BV50/R50 composite paper의 Sleeve A)
- [x] **Day 2 (2026-04-02)** — 합산 10,082,023원(+0.82%), 무신호 보유일, NORMAL 판정

### v5.0 운영 안정성 + 전략 재평가 (코드 감사 대응)

- [x] **`--force-live` 제거** — canonical bundle + paper evidence hard gate 우회 불가
- [x] **OrderGuard 수정** — mark_pending을 API 호출 이전으로, 체결/실패 후 clear() 호출
- [x] **sync_with_broker PositionLock** — 동기화 중 position 동시 접근 방지
- [x] **signal_at/order_at/price_gap/peak_value 마이그레이션** — 기존 DB 자동 컬럼 추가
- [x] **벤치마크 거래비용 반영** — `_buy_and_hold_metrics`에 commission/tax/slippage 적용
- [x] **WF 0-windows 수정** — validator flat key 구조 확인, 6 windows 정상 생성
- [x] **주문 상태기계** — `core/order_state.py` (OrderStatus 9개 상태, OrderBook, OrderRecord)
- [x] **OrderExecutor 이관** — 상태기계 기반 주문 처리, FILLED assert 후에만 DB 반영
- [x] **OrderRecord DB 테이블** — `database/models.py`에 order_records 추가
- [x] **debiased 전략 평가** — 거래대금 기반 ex-ante proxy 20종목, portfolio WF 6 windows
- [x] **승격 규칙 v3** — `core/promotion_engine.py` metrics 기반 자동 판정 + artifact-driven
- [x] **Paper Evidence 체계** — `core/paper_evidence.py` 일별 22개 지표, 6 anomaly rule, 9 approval gate
- [x] **전략 분류 확정** — rotation: provisional, scoring: paper_only. BV/MR/TF/ensemble: disabled (research_only)
- [x] **2026-04-29 all-family quick sweep** — rotation/momentum/breakout 14개 후보 모두 `NO_ALPHA_CANDIDATE`; promotion 미진행
- [x] **2026-04-30 top-20 all-family quick sweep** — 20종목 후보 14개 모두 `NO_ALPHA_CANDIDATE`; best momentum도 benchmark excess/MDD 미달
- [x] **pullback 후보군 추가** — `trend_pullback` 기반 research-only 후보 4개 추가. `all` sweep에 포함
- [x] **benchmark-relative momentum 후보군 추가** — KS11 대비 초과 모멘텀과 변동성 게이트로 현재 underperformance 원인을 직접 검증
- [x] **신규 후보 5종목 smoke sweep** — benchmark_relative/pullback 모두 `NO_ALPHA_CANDIDATE`; 다음은 노출 구조와 동일 유니버스 상대강도 개선 우선
- [x] **risk-budget 후보군 추가** — 후보별 diversification budget을 artifact에 남기고 집중형/균형형/방어형 exposure 비교 가능
- [x] **risk-budget smoke sweep** — MDD 개선은 확인했지만 benchmark excess 실패. 다음은 상대강도/현금 전환/부분 헤지 설계
- [x] **cash-switch 후보군 추가** — KS11 이동평균 하회 시 보유 포지션을 현금화하는 rotation 변형 3개 추가
- [x] **cash-switch smoke sweep** — MDD 방어는 일부 확인했지만 benchmark excess 실패. 다음은 benchmark-aware 랭킹/exposure-matched 검증 우선
- [x] **exposure-matched benchmark 진단 추가** — cash-switch 평균 노출 8.4~10.0%, 같은 노출 B&H 대비 excess도 음수라 단순 현금화보다 신호 edge 개선 필요
- [x] **benchmark-aware rotation 후보군 추가** — KS11 대비 상대강도 랭킹, dense entry, score-floor exit로 노출 유지형 회전 후보 검증
- [x] **benchmark-aware rotation smoke sweep** — best return +21.65%였지만 raw excess=-151.98%p라 promotion 금지. fast 후보의 exposure-matched excess +2.04%p는 top-N 목표비중 연구 힌트로만 사용
- [x] **target-weight top-N rotation 백테스터 추가** — 월간 직전일 score 기준 top-N 목표비중 보유/교체, delta 리밸런싱 비용, 노출 진단 구현
- [x] **target-weight top-N rotation smoke sweep** — best +128.44%/Sharpe 1.13/avg exposure 85.3%로 sparse 노출 병목은 해소했지만 raw excess=-45.19%p라 promotion 금지
- [x] **canonical top-20 target-weight full sweep** — best 기존 후보 +212.21%/raw excess +62.82%p/exposure-matched excess +83.66%p로 alpha 후보 확인. turnover/year 1412.1%와 paper_only 상태 때문에 `KEEP_RESEARCH_ONLY`
- [x] **target-weight score-floor 후보 추가** — `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금화. best top5 floor0 +210.21%/Sharpe 1.41/WF positive 100%였지만 turnover/year 1081.5%로 승격 금지
- [x] **target-weight rank-hysteresis 후보 추가** — `hold_rank_buffer`로 churn 완화. best top5 floor0 hold3 +278.57%/raw excess +129.18%p/Sharpe 1.65/WF positive 100%/turnover 807.8%. MDD=-28.25%라 drawdown gate는 미통과
- [x] **target-weight benchmark-risk overlay 후보 추가** — KS11 SMA/낙폭/변동성 기반 부분 노출 축소. best risk60_35 +210.24%/raw excess +60.85%p/Sharpe 1.60/PF 5.73/MDD -19.24%/turnover 858.0%/WF positive 100%로 research sweep 기준 provisional 후보 도달
- [x] **target-weight canonical bridge 추가** — `evaluate_and_promote.py --canonical`이 risk60_35를 canonical promotion bundle에 포함하고 `promotion_result.json`에서 provisional 상태 재현
- [x] **target-weight paper/pilot adapter 추가** — portfolio-level plan, pilot cap validation, paper-only exact quantity order path 추가. Live gate는 변경 없음
- [x] **target-weight liquidity preflight 추가** — 최근 20일 평균 거래대금 대비 주문 notional 기본 5% 초과 시 readiness/execute fail-closed 차단
- [x] **target-weight 비용 반영 pre-trade risk 추가** — 수수료/세금/동적 슬리피지 예상 체결가로 현금 부족과 분산/현금/투자비중 한도를 주문 전 차단하고 evidence snapshot에 기록
- [x] **테스트 298건 회귀 green** — live/paper/promotion/research sweep 회귀 묶음 기준

### v5.1 Paper Runtime 완성 (2026-04-09)

- [x] **Paper Runtime State Machine** — `core/paper_runtime.py` 5개 상태, schema quarantine, allowed_actions
- [x] **Paper Pilot Authorization** — `core/paper_pilot.py` launch readiness + pilot auth + 리스크 캡
- [x] **Paper Preflight** — `core/paper_preflight.py` 세션 전 운영 준비 상태 점검
- [x] **Strategy Universe** — `core/strategy_universe.py` paper 대상 전략 canonical 목록
- [x] **Paper Evidence E2E** — scheduler → evidence_collector → benchmark finalization → JSONL 자동 누적
- [x] **Paper 운영 도구** — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI (`tools/`)
- [x] **Notifier Health Check** — Discord webhook 설정 확인, launch readiness 연동
- [x] **Zero-return Semantics** — cash-only/no-position day deadlock 해소 (daily_return=0.0 추론)
- [x] **scoring clean_final_days=3** — infra_ready=true 달성 (pilot auth 대기)
- [x] **테스트 확장** — paper_evidence/runtime/pilot/preflight 전용 테스트 127건 추가

### v5.1 hotfix (2026-04-15 Paper scheduler 운영 안정화)

- [x] **scheduler `_run_monitoring` import regression 복구** — `database.repositories`에서 삭제된 `update_stop_loss_price`를 compat shim으로 복구(`update_position_targets(stop_loss_price=...)`에 위임). 매 사이클 ImportError로 장중 entry/exit/dynamic stop 전체가 skip되던 상태를 정상화. regression test 4건 추가(`tests/test_update_stop_loss_price_shim.py`)
- [x] **2026-04-13 / 04-14 세션 미실행 백필** — scheduler 프로세스 미기동이 root cause였음을 사후검증(dashboard_runtime_state / daily_evidence / DB row 3개 아티팩트 교차 확인). `tools/run_paper_evidence_pipeline.py --finalize --generate-package`로 두 날짜 evidence 라인 보강
- [x] **2026-04-15 `_run_post_market()` 자동 finalize 확인** — patch 반영된 스케줄러 재기동 후 15:35 훅이 스스로 `daily_evidence_scoring.jsonl`에 04-15 라인을 final로 기록(이후 수동 backfill 호출은 `Evidence already final`로 no-op 확인)

### v5.2 Freeze Pack + 운영 문서 최신화 (2026-04-29)

- [x] **GitHub 원격 브랜치 정리** — 병합 완료 브랜치 삭제, 원격은 `main` 단일 브랜치 운영
- [x] **60영업일 experiment freeze pack 병합** — `reports/experiment_freeze_pack.md`, `daily_ops_checklist.md`, `weekly_ops_checklist.md`, `experiment_stop_conditions.md`
- [x] **`QUANT_AUTO_ENTRY` 해석 단일화** — ENV > YAML > default(false), live 모드 ENV override 무시
- [x] **YAML/resolved hash 분리** — YAML 원본 동결과 실행 설정 drift를 별도 감지
- [x] **Paper manifest 충돌 해결** — 기존 scheduled run 정책과 freeze-pack metadata를 통합한 `reports/paper_experiment_manifest.json`

### 단기 개선 (1~2개월 내)

| # | 액션 | 상세 | 참고 |
|---|------|------|------|
| 5 | **팩터 유효성 검증** | momentum_top, low_vol_top 등이 한국 시장에서 유효한지 과거 5년 데이터로 별도 검증 | §4.8 |

### 중기 개선 (3~6개월 내)

| # | 액션 | 상세 | 참고 |
|---|------|------|------|
| 12 | **DART·어닝 필터 고도화** | 기본 연동 완료(§5.11). 유상증자·CB 등 키워드 공시, 예정일 커버리지 확대, 캐시·장애 시 폴백 정책 정교화 | §5.11 |
| 13 | **펀더멘털 신호 고도화** | `fundamental_factor` 전략·앙상블 구성으로 1차 반영됨. ROE 외 지표·해외 종목·공시 연계 강화는 지속 | §4.3a, §4.4 |
| 14 | **뉴스/센티먼트 데이터** | DART 공시 또는 뉴스 센티먼트를 신호에 반영하여 기술지표만의 한계 보완 | §4.7.2 |
| 15 | **웹 대시보드 강화** | 전략별 신호, 주문 목록, 시장 국면, API 사용량, 블랙스완 상태 실시간 표시 | §9.1 |
| 16 | **WebSocket 갭 처리** | 재연결 시 REST API 보충 조회, 갭 중 급변 감지 | §9.1 |
| 17 | **PostgreSQL 전환** | 운영 기간 길어질수록 SQLite의 데이터 용량·성능 문제. SQLAlchemy 연동 완비 | §9 |

### 장기 검토 (선택)

- [ ] ML/딥러닝 예측 모델 연동
- [ ] 멀티 증권사(Kiwoom 등) 지원
- [ ] Grafana 등 고급 대시보드

---

## 11. 주의사항

### 🚨 치명적 주의 — 실전 투입 전 반드시 확인

- **신호 품질 미검증**: 현재 가중치(RSI +2, MACD +2, 볼린저 +1 등)는 직관·예시용이며 통계적 근거가 없습니다. **이 상태로 실전 자동매매를 돌리면 수익보다 손실 가능성이 더 높습니다.** §1.3의 "실전 투입 전 반드시 완료해야 할 4가지"를 모두 마칠 때까지 실전 투입을 하지 마세요.
- **과적합**: OOS 검증·워크포워드를 통과해도 같은 시대의 데이터로 검증하면 간접적으로 과적합될 수 있습니다. 여러 시장 국면(상승·하락·횡보)이 포함된 기간으로 검증하세요.
- **블랙스완·갭**: 비상 손절·현금 비중 유지. REST 주기 갱신·`gap_risk`로 갭다운 대응은 보강되었으나, 웹소켓 단절 구간의 **초단기** 급변은 여전히 누락 가능(§9.1).

### ⚠️ 경고

- **소액 시작**: 페이퍼 1개월 이상 → 소액 실전(운용 예정 금액의 10% 이하) → 점진적 증액. 모든 검증을 통과한 후에만.
- **수수료·과매매**: 왕복 약 0.23%(수수료 0.015%×2 + 거래세 0.20%, 2026년 기준). 히스터리시스(§5.13)와 최소 보유 기간 5일(§5.14)이 기본 활성화되어 과매매를 구조적으로 억제하지만, 직관값 가중치 상태에서는 여전히 위험 존재. §8.3의 구체적 수치 예시 참고.
- **생존자 편향**: `backtest_universe.mode: historical` 필수. `current` 상태에서 백테스트 수익률은 허구일 수 있음. §8.2.1 참고.
- **한국 시장 특성**: 현재 전략 파라미터(200일선, ADX 25 등)는 미국 시장 기준. 한국 시장(박스권, 빠른 추세 전환)에서 동일하게 유효하다는 근거 부족. §4.5, §4.7.1 참고.
- **앙상블 독립성**: technical과 momentum_factor가 실질적으로 같은 정보 사용. 진정한 다각화를 위해 펀더멘털/뉴스 신호 추가 필요. §4.5.4 참고.

### ℹ️ 참고

- **법적**: 개인 계좌만 자동매매 허용. 타인 자금 대리 운용 불법.
- **세금**: 양도소득세·증권거래세 등 신고 의무 확인.
- **운영 환경**: 장 시간 무중단 필요 시 클라우드·NAS 등 권장.
- **데이터 소스 불일치**: 백테스트와 실전에서 **동일 데이터 소스**(FDR 권장) 사용 필수. `data_source.preferred: fdr`, `allow_kis_fallback: false` 설정 권장. §2.2 참고.
- **KIS API 의존성**: 시세·주문·잔고가 모두 KIS API에 의존. KIS 서버 장애 시 시스템 전체가 멈추며, 포지션을 들고 있는 경우 손절이 지연될 수 있음.

---

## 부록: 용어 정리

| 용어 | 설명 |
|------|------|
| **시장 비효율성** | 가격이 정보를 완전 반영하지 않아 수익 기회가 생기는 현상. 퀀트 전략은 특정 비효율성(과반응 후 되돌림, 모멘텀 등)을 이용해 수익을 노린다. |
| **모멘텀 효과** | 좋은(나쁜) 성과가 일정 기간 지속되는 현상. 추세 추종 전략이 이용하는 팩터. |
| **과반응 후 되돌림** | 단기적으로 가격이 과하게 움직였다가 평균으로 돌아오는 현상. 평균 회귀 전략이 이용하는 팩터. |
| EMA/SMA | 지수/단순 이동평균 |
| 골든크로스/데드크로스 | 단기선이 장기선 상향/하향 돌파 |
| 슬리피지 | 주문 예상가와 실제 체결가 차이 |
| MDD | Maximum Drawdown |
| 샤프 지수 | 위험 대비 수익 효율 |
| 워크포워드 | 슬라이딩 윈도우 반복 검증 |
| Z-Score | 평균 대비 표준편차 배수 |
| OBV | On Balance Volume |
| ATR | Average True Range |
| ADX | Average Directional Index |
| VWAP | Volume Weighted Average Price |

---

> 📌 **이 문서는 개발 진행에 따라 지속적으로 업데이트됩니다.**  
> 상세 파일별 역할·데이터 흐름은 `docs/PROJECT_GUIDE.md` 참고.
> **최종 수정**: 2026-05-06 (target-weight cost-aware pre-trade risk 반영)

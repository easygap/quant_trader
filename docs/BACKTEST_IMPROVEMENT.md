# 백테스트 신뢰성 개선 내역

> **문서 버전**: v5.2
> **최종 수정**: 2026-04-30
> **목적**: 백테스트 왜곡을 줄이기 위해 적용된 개선 사항, 알려진 한계, 추가 과제를 정리

---

## 1. 적용 완료된 개선 사항

### 1.1 Look-Ahead Bias 방지

| 항목 | 구현 | 파일 |
|------|------|------|
| **Strict Lookahead 기본 활성화** | `Backtester.run(strict_lookahead=True)` — 매매 판단 시 `T+1` 시가로 체결, 당일 종가로 판단 불가 | `backtest/backtester.py` |
| **Fundamental Factor Point-in-Time** | `as_of_date` 전파 + 60일 안전 마진. 분기 실적 공시 전 데이터 사용 차단 | `strategies/fundamental_factor.py` |
| **Lookahead Gap 검증** | `StrategyValidator.run()`이 strict=True/False 두 결과를 비교하여 gap 보고 | `backtest/strategy_validator.py` |

### 1.2 거래 비용 반영

| 항목 | 설정값 | 구현 |
|------|--------|------|
| **수수료** | 0.015% (매수/매도 각) | `risk_params.yaml:transaction_costs.commission_rate` |
| **거래세** | 0.20% (매도 시, 2025 이후 인하 반영) | `risk_params.yaml:transaction_costs.tax_rate` |
| **슬리피지** | 기본 0.05% + 동적(체결량 기반) | `risk_params.yaml:transaction_costs.slippage` + `dynamic_slippage` |
| **백테스트 반영** | 체결가 = 시가 × (1 + slippage), PnL에서 수수료+세금 차감 | `backtest/backtester.py:_execute_trade()` |
| **실전 반영** | `OrderExecutor._calculate_costs()` → TradeHistory에 commission/tax/slippage 별도 저장 | `core/order_executor.py` |

### 1.3 과매매 억제

| 항목 | 구현 | 파일 |
|------|------|------|
| **월간 왕복 제한** | `max_monthly_roundtrips: 8` (종목당) | `risk_params.yaml`, `core/order_executor.py` |
| **최소 보유 기간** | `min_holding_days: 5` | `risk_params.yaml`, `core/order_executor.py` |
| **히스터리시스** | BUY 진입 임계값과 SELL 청산 임계값 분리 | `strategies.yaml:scoring.hysteresis` |
| **장 초반/종반 매수 차단** | 09:00~09:30, 15:00~15:30 신규 매수 불가 | `core/order_executor.py` |

### 1.4 생존자 편향 완화

| 항목 | 구현 | 파일 |
|------|------|------|
| **universe 모드** | `backtest_universe.mode: kospi200` 설정 시 해당 시점 코스피200 구성종목 사용 | `risk_params.yaml`, `backtest/strategy_validator.py` |
| **current 모드 경고** | `mode=current` 시 명시적 survivorship bias 경고 로그 출력 | `backtest/strategy_validator.py:_get_kospi_top_n_symbols()` |
| **관리종목 제외** | `exclude_administrative: true` 기본 적용 | `risk_params.yaml` |

### 1.5 데이터 품질

| 항목 | 구현 | 파일 |
|------|------|------|
| **수정주가 소스 추적** | FDR(수정주가)/yfinance(수정주가)/KIS(비수정) 소스별 기록 | `core/data_collector.py` |
| **소스 혼용 감지** | `check_source_consistency()` — KIS/FDR 혼용 시 경고 | `core/data_collector.py` |
| **KIS 폴백 경고** | KIS 비수정주가 사용 시 명시적 경고 + 백테스트 소스 불일치 안내 | `core/data_collector.py` |

### 1.6 Z-Score 안전장치

| 항목 | 구현 | 파일 |
|------|------|------|
| **Division by zero 방어** | `z_std == 0` → NaN → `fillna(0)` → HOLD 유도 (Inf 전파 차단) | `strategies/mean_reversion.py` |

---

## 2. 검증 체계

### 2.1 Walk-Forward 검증

- `StrategyValidator.run_walk_forward()`: 슬라이딩 윈도우 방식
- 각 윈도우별 OOS Sharpe, MDD, 수익률 기록
- 전체 통과율 60% 이상이 `paper_candidate` 승격 조건
- 결과: `reports/validation_walkforward_*.json`

### 2.2 벤치마크 비교

- 코스피 지수(KS11) Buy & Hold
- 코스피 상위 50 동일비중 Buy & Hold
- 비용 반영 후 초과수익 기준
- Research sweep은 promotion gate용 raw EW B&H excess를 유지하면서, cash-heavy/defensive 후보 해석을 위해 후보 일별 노출(`value-cash`)과 동일한 노출로 B&H를 했을 때의 exposure-matched excess도 진단값으로 기록

### 2.3 In/Out-of-Sample 분리

- 기본 70/30 분할
- IS/OOS 각각 Sharpe, MDD, 수익률 보고
- `lookahead_return_gap` 자동 계산 (strict vs relaxed 차이)

---

## 3. 알려진 한계

| 한계 | 상세 | 영향 |
|------|------|------|
| **유동성 미모델링** | 체결량 대비 주문량 비율 미고려. 소형주에서 실제 체결 불가 가능 | 백테스트 성과 과대평가 |
| **이벤트 리스크** | 배당락, 공시, 합병 등 이벤트 미반영 | 급변 구간에서 왜곡 |
| **호가 단위 미반영** | 한국 시장 호가 단위(5원/10원/50원 등) 미적용 | 체결가 미세 차이 |
| **장중 가격 미사용** | 일봉 기반 시뮬레이션. 장중 변동 미반영 | 손절/익절 발동 시점 차이 |
| **단일 종목 검증 한계** | 현재 검증은 watchlist 3종목 위주. 유니버스 전체 검증 미실행 | 전략 일반화 불확실 |
| **멀티전략 강건성** | BV50/R50 Paper 가동 중 (2026-04-01~). **debiased 재평가**: Rotation 단독 +18.09%/PF 1.62/WF 100%, BV 단독 -13.31%/PF 0.79. BV sleeve merit=research_only | Paper Evidence 체계 (`core/paper_evidence.py`) + 승격 규칙 v3 자동 판정. scoring: clean_final_days=3 달성 (2026-04-09) |
| **cash-only day 처리** (v5.1) | blocked/no-position 상태에서 당일 PortfolioSnapshot 없으면 daily_return=None → benchmark_status=failed → clean day 불인정 deadlock | **수정 완료** — 직전 snapshot + 거래 0건이면 daily_return=0.0 추론. 진짜 데이터 부재만 failed |
| **벤치마크 비용 미반영** (v5.0 수정) | `_buy_and_hold_metrics`에 거래비용 미적용 → 전략 alpha 0.2~0.5%p 과대평가 | **수정 완료** — commission/tax/slippage 반영 |
| **방어형 후보 raw benchmark 해석** | cash-switch처럼 평균 노출이 낮은 후보는 full B&H 대비 excess가 과도하게 나빠 보일 수 있음 | **진단 추가** — research sweep에 exposure-matched B&H return/sharpe/MDD/excess 기록. 단, promotion gate는 raw benchmark excess 유지 |
| **회전 전략의 sparse signal 한계** | 월간 상대강도 후보가 BUY/SELL 신호만 내면 목표 top-N을 지속적으로 채우지 못해 평균 노출이 낮게 측정될 수 있음 | **검증 완료** — target-weight top-N research backtester로 avg exposure 85%대까지 개선. 5종목 smoke는 raw excess 음수였지만 canonical top-20 full sweep은 alpha 후보 확인. `hold_rank_buffer` 적용 후 turnover gate 통과, `benchmark_risk` overlay 적용 후 best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`가 return=+210.24%, raw excess=+60.85%p, Sharpe=1.60, MDD=-19.24%, turnover/year=858.0%, WF positive/Sh+ 100%로 research sweep 기준 provisional gate 통과 |
| **target-weight 후보의 paper 연결 부재** | research-only evaluator에서 provisional 후보가 나와도 기존 canonical/paper 경로는 등록 전략만 평가 | **부분 해결** — `tools/evaluate_and_promote.py --canonical`이 `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`를 canonical bundle에 포함하고 `provisional_paper_candidate`로 재현. 다음 과제는 paper pilot execution adapter |
| **백테스트 BlackSwan/어닝/갭 필터 미적용** | backtester에 BlackSwan, 어닝 필터, 갭 리스크 체크 미포함. paper/live에만 존재 | 백테스트-live 성과 차이 원인. 문서화됨 |

---

## 4. 추가 과제

| 과제 | 우선순위 | 상태 |
|------|----------|------|
| 유니버스 전체 (코스피200) 백테스트 | 높음 | 미실행 |
| Strategy Ablation Test (전략별 단독 성과 비교) | 높음 | **C-4/C-5 단독·sleeve 비교 완료** |
| 비용 반영 전/후 성과 비교 리포트 자동화 | 중간 | 미실행 |
| 월별 성과 분해 | 중간 | **C-5 반기별 분해 구현 완료** |
| 유동성 필터 (일평균 거래대금 기준 종목 제외) | 높음 | 미구현 |
| Sortino Ratio 자동 계산 | 낮음 | 구현 완료 (리포트 미포함) |
| Calmar Ratio 자동 계산 | 낮음 | 구현 완료 (리포트 미포함) |
| Rotation 하락장 방어 (시장 국면 필터) | 높음 | **완료 — KS11 SMA200 필터, abs momentum 필터 테스트 후 NO_MEANINGFUL_IMPROVEMENT 판정. trailing stop 제거(승률 18-29%)로 DEV -4.99% -> -0.96% 개선** |
| 멀티전략 sleeve 비중 최적화 재검증 | 높음 | **완료 — TS OFF + TP 7% 적용 후 BV50/R50 OOS 2.87%, rolling WF 60% positive** |
| KR_CORE_10 유니버스 확장 | 중간 | 대기 (BV50/R50 paper 운영 결과 확인 후) |
| Rotation trailing stop 제거 | 높음 | **완료 — disable_trailing_stop: true. 승률 18-29%, negative EV -> capture rate 71%->79%(DEV), 78%->83%(OOS)** |
| Rotation TP sweep (8% -> 7%) | 높음 | **완료 — per-strategy TP override. DEV -0.96% -> -0.19%, OOS 4.25% -> 4.71%** |
| Rolling walk-forward 검증 (10 windows) | 높음 | **완료 — BV50/R50 positive 60%, median +0.45%, worst -2.05%** |
| Paper 모니터링 인프라 | 높음 | **완료 — c5_paper_monthly_report.py, signal/executed/skipped 카운터, guardrail 설정** |
| BV50/R50 Paper Trading 60영업일 | 높음 | **진행 중 — 2026-04-01 개시. Paper Evidence 체계 도입** |
| Debiased 전략 재평가 | 높음 | **완료 — 거래대금 기반 ex-ante proxy 20종목, portfolio WF 6 windows** |
| 승격 규칙 v3 자동 판정 | 높음 | **완료 — `core/promotion_engine.py` + `tools/evaluate_and_promote.py` artifact-driven** |
| 주문 상태기계 | 높음 | **완료 — OrderStatus 9개 상태, FILLED 전 position 반영 없음, live/paper 회귀 테스트 green** |
| 벤치마크 거래비용 반영 | 높음 | **완료 — `_buy_and_hold_metrics`에 commission/tax/slippage 적용** |
| Paper Evidence 수집 체계 | 높음 | **완료 — `core/paper_evidence.py` 일별 22개 지표, 6 anomaly rule, 9 approval gate** |
| Paper Runtime State Machine | 높음 | **완료 — `core/paper_runtime.py` 5개 상태(normal/degraded/frozen/blocked/research_disabled), schema quarantine** |
| Paper Pilot Authorization | 높음 | **완료 — `core/paper_pilot.py` launch readiness + pilot auth + 리스크 캡** |
| Paper Preflight Check | 높음 | **완료 — `core/paper_preflight.py` 운영 준비 상태 점검** |
| Strategy Universe Registry | 높음 | **완료 — `core/strategy_universe.py` paper 대상 전략 canonical 목록** |
| Zero-return Semantics (deadlock 해소) | 높음 | **완료 — cash-only/no-position day에서 daily_return=0.0 추론, benchmark final 가능** |
| Paper 운영 도구 (tools/) | 높음 | **완료 — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI** |
| Entry filter 탐색 (market filter, abs momentum, cooling) | 중간 | **완료 — 모두 NO_MEANINGFUL_IMPROVEMENT 또는 ADVERSE EFFECT. 현행 유지** |
| Research sweep exposure-matched benchmark | 중간 | **완료 — 후보별 평균 노출/현금비중과 exposure-matched B&H excess 진단 추가. promotion gate는 raw benchmark 기준 유지** |
| Target-weight top-N rotation backtester | 높음 | **완료 — 월간 직전일 점수 기준 top-N 목표비중 리밸런싱, delta 거래비용, 노출 진단 구현. 5종목 smoke best +128.44%/Sharpe 1.13/avg exposure 85.3%지만 raw excess=-45.19%p. canonical top-20 full sweep best 기존 후보는 +212.21%/raw excess=+62.82%p였으나 turnover/year=1412.1%로 research-only** |
| Target-weight score-floor 후보 | 중간 | **완료 — `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금으로 남김. best=`target_weight_rotation_top5_60_120_floor0`, +210.21%/Sharpe 1.41/WF positive 100%였으나 turnover/year=1081.5%라 다음 과제는 turnover-aware 리밸런싱** |
| Target-weight rank-hysteresis 후보 | 높음 | **완료 — `hold_rank_buffer`로 보유 종목이 top-N 밖으로 소폭 밀려도 버퍼 안이면 유지. best=`target_weight_rotation_top5_60_120_floor0_hold3`, +278.57%/Sharpe 1.65/WF positive 100%/turnover 807.8%. 남은 병목은 MDD=-28.25%** |

---

## 5. 참고 문서

| 문서 | 내용 |
|------|------|
| `quant_trader_design.md` §8 | 백테스팅 & 검증 전체 아키텍처 |
| `reports/strategy_promotion_policy.md` | 전략 승격 정량 기준표 |
| `reports/live_gate_policy.md` | Live 진입 canonical/evidence hard gate |
| `reports/paper_experiment_manifest.json` | 60영업일 paper 실험 설정 |
| `reports/full_paper_lifecycle_test.json` | Lifecycle 테스트 4/4 PASS 결과 |
| `reports/paper_evidence/` | Paper Evidence JSONL + promotion package |
| `reports/paper_runtime/` | Runtime state + launch readiness artifact |
| `reports/promotion/` | Promotion 판정 결과 |
| `reports/paper_runbook.md` | Paper Trading 운영 가이드 |
| `scripts/c5_rotation_filter_test.py` | KS11 SMA200 시장 필터 비교 테스트 |
| `scripts/c5_rotation_absmom_test.py` | 절대 모멘텀 필터 비교 테스트 |
| `scripts/c5_rotation_trade_diagnostic.py` | 거래 단위 진단 분석 |
| `scripts/c5_rotation_cooling_test.py` | min_hold_days 냉각 기간 테스트 |
| `scripts/c5_rotation_no_ts_test.py` | trailing stop 제거 효과 테스트 |
| `scripts/c5_rotation_tp_sweep.py` | TP 비율 스윕 + sleeve 재비교 |
| `scripts/c5_sleeve_sweep_nots.py` | TS OFF 상태 sleeve 비중 스윕 |
| `scripts/c5_rolling_walkforward.py` | rolling walk-forward 검증 (10 windows x 12mo) |
| `scripts/c5_tp_override_verify.py` | per-strategy TP override 검증 |
| `scripts/c5_paper_monthly_report.py` | paper trading 월간 리포트 생성 |

# 백테스트 신뢰성 개선 내역

> **문서 버전**: v5.3
> **최종 수정**: 2026-05-08
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
| **백테스트 반영** | 체결가에 슬리피지 반영, PnL에서 수수료+세금 차감. 단일종목/포트폴리오/target-weight research 모두 평균 거래량 기반 동적 슬리피지 적용 | `backtest/backtester.py`, `backtest/portfolio_backtester.py`, `tools/research_candidate_sweep.py` |
| **실전 반영** | `OrderExecutor._calculate_costs()` → TradeHistory에 commission/tax/slippage 별도 저장. live 주문은 체결가·체결수량 확인 후에만 거래·포지션 DB 반영 | `core/order_executor.py` |

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
| **백테스트 BlackSwan/어닝/갭 필터 미적용** | 단일종목·포트폴리오 백테스터에 BlackSwan, 어닝 필터, 갭 리스크 체크가 없으면 paper/live보다 낙관적인 성과가 나올 수 있음 | **수정 완료** — `backtest/backtester.py`와 `backtest/portfolio_backtester.py`가 원본 `open`/이벤트 컬럼을 보존하고 `gap_risk` 갭다운 청산·갭업 신규 매수 차단, `earnings_date`/`next_earnings_date`/flag 기반 어닝 윈도우 신규 매수 차단, `risk_params.blackswan` 기반 긴급 청산·쿨다운·recovery 사이징을 반영 |
| **리서치 벤치마크 부분 결측** | EW B&H 벤치마크 일부 종목 수집 실패 시 누락 종목 몫의 capital이 빠진 채 전체 capital 대비 수익률을 계산하면 후보 초과수익이 과대평가될 수 있음 | **수정 완료** — `research_candidate_sweep`이 벤치마크 입력 universe 전체 수집·기간 검증을 요구하고, 결측 시 `INSUFFICIENT_BENCHMARK_DATA`로 excess gate를 fail-closed 차단 |
| **live ACK 미체결 장부 오염 위험** | KIS 주문 ACK만 있고 평균 체결가·체결수량 조회가 실패했거나 부분체결인데 예상가 기준 전량 체결로 기록하면 실제 잔고와 DB 포지션이 어긋날 수 있음 | **수정 완료** — live BUY/SELL은 체결 확인 실패 시 `ACKED`/pending, 부분체결 시 `PARTIAL_FILLED`/pending과 `requires_reconcile=True`를 반환하고, KIS 잔고 대조 전 TradeHistory·Position 반영을 보류 |
| **live 미체결 조회 fail-open 위험** | KIS 미체결 조회 API 실패나 응답 형식 오류를 “미체결 없음”으로 처리하면 재시작/통신 장애 상황에서 중복 주문이 제출될 수 있음 | **수정 완료** — `get_unfilled_order_status()`가 조회 성공 여부를 분리하고, live BUY/SELL은 `checked=False`면 주문 전 fail-closed 차단. 재시작 복구의 전체 미체결 조회 실패도 critical 알림으로 노출 |

---

## 4. 추가 과제

| 과제 | 우선순위 | 상태 |
|------|----------|------|
| 유니버스 전체 (코스피200) 백테스트 | 높음 | 미실행 |
| Strategy Ablation Test (전략별 단독 성과 비교) | 높음 | **C-4/C-5 단독·sleeve 비교 완료** |
| 비용 반영 전/후 성과 비교 리포트 자동화 | 중간 | 미실행 |
| 월별 성과 분해 | 중간 | **C-5 반기별 분해 구현 완료** |
| 유동성 필터 (일평균 거래대금 기준 종목 제외) | 높음 | **완료 — watchlist 진입 대상, 포트폴리오 백테스트 입력 universe, research candidate sweep universe에 20일 평균 거래대금 하한 필터 적용. target-weight pilot 주문 전 ADV preflight도 유지** |
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
| 주문 상태기계 | 높음 | **완료 — OrderStatus 9개 상태, FILLED 전 position 반영 없음, 테스트 226건 green** |
| 벤치마크 거래비용 반영 | 높음 | **완료 — `_buy_and_hold_metrics`에 commission/tax/slippage 적용** |
| Paper Evidence 수집 체계 | 높음 | **완료 — `core/paper_evidence.py` 일별 22개 지표, 6 anomaly rule, 9 approval gate** |
| Paper Runtime State Machine | 높음 | **완료 — `core/paper_runtime.py` 5개 상태(normal/degraded/frozen/blocked/research_disabled), schema quarantine** |
| Paper Pilot Authorization | 높음 | **완료 — `core/paper_pilot.py` launch readiness + pilot auth + 리스크 캡** |
| Paper Preflight Check | 높음 | **완료 — `core/paper_preflight.py` 운영 준비 상태 점검** |
| Portfolio backtest event guard | 높음 | **완료 — `backtest/portfolio_backtester.py`에 gap/어닝/BlackSwan 청산·차단·recovery와 진단 카운터 추가** |
| Portfolio backtest dynamic slippage | 높음 | **완료 — 포트폴리오 백테스터가 20일 평균 거래량을 거래비용 계산에 전달하고 trade record에 participation/slippage 진단값 기록** |
| Target-weight research dynamic slippage | 높음 | **완료 — target-weight 리서치 백테스터가 20일 평균 거래량을 거래비용 계산에 전달하고 participation/slippage 진단값 기록** |
| Research sweep benchmark coverage guard | 높음 | 완료 — 벤치마크 일부 종목 결측 시 초과수익 계산을 신뢰하지 않고 artifact/Markdown에 결측 종목과 coverage ratio를 남김 |
| Live 체결 확인 guard | 높음 | 완료 — KIS 주문 ACK 후 체결가·체결수량 조회가 실패하거나 부분체결만 확인되면 예상가 기준 `FILLED` 처리 대신 `ACKED`/`PARTIAL_FILLED` pending으로 남기고 `requires_reconcile=True`로 운영 대조를 요구 |
| Live 미체결 조회 fail-closed | 높음 | 완료 — KIS 미체결 조회 실패, `rt_cd != 0`, 응답 형식 이상을 주문 가능 상태로 보지 않고 live BUY/SELL을 제출 전 차단 |
| Strategy Universe Registry | 높음 | **완료 — `core/strategy_universe.py` paper 대상 전략 canonical 목록** |
| Zero-return Semantics (deadlock 해소) | 높음 | **완료 — cash-only/no-position day에서 daily_return=0.0 추론, benchmark final 가능** |
| Paper 운영 도구 (tools/) | 높음 | **완료 — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI** |
| Entry filter 탐색 (market filter, abs momentum, cooling) | 중간 | **완료 — 모두 NO_MEANINGFUL_IMPROVEMENT 또는 ADVERSE EFFECT. 현행 유지** |

---

## 5. 참고 문서

| 문서 | 내용 |
|------|------|
| `quant_trader_design.md` §8 | 백테스팅 & 검증 전체 아키텍처 |
| `reports/strategy_promotion_policy.md` | 전략 승격 정량 기준표 |
| `reports/live_gate_policy.md` | Live 진입 5개 조건 |
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

# 백테스트 신뢰성 개선 내역

> **문서 버전**: v6.1
> **최종 수정**: 2026-05-21
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
| **백테스트 반영** | 체결가에 슬리피지 반영, PnL에서 수수료+세금 차감. 단일종목/포트폴리오/target-weight research 모두 평균 거래량 기반 동적 슬리피지 적용. target-weight research는 직전 거래일 신호 기준 다음 거래일 시가 체결, 종가 평가 | `backtest/backtester.py`, `backtest/portfolio_backtester.py`, `tools/research_candidate_sweep.py` |
| **실전 반영** | `OrderExecutor._calculate_costs()` → TradeHistory에 commission/tax/slippage 별도 저장. live 주문은 체결가·체결수량 확인 후에만 거래·포지션 DB 반영 | `core/order_executor.py` |
| **Paper/live 정산** | TradeHistory.price를 실제 체결가로 고정. 슬리피지는 체결가에 반영된 진단값으로 남기고 현금 흐름에서는 수수료·세금만 별도 차감해 중복 비용 차감을 방지 | `database/repositories.py`, `core/order_executor.py` |

### 1.3 과매매 억제

| 항목 | 구현 | 파일 |
|------|------|------|
| **월간 BUY 제한** | `max_monthly_roundtrips: 8` (종목당). 백테스트와 paper/live 신규 BUY 주문에 동일하게 적용 | `risk_params.yaml`, `core/order_executor.py` |
| **최소 보유 기간** | `min_holding_days: 5`. 손절·갭다운·블랙스완·수동 긴급 청산은 손실 방어로 예외 허용 | `risk_params.yaml`, `core/order_executor.py` |
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
| **pilot guard fail-open 위험** | pilot authorization 이후 runtime/evidence/notifier/cap guard 오류가 예외 처리로 삼켜지거나, 현재 노출만 보고 이번 주문 후 gross exposure cap 초과를 놓치면 제한 주문이 허용될 수 있음 | **수정 완료** — `check_pilot_entry()` 모든 blocked/allowed 결과 audit, guard 예외는 fail-closed block, notifier health missing/corrupt 차단, 날짜/cap이 잘못된 enabled auth record 제외, 현재 노출+이번 주문 금액 기준 gross exposure cap 검증 |
| **paper evidence append 순서 의존** | backfill/shadow/finalize 기록이 JSONL 뒤쪽에 섞이면 최신 evidence, 최근 10일, promotion period가 append 순서 기준으로 왜곡될 수 있음 | **수정 완료** — canonical view를 날짜별 최신 record + 날짜순 반환으로 고정하고, 중복 확인은 전체 파일의 해당 날짜 최신 record를 조회 |
| **legacy evidence E2E 수집 경로** | 오래된 v1 `save_daily_evidence`/`evidence_collector` 계약이 남아 pytest collection과 scheduler 장마감 legacy 호출을 깨뜨릴 수 있음 | **수정 완료** — E2E를 v2 JSONL/canonical/report 계약으로 교체, scheduler legacy 호출 제거, `core.evidence_collector`는 import 호환 no-op shim으로 축소 |
| **방어형 후보 raw benchmark 해석** | cash-switch처럼 평균 노출이 낮은 후보는 full B&H 대비 excess가 과도하게 나빠 보일 수 있음 | **진단 추가** — research sweep에 exposure-matched B&H return/sharpe/MDD/excess 기록. 단, promotion gate는 raw benchmark excess 유지 |
| **회전 전략의 sparse signal 한계** | 월간 상대강도 후보가 BUY/SELL 신호만 내면 목표 top-N을 지속적으로 채우지 못해 평균 노출이 낮게 측정될 수 있음 | **검증 완료** — target-weight top-N research backtester로 avg exposure 85%대까지 개선. 5종목 smoke는 raw excess 음수였지만 canonical top-20 full sweep은 alpha 후보 확인. `hold_rank_buffer`와 `benchmark_risk` overlay 적용 후 best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`가 next-open/결측 진단 재검증 기준 return=+171.20%, PF=4.24, Sharpe=1.41, MDD=-19.90%, WF positive=100%, WF Sharpe+=83.3%로 provisional gate 통과. 단, held stale valuation 21일과 보유 종목 시가 누락 리밸런싱 skip 1회가 남아 paper pilot에서 체결 가능성 검증 필요 |
| **target-weight 후보의 paper 연결 부재** | research-only evaluator에서 provisional 후보가 나와도 기존 canonical/paper 경로는 등록 전략만 평가 | **대부분 해결** — canonical bundle 재현 완료 + `core/target_weight_rotation.py`, `tools/target_weight_rotation_pilot.py`로 전용 paper/pilot adapter 추가. dry-run은 `--record-shadow-evidence`, `--shadow-days 3`, 또는 `--shadow-start-date/--shadow-end-date`로 non-promotable shadow readiness evidence, launch readiness artifact, pilot runbook을 남기고 cap preview와 plan 기반 최소/추천 cap + enable 명령으로 pilot 승인 전 캡 적합성을 확인. 이후 `--readiness-audit`가 주문 제출, evidence 기록, pilot session 저장 없이 clean shadow/launch readiness, active pilot auth와 cap validation, 중복 session idempotency, 실행일/장 시간, 실행 전 position drift, 추천 cap 충족 여부, 유동성 preflight, 비용 반영 pre-trade risk를 JSON artifact와 Markdown 운영 리포트로 판정한다. 유동성 preflight는 최근 20일 평균 거래대금 대비 주문 notional 비율을 계산하고 기본 5%(`--max-order-adv-pct`) 초과 주문은 readiness와 실행을 fail-closed 차단한다. pre-trade risk는 `RiskManager.calculate_transaction_costs()`의 수수료/세금/동적 슬리피지 예상 체결가를 재사용해 현금 부족, 최소 현금비중, 총투자비중, 종목별 비중, 보유 종목 수 위반을 주문 제출 전에 차단한다. Markdown 리포트는 shadow 수집, audit 재실행, 추천 cap 승인, capped paper 실행 명령을 함께 남긴다. `--shadow-days N`은 휴장/데이터 공백으로 같은 거래일에 매핑될 때 과거 평일을 추가 스캔해 N개 고유 resolved trade_day 충족을 목표로 하며, 목표 미달이나 날짜별 실패는 non-zero 종료로 fail-closed 처리한다. 실행형 `pilot_paper` evidence는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, `run_pilot` 점검과 `execute_plan` 주문 제출 직전 재확인에서 실제 paper position이 계획 입력 장부 `position_quantities_before`와 모두 일치하고, KST 실행일과 KRX 정규장 주문 가능 시간을 통과하고, 유동성 preflight와 pre-trade risk를 통과하고, 계획 주문 전부 성공 및 주문 결과 payload와 당일 `TradeHistory` fill 집계가 plan과 일치하며, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 수집한다. 같은 날짜가 이미 기록된 경우에도 최신 canonical evidence가 `pilot_paper`/authorized/execution-backed이고 target-weight plan hash와 complete/execution-market-session/liquidity/pre-trade-risk/order/fill/position 검증을 통과해야 재사용해 중복 실행/stale plan/장 외 실행/부분 실행/중단/주문 결과 불일치/체결 기록 불일치/기존 evidence 검증 실패/포지션 불일치일이 승격 증거로 섞이지 않게 한다. 명시 재시도는 `--allow-rerun`으로만 허용한다. 다음 과제는 shadow clean days 충족 후 정규장 capped pilot_paper execution-backed evidence 축적 |
| **target-weight stale 가격 ffill 위험** | 일부 종목 또는 벤치마크의 최신 데이터가 비었는데 panel ffill로 오래된 종가가 `trade_day`/research 평가일 최신 가격처럼 쓰이면 목표 수량·리스크·승격 증거·연구 성과가 왜곡될 수 있음 | **수정 완료** — `build_target_weight_plan()`은 운영 주문 계획이므로 종목별 `price_last_dates`와 `benchmark_last_date`를 diagnostics에 기록하고 stale 종목/벤치마크를 plan 생성 전 fail-closed 차단. `run_target_weight_rotation_backtest()`는 research-only 경로라 score day 실제 종가가 없는 종목은 해당 리밸런싱 후보에서 제외하고, 보유 종목의 일별 stale 평가는 `held_stale_valuation_*`로 기록하며, 보유 종목 시가가 없어 리밸런싱할 수 없는 날은 거래를 건너뛰고 `missing_held_open_*`/`skipped_rebalance_missing_held_open_count`로 기록. 벤치마크 stale과 신규 top-N 매수 후보의 시가 누락은 계속 fail-closed |
| **백테스트 BlackSwan/어닝/갭 필터 미적용** | 단일종목·포트폴리오 백테스터에 BlackSwan, 어닝 필터, 갭 리스크 체크가 없으면 paper/live보다 낙관적인 성과가 나올 수 있음 | **수정 완료** — `backtest/backtester.py`와 `backtest/portfolio_backtester.py`가 원본 `open`/이벤트 컬럼을 보존하고 `gap_risk` 갭다운 청산·갭업 신규 매수 차단, `earnings_date`/`next_earnings_date`/flag 기반 어닝 윈도우 신규 매수 차단, `risk_params.blackswan` 기반 긴급 청산·쿨다운·recovery 사이징을 반영. 운영 `OrderExecutor`도 갭업 신규 매수 확인용 최근 가격 조회 실패·데이터 부족을 신규 BUY 차단으로 처리해 데이터 공백 상태의 추격매수를 막는다 |
| **리서치 벤치마크 부분 결측** | EW B&H 벤치마크 일부 종목 수집 실패 시 누락 종목 몫의 capital이 빠진 채 전체 capital 대비 수익률을 계산하면 후보의 raw benchmark excess가 과대평가될 수 있음 | **수정 완료** — `buy_and_hold_benchmark_with_returns()`가 입력 universe 전체 수집·기간 검증을 요구하고, 결측이 있으면 `benchmark_coverage_complete=false`, 결측 종목, coverage ratio를 artifact/Markdown에 남긴 뒤 `INSUFFICIENT_BENCHMARK_DATA`로 excess gate를 fail-closed 차단 |
| **live ACK 미체결 장부 오염 위험** | KIS 주문 ACK만 있고 평균 체결가·체결수량 조회가 실패했거나 부분체결인데 예상가 기준 전량 체결로 기록하면 실제 잔고와 DB 포지션이 어긋날 수 있음 | **수정 완료** — live BUY/SELL은 체결 확인 실패 시 `ACKED`/pending, 부분체결 시 `PARTIAL_FILLED`/pending과 `requires_reconcile=True`를 반환하고, KIS 잔고 대조 전 TradeHistory·Position 반영을 보류 |
| **paper 매수·매도·현금 정산 비용 왜곡** | paper BUY 수량·방어 가격이 원 신호가 기준이고 SELL이 모델 슬리피지를 체결가에 반영하지 않거나, 이미 체결가에 반영된 슬리피지를 현금 흐름에서 다시 차감하면 paper 손익과 가용 현금이 왜곡될 수 있음 | **수정 완료** — paper BUY 수량·손절·익절·트레일링 기준을 예상 체결가로 보수화하고, paper SELL도 매수처럼 `RiskManager.calculate_transaction_costs()`의 execution_price로 체결 처리. DB 현금 요약은 체결가 기준으로 수수료·세금만 별도 반영하고 슬리피지는 진단값으로 집계 |
| **live 미체결 조회 fail-open 위험** | KIS 미체결 조회 API 실패나 응답 형식 오류를 “미체결 없음”으로 처리하면 재시작/통신 장애 상황에서 중복 주문이 제출될 수 있음 | **수정 완료** — `get_unfilled_order_status()`가 조회 성공 여부를 분리하고, live BUY/SELL은 `checked=False`면 주문 전 fail-closed 차단. 재시작 복구의 전체 미체결 조회 실패도 critical 알림으로 노출 |
| **target-weight research 당일 종가 체결 착시** | 직전 거래일 점수로 리밸런싱하면서 체결가를 리밸런싱 당일 종가로 쓰면 장중 변동을 이미 알고 체결한 것처럼 성과가 과대평가될 수 있음 | **수정 완료** — target-weight research는 원본 `open` panel을 별도 보존해 리밸런싱을 다음 거래일 시가로 체결하고, 일말 평가는 `close`로 분리. 신규 top-N 매수 후보의 리밸런싱일 `open` 누락은 `target_weight_research_execution_price_missing`으로 fail-closed 차단하고, 이미 보유한 종목의 `open`이 없으면 해당 월 리밸런싱을 skip 진단으로 남김 |

---

## 4. 추가 과제

| 과제 | 우선순위 | 상태 |
|------|----------|------|
| 유니버스 전체 (코스피200) 백테스트 | 높음 | 1차 실행 완료 — `--top-n 200 --candidate-id target_weight_rotation_top5_60_120_floor0_hold3_risk60_35` full sweep에서 200개 중 유동성 통과 164개, benchmark coverage 100%, return +110.39%, raw excess +78.50%p였지만 MDD -25.79%, turnover/year 1097.1%로 `paper_only` |
| target-weight 리스크 완화 후보군 검증 | 높음 | 완료 — `--candidate-family target_weight_risk_relief --top-n 200` full sweep에서 10개 후보 비교. 최상위 후보=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5`, return +125.61%, raw excess +93.72%p, exposure-matched excess +104.53%p, Sharpe 0.91, PF 2.19였지만 전 후보가 MDD -25.27%~-32.14%, turnover/year 1027.2%~1344.3%로 provisional 게이트 미통과. 판정=`KEEP_RESEARCH_ONLY`; 이후 저회전·변동성 타깃 후보군으로 병목을 재검증 |
| target-weight 저회전 후보군 | 높음 | 검증 완료 — `--candidate-family target_weight_turnover_relief --top-n 200` full sweep에서 6개 후보 비교. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk90_35_bimonthly`, return +85.72%, raw excess +53.83%p, turnover/year 616.7%. 분기 후보는 turnover/year 456.1~476.9%까지 낮췄지만 전 후보가 benchmark excess Sharpe<=0 및 MDD -25.35%~-35.12%로 `NO_ALPHA_CANDIDATE` |
| target-weight 변동성 타깃 후보군 | 높음 | 검증 완료 — `--candidate-family target_weight_volatility_target --top-n 200` full sweep에서 6개 후보 비교. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_vol16_dd8_floor35`, return +105.18%, raw excess +73.29%p, exposure-matched excess +91.35%p, Sharpe 0.81, PF 2.03, turnover/year 958.0%. 다만 전 후보가 MDD -24.26%~-31.04%로 provisional MDD 게이트 미통과, 5개 후보가 benchmark excess Sharpe<=0이라 판정=`KEEP_RESEARCH_ONLY`. 다음은 단순 노출 축소보다 종목 선별 랭킹에 낙폭·하방변동성·상관/업종 집중 페널티를 반영 |
| target-weight 리스크 페널티 랭킹 후보군 | 높음 | 검증 완료 — `--candidate-family target_weight_downside_rank_relief --top-n 200` full sweep에서 5개 후보 비교. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60`, return +132.51%, raw excess +100.62%p, exposure-matched excess +111.43%p, Sharpe 0.94, PF 2.21, WF positive/Sh+ 100%. 다만 전 후보가 MDD -25.20%~-33.07%, turnover/year 1143.8%~1366.0%로 provisional 게이트 미통과. 판정=`KEEP_RESEARCH_ONLY`; 다음은 랭킹 페널티와 격월 리밸런싱·넓은 tolerance·월별 신규 편입 수 제한 결합 검증 |
| target-weight churn control 후보군 | 높음 | 검증 완료 — `--candidate-family target_weight_churn_relief --top-n 200` full sweep에서 5개 후보 비교. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew2`, return +118.89%, raw excess +87.00%p, exposure-matched excess +97.61%p, Sharpe 0.89, PF 2.04, WF positive/Sh+ 100%. 다만 MDD -26.95%, turnover/year 1034.2%로 provisional 게이트 미통과. `maxnew1`·격월 후보는 turnover/year를 423.0~674.4%까지 낮췄지만 MDD와 benchmark excess Sharpe가 악화되어 판정=`KEEP_RESEARCH_ONLY`; 다음은 교체 제한보다 포트폴리오 drawdown guard·재진입 cooldown·상관/업종 집중도 페널티 검증 |
| target-weight portfolio drawdown guard 후보군 | 높음 | canonical 검증 완료 — `target_weight_drawdown_guard` family에 exp75/rankrisk90/pdd10/floor40 기반 `tol3`, `tol4`, `tol5`, `maxnew2`, `tol3_maxnew2`, tol4 guard-only 강화 4종, rank-risk 강화 2종, sector cap 3종, correlation cap 3종, correlation rank penalty 3종, loss re-entry guard 3종, position loss reduction 5종을 추가하고 `--top-n 200` full sweep으로 병목을 비교했다. 업종 매핑은 FDR Sector 누락 시 KRX KIND 상장법인 목록으로 2,766개 종목을 보강하고, 성공한 매핑은 `reports/sector_map_cache.json`에 저장해 실시간 소스 장애 시 fallback으로 사용한다. KRX KIND가 누락한 우선주는 같은 5자리 stem의 보통주 섹터를 추론하되, target-weight sector cap plan은 planning universe 전체 섹터 coverage가 없으면 `target_weight_sector_map_incomplete`로 fail-closed 차단한다. `rankrisk90_tol4_corrcap85`와 `sectorcap2_corrcap85`는 top-200에서 각각 기존 tol4/sectorcap2와 동일한 return +81.19%/+82.57%, raw excess +49.30%p/+50.68%p, MDD -19.33%/-19.59%, turnover/year 886.7%/909.3%였고 관측 최대 선택 상관도는 0.8147. correlation rank penalty는 top-200에서는 개선됐지만 canonical top-20에서는 benchmark excess가 음수라 모두 `paper_only`. loss re-entry guard는 회전율만 낮추고 MDD를 악화시켜 canonical 제외. 포지션 손실 감산은 `sectorcap2_posloss8`이 top-200 return +105.79%, raw excess +73.90%p, MDD -18.96%, Sharpe 1.00으로 통과했으나 canonical turnover/year 1006.2%로 `paper_only`; `tol5` 결합 후 `target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1`가 top-200 return +112.16%, raw excess +80.27%p, MDD -19.23%, Sharpe 1.04로 통과했고 canonical top-20에서도 return +198.15%, benchmark excess +48.76%p, Sharpe 1.57, PF 5.58, MDD -17.18%, turnover/year 993.9%, WF positive/Sh+ 100%로 신규 `provisional_paper_candidate`. 다만 live는 60일 paper evidence와 target-weight verified proof가 없어 차단. paper/pilot plan builder는 rank penalty, sector cap, position-loss 감산 산식과 paper evidence 기반 portfolio drawdown guard 상태를 적용하도록 보강했으며, sector map 누락 또는 drawdown guard 명시 상태 누락은 fail-closed 차단한다. evidence snapshot은 guard cooldown을 저장해 다음 리밸런싱 상태 복원에 사용한다 |
| target-weight 변동성 예산 목표비중 | 높음 | canonical 검증 완료·승격 보류 — `target_allocation_mode=inverse_volatility`가 선택 종목의 목표 금액을 rolling 실현 변동성 역수로 배분하고 sleeve weight 상한을 적용한다. `--candidate-family target_weight_volatility_budget --top-n 200` full sweep에서 유동성 통과 164개, benchmark coverage 100% 기준 best=`target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35`가 return +78.77%, raw excess +46.88%p, exposure-matched excess +63.79%p, Sharpe 0.81, PF 1.96, MDD -18.96%, trades 107로 `provisional_paper_candidate` 통과. sectorcap2+volbudget60 후보도 return +77.18%, raw excess +45.29%p, MDD -19.19%로 통과했고, volbudget90 후보는 benchmark excess Sharpe<=0 병목. 하지만 canonical top-20 재평가에서는 volbudget60 후보가 return +170.73%, benchmark excess +21.34%p, Sharpe 1.44, PF 4.53, turnover/year 982.6%, WF positive/Sh+ 100%였으나 MDD -20.82%로 `paper_only`; sectorcap2+volbudget60도 return +173.80%, benchmark excess +24.41%p, Sharpe 1.46, PF 4.68, turnover/year 978.1%, WF positive/Sh+ 100%였으나 MDD -20.89%로 `paper_only`. 초과수익과 회전율은 유지됐지만 canonical MDD가 더 악화되어 paper/live 승격 대상은 아니며, 현재 배분 산식은 research backtest 경로에만 있으므로 향후 다시 통과 후보가 생기면 paper pilot 전 plan builder 이식과 params_hash 일치 검증이 필요하다. 다음은 포지션별 손실 감산 또는 더 보수적인 drawdown guard 결합으로 MDD를 -20% 안쪽에서 안정화하는 쪽 |
| Strategy Ablation Test (전략별 단독 성과 비교) | 높음 | **C-4/C-5 단독·sleeve 비교 완료** |
| 비용 반영 전/후 성과 비교 리포트 자동화 | 중간 | 완료 — `backtest.cost_impact`가 수수료·세금·슬리피지 explicit cost를 표준 집계하고, 단일/포트폴리오 백테스트 metrics와 txt/html 리포트에 비용 차감 전 추정 수익률, 비용 차감 후 수익률, 비용 드래그(bp), 비용/순손익, cost impact status를 자동 노출한다. 비용 때문에 gross profit이 net loss로 뒤집히거나 비용이 순이익을 초과하면 fail/warn으로 표시해 과매매·고회전 후보 착시를 줄임 |
| 월별 성과 분해 | 중간 | **C-5 반기별 분해 구현 완료** |
| 유동성 필터 (일평균 거래대금 기준 종목 제외) | 높음 | **완료** — watchlist 진입 대상, 포트폴리오 백테스트 입력 universe, research candidate sweep universe에 20일 평균 거래대금 하한 필터 적용. `OrderExecutor` 일반 BUY와 고정수량 paper BUY는 주문 직전 `avg_daily_volume`이 누락/0이거나 20일 평균 거래대금 하한 미달이면 fail-closed 차단한다. target-weight pilot 주문 전 평균 거래대금 대비 주문 비율 preflight도 유지 |
| Target-weight 비용 반영 pre-trade risk | 높음 | 완료 — target-weight pilot 실행 전 수수료/세금/동적 슬리피지 반영 예상 체결가로 현금 부족과 분산/현금/투자비중 한도를 점검. readiness audit, execute, session artifact, evidence snapshot에 `pre_trade_risk_check` 기록 |
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
| Safety Regression CI 확대 | 높음 | **완료 — PR/main 안전 회귀에 `test_audit_safety.py`와 `test_critical_fixes.py`를 포함해 live hard gate, 긴급 청산 확인 플래그, 데이터 소스, WebSocket gap 임계값 회귀를 자동 검증** |
| 벤치마크 거래비용 반영 | 높음 | **완료 — `_buy_and_hold_metrics`에 commission/tax/slippage 적용** |
| Paper Evidence 수집 체계 | 높음 | **완료 — `core/paper_evidence.py` 일별 22개 지표, 6 anomaly rule, 9 approval gate** |
| Paper Evidence canonical 정렬 | 높음 | 완료 — append-only JSONL에서 같은 날짜의 최신 record만 canonical로 유지하고 날짜순으로 반환. backfill/finalize/shadow append 순서와 무관하게 freshness, 최근 10일, promotion period가 실제 날짜 기준으로 계산됨 |
| Paper Evidence 최신성 승격 gate | 높음 | 완료 — promotion package에 `earliest_evidence_date`/`latest_evidence_date`를 기록하고, live_candidate 판정은 canonical 평가 기준 최신 evidence age 14일 이내만 허용한다. 날짜 누락, 미래일, 14일 초과 package는 fail-closed로 막아 오래된 60영업일 증거 재사용을 차단 |
| Live gate Paper Evidence 최신성 검증 | 높음 | 완료 — `core/live_gate.py`가 live 진입 직전에 promotion evidence의 `latest_evidence_date` 또는 `period` 종료일을 확인한다. 최신 증거가 14일을 초과하거나 날짜가 누락/미래이면 promotion_result가 live_candidate여도 실전 전환을 차단 |
| Live gate Paper Evidence source 정합성 | 높음 | 완료 — live 진입 직전 `source_records`를 daily evidence JSONL에서 재계산하고, package의 `earliest_evidence_date`/`latest_evidence_date`/`promotable_evidence_days`가 재계산된 first/last/count와 일치해야 통과한다. payload hash만 맞춘 수동 편집 package가 승격 일수나 기간을 부풀리는 경로를 차단 |
| Live gate 승격 재계산 검증 | 높음 | 완료 — `core/live_gate.py`가 `promotion_result.json`의 status를 확인한 뒤 canonical metrics와 paper evidence를 `promotion_engine`으로 다시 로딩해 `promote()` 결과를 재계산한다. 파일상 live_candidate라도 현재 승격 규칙의 MDD/PF/WF/turnover/benchmark/evidence 조건을 통과하지 못하면 실전 전환을 fail-closed 차단 |
| Check-only canonical 최신성 검증 | 높음 | 완료 — `tools/evaluate_and_promote.py --check-only`가 live gate와 같은 7일 기준으로 `run_metadata.generated_at`을 확인한다. 오래된 canonical bundle은 blocker/current blockers 동기화가 맞아도 운영 점검 OK로 표시하지 않고 `--canonical` 재실행을 요구 |
| Check-only paper evidence 경고 정리 | 중간 | 완료 — invalid `promotion_evidence_{strategy}.json`은 승격 입력에서 계속 제외하되, `--check-only`가 log spam 대신 전략별 구조화된 WARN으로 package 재생성/격리 필요성을 한 번만 노출한다 |
| Invalid paper evidence package 격리 CLI | 중간 | 완료 — `tools/evaluate_and_promote.py --paper-evidence-quarantine-invalid --dry-run`으로 invalid promotion evidence package 격리 대상을 확인하고, 실제 실행 시 `reports/paper_evidence/_invalid_packages/`로 이동한다. `--check-only` WARN에서 바로 실행할 정리 명령을 안내해 오래된 package가 운영 입력 경로에 남는 시간을 줄인다 |
| Promotion paper reference date 정합성 | 높음 | 완료 — artifact-driven promotion 재계산은 paper evidence freshness를 wall-clock이 아니라 canonical `run_metadata.generated_at` 기준으로 계산한다. 같은 canonical bundle을 며칠 뒤 재생성해도 paper evidence age가 흔들려 promotion_result가 달라지는 경로를 차단 |
| Live 전략 상태 emergency disable | 높음 | 완료 — `run_live_trading()`과 live `Scheduler`가 전략 상태 레지스트리의 live 미허용을 warning으로 넘기지 않고 fail-closed 차단한다. canonical live gate가 통과해도 운영자가 registry 상태를 낮춘 전략은 실전 시작 불가 |
| Paper excess 승격 기준 정합성 | 높음 | 완료 — `promotion_engine` live_candidate 조건에 same-universe excess > 0과 cash-adjusted excess > 0을 모두 요구한다. live gate와 promotion_result 기준을 맞춰 현금 보정 기준으로 손실인 paper evidence가 live 후보로 표시되는 경로를 차단 |
| Paper Evidence 전략 식별 검증 | 높음 | 완료 — `promotion_evidence_{strategy}.json` 내부 `strategy`가 현재 전략명과 정확히 일치하지 않으면 promotion engine, promotion result 생성, live gate가 해당 package를 승격/라이브 증거로 쓰지 않는다. 전략명 누락 또는 다른 전략 evidence 재사용을 fail-closed로 차단 |
| Promotion evidence package 무결성 검증 | 높음 | 완료 — `promotion_engine`이 `package_integrity.schema_version`과 `payload_hash`를 재계산 검증한다. 수동 편집·충돌 해결·부분 재생성으로 payload와 hash가 어긋난 package는 승격 입력으로 쓰지 않아 오염된 paper evidence가 live_candidate 근거가 되는 경로를 차단 |
| Legacy evidence E2E 정리 | 높음 | 완료 — v1 helper API 기반 `tests/test_evidence_e2e.py`를 v2 smoke/E2E로 교체하고 scheduler의 deprecated v1 collector 호출 제거 |
| Paper Runtime State Machine | 높음 | **완료 — `core/paper_runtime.py` 5개 상태(normal/degraded/frozen/blocked/research_disabled), schema quarantine** |
| Paper runtime registry 오류 차단 | 높음 | 완료 — legacy `reports/approved_strategies.json`가 JSON 파싱 실패 또는 스키마 오류 상태면 paper runtime을 `research_disabled`로 fail-closed 처리. 신규 entry/shadow는 막고 exit/cancel/reconcile/finalize/evidence/reporting만 허용 |
| Paper Pilot Authorization | 높음 | **완료 — `core/paper_pilot.py` launch readiness + pilot auth + 리스크 캡 + fail-closed/audited entry guard. pilot auth 생성 전 `valid_from/valid_to` 형식과 기간 순서, max orders/positions/notional/exposure 양수 cap을 검증하고, malformed enabled auth record는 active pilot으로 쓰지 않는다. pilot auth 생성 뒤 전략 상태나 artifact eligibility가 내려가도 entry 직전에 eligibility를 재검증해 기존 승인만으로 신규 pilot 진입이 열리지 않게 한다** |
| Paper Preflight Check | 높음 | **완료 — `core/paper_preflight.py` 운영 준비 상태 점검** |
| Strategy Universe Registry | 높음 | **완료 — `core/strategy_universe.py` paper 대상 전략 canonical 목록** |
| Zero-return Semantics (deadlock 해소) | 높음 | **완료 — cash-only/no-position day에서 daily_return=0.0 추론, benchmark final 가능** |
| Paper 운영 도구 (tools/) | 높음 | **완료 — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI. `tools/paper_pilot_control.py`와 `tools/paper_runtime_status.py`는 액션 옵션을 한 번에 하나만 허용하고, 상충 액션 동시 지정 시 상태 조회·파일 기록 전에 fail-closed로 종료. `tools/paper_bootstrap.py`는 빈 watchlist, 빈/역전 날짜 범위, 요청일 canonical evidence 미생성을 incomplete로 보고 non-zero 종료** |
| Basket rebalance paper BUY 실행 경로 | 중간 | 완료 — `BasketRebalancer.execute()`의 paper BUY가 존재하지 않는 포트폴리오 메서드 대신 `get_current_capital()`/`get_available_cash()`를 사용한다. 실행 전 자본·현금 값이 `OrderExecutor.execute_buy_quantity()`에 전달되는 회귀 테스트로 고정 |
| Live basket rebalance 승인 단위 정합성 | 높음 | 완료 — live 바스켓 리밸런싱은 CLI와 스케줄러 자동 체크 모두 바스켓별 `basket_rebalance:<basket>`을 live gate, account_key, 주문 strategy tag에 동일하게 사용해야 한다. 일반 전략 gate 통과 후 `basket_rebalance` 기본 태그/계좌로 주문이 나가는 경로를 차단하고, 실행 전 KIS↔DB 포지션 동기화 실패 시 주문 계획/실행 전에 중단 |
| Entry filter 탐색 (market filter, abs momentum, cooling) | 중간 | **완료 — 모두 NO_MEANINGFUL_IMPROVEMENT 또는 ADVERSE EFFECT. 현행 유지** |
| 운영/백테스트 시장국면 설정 동기화 | 높음 | 완료 — `trading.market_regime_*`를 canonical 설정으로 두고 `resolve_market_regime_config()`로 paper/live와 백테스트가 같은 파라미터를 보게 했다. `backtest_regime_filter`는 명시 값만 실험용 override로 사용하며, 백테스트도 MA(20/60) 데드크로스 신호를 반영한다. 운영 `market_regime_filter=true`에서 지수 조회 실패, 빈 데이터, MA 계산 실패가 발생하면 `unknown` 국면으로 신규 BUY를 fail-closed 차단한다. 기본값은 검증 결과에 맞춰 OFF |
| Earnings filter 조회 실패 fail-closed | 높음 | 완료 — `earnings_filter`가 yfinance와 DART 모두에서 실적일을 확인하지 못하면 기본 `trading.earnings_filter_unknown_policy=block`으로 신규 BUY를 차단한다. `allow`는 운영자가 조회 불가 상태를 감수한다고 명시한 경우에만 허용하며, `OrderExecutor`는 현재 설정 객체를 필터에 전달해 paper/live 주문 전 같은 정책을 적용한다 |
| Research sweep exposure-matched benchmark | 중간 | **완료 — 후보별 평균 노출/현금비중과 exposure-matched B&H excess 진단 추가. promotion gate는 raw benchmark 기준 유지** |
| Target-weight top-N rotation backtester | 높음 | **완료 — 월간 직전일 점수 기준 top-N 목표비중 리밸런싱, 다음 거래일 시가 체결, 종가 평가, delta 거래비용, 노출 진단 구현. score day 종가 stale 종목은 후보에서 제외하고, 보유 종목 stale 평가는 별도 metrics로 기록하며, 보유 종목 시가 누락 리밸런싱은 거래 없이 skip 기록. 벤치마크 stale과 신규 top-N 후보 시가 누락은 fail-closed. 기존 종가 체결 기반 target-weight research artifact는 재생성 또는 execution price mode 확인 후 사용** |
| Target-weight research next-open execution guard | 높음 | 완료 — 리서치 백테스트의 리밸런싱 체결가를 당일 종가에서 원본 `open` 기반 다음 거래일 시가로 변경하고, trade/metrics에 `execution_price_mode=next_open`, `execution_price_freshness_checked`, `avg_volume_lookback_lag_days=1`을 기록 |
| Target-weight score-floor 후보 | 중간 | **완료 — `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금으로 남김. best=`target_weight_rotation_top5_60_120_floor0`, +210.21%/Sharpe 1.41/WF positive 100%였으나 turnover/year=1081.5%라 turnover-aware 변형으로 후속 검증** |
| Target-weight rank-hysteresis 후보 | 높음 | **완료 — `hold_rank_buffer`로 보유 종목이 top-N 밖으로 소폭 밀려도 버퍼 안이면 유지. best=`target_weight_rotation_top5_60_120_floor0_hold3`, +278.57%/Sharpe 1.65/WF positive 100%/turnover 807.8%. 남은 병목은 MDD=-28.25%** |
| Target-weight shadow proof | 높음 | **완료 — dry-run plan을 `shadow_bootstrap` evidence로 기록하되 `execution_backed=False`, excess=null로 유지해 promotion을 오염시키지 않음. `--shadow-days` 기반 자동 날짜 선택은 N개 평일이 아니라 N개 고유 resolved trade_day를 목표로 과거 평일을 보충 스캔하며, 명시적 날짜 범위 batch도 지원. 목표 미달/날짜별 실패는 CLI non-zero 종료로 자동화가 불완전한 shadow proof를 성공 처리하지 못하게 차단. artifact와 runbook에 cap preview, plan 기반 최소/추천 cap, enable 명령, launch artifact 경로를 기록해 기본 pilot cap 부족(주문 수/포지션/1건 금액/총노출), clean day 부족, notifier/auth 누락을 사전 확인** |
| Target-weight readiness audit | 높음 | **완료 — `--readiness-audit`로 주문 제출/evidence 기록 없이 capped pilot 직전 상태를 JSON artifact와 Markdown 운영 리포트로 점검. clean shadow/launch readiness, active pilot auth와 cap validation, 추천 cap, 중복 session idempotency, 실행일/장 시간, 실행 전 position drift, 데이터 품질, 유동성 preflight, 비용 반영 pre-trade risk, 다음 조치를 함께 판정하고 shadow 수집/audit 재실행/추천 cap 승인/capped paper 실행 명령을 남긴다. 실행 준비/장 대기 상태는 실행일/장 시간 점검과 pilot authorization snapshot이 `checked=True`로 통과한 경우에만 표시해 미점검 placeholder를 실행 가능 상태로 오인하지 않게 한다. `READY_TO_EXECUTE`가 아니면 readiness/daily ops/manifest의 capped paper 실행 명령은 `# blocked:`로 고정해 blocker 해소 전 paper 실행을 운영 리포트가 유도하지 않는다. cap 승인 준비가 안 됐거나 장 외 실행이면 non-zero 종료** |
| Target-weight preflight/runbook 전용 안내 | 중간 | 완료 — preflight와 pilot runbook이 target-weight 후보에서 generic `paper_bootstrap.py`/`run_paper_evidence_pipeline.py`를 pilot 증거 경로처럼 안내하지 않고, `target_weight_rotation_pilot.py --shadow-days`, `--daily-ops-summary`, `--readiness-audit`, `paper_preflight.py --send-test-notification` 순서로 운영 명령을 제시한다. backfill/shadow/pilot_paper 증거를 혼동해 승격 불가능한 기록을 60일 증거로 착각하는 운영 리스크를 낮춘다 |
| Target-weight pilot 실행락 | 높음 | 완료 — `--execute` 주문 제출 전 same-candidate/trade-day atomic `.lock`을 선점하고, in-progress lock이 있으면 주문 제출·포지션 조회·session 저장·evidence 수집 전 fail-closed 차단한다. 정상 종료 후 session artifact가 남으면 lock을 해제하고, 이전 session이 주문 제출 도달/실행 수량/실패 주문/중단 흔적을 남기면 `--allow-rerun`도 차단해 중복 주문과 불확실한 부분 실행 재시도를 막음 |
| Target-weight cash override 차단 | 높음 | 완료 — `--cash`는 dry-run 계획 검토와 shadow bootstrap 보조 입력으로만 허용하고, `--readiness-audit`, `--daily-ops-summary`, `--execute`, `--collect-evidence`에서는 실제 paper 계좌 현금 기준만 사용하도록 CLI와 함수 호출 모두 `target_weight_cash_override_blocked`로 fail-closed 차단 |
| Target-weight evidence 단독 수집 차단 | 높음 | 완료 — `--collect-evidence`는 `--execute --collect-evidence` 조합에서만 허용한다. 단독 `--collect-evidence`가 dry-run처럼 0으로 종료돼 자동화가 pilot evidence 수집 성공으로 오해하는 경로를 CLI와 함수 호출 모두 `target_weight_collect_evidence_requires_execute`로 fail-closed 차단 |
| Target-weight preflight/notifier 동기화 | 높음 | 완료 — readiness audit 시작 시 paper preflight를 먼저 갱신해 `preflight_refresh` artifact를 남긴다. Discord webhook 미설정이나 notifier health 비정상은 pilot auth/cap 상태와 별개로 주문 전 `BLOCKED` 처리하며, `.env`의 `DISCORD_WEBHOOK_URL` 설정 후 preflight/audit 재실행이 필요 |
| Paper preflight/readiness 실패 종료 보강 | 중간 | 완료 — `tools/paper_preflight.py`와 `tools/paper_launch_readiness.py`는 `--strategy`와 `--all`을 동시에 받으면 DB 초기화와 artifact 생성 전에 argparse 단계에서 실패한다. preflight는 overall fail 또는 entry 차단이면 non-zero로 종료하고 launch readiness는 READY가 아니면 non-zero로 종료한다. launch readiness 계산 예외도 preflight fail check로 남겨 자동화가 실패 상태를 성공으로 오인하지 않게 한다 |
| Target-weight pilot enable guard | 높음 | **완료 — `tools/paper_pilot_control.py --enable`이 canonical `strategy_specs[].base_strategy=target_weight_rotation`을 우선 기준으로 target-weight 후보를 식별하고, 기존 `target_weight_*` 접두어는 호환 fallback으로만 사용한다. pilot 제어 액션은 단일 명령에서 하나만 허용해 `--enable`과 다른 액션을 같이 주는 경우 auth 기록 전에 차단한다. target-weight 후보 승인 전에는 readiness audit을 재실행하고, 운영자가 요청한 cap이 현재 plan/launch readiness/유동성 preflight/비용 반영 pre-trade risk를 만족할 때만 pilot auth를 기록. 요청 cap은 audit의 추천 cap과 정확히 일치해야 하므로, 통과 가능한 더 큰 cap으로 pilot 범위를 넓히는 경로도 차단한다. 승인 auth에는 plan의 trade day/as-of date/params hash/targets/시작·목표 수량 snapshot을 함께 저장한다. 추천 cap 미충족, stale plan, audit blocker가 있으면 승인 자체를 fail-closed 차단** |
| Target-weight cap validation artifact | 중간 | **완료 — `run_pilot(execute=True)`가 pilot cap validation에서 막혀도 주문 제출 전 session JSON artifact에 차단 사유와 skipped order를 기록. runtime pilot session/evidence/fill reconciliation은 쓰지 않아 cap 수정 후 같은 trade_day를 다시 점검 가능** |
| Target-weight promotion proof guard | 높음 | **완료 — promotion package가 target-weight 계열 strategy의 promotable day를 `execution_backed=True`인 verified `pilot_paper` execution proof로만 계산. `pilot_authorized`, 승인 snapshot 일치, target-weight plan/execution params hash 일치, 실행 전 포지션 검증, liquidity/pre-trade risk, 주문 수량/결과 complete, order result reconciliation, fill reconciliation, 사후 position reconciliation이 모두 필요하며, `evaluate_and_promote` live_candidate 판정과 live gate 모두 proof summary 누락/invalid day/params hash drift를 fail-closed 차단** |
| Target-weight canonical evidence 선택 가드 | 높음 | 완료 — 같은 날짜의 verified `pilot_paper`/`real_paper` 증거가 나중에 추가된 backfill/shadow record에 canonical view에서 밀리지 않게 보호한다. target-weight proof shape가 있는 record는 `_target_weight_record_proof_status()` 기준으로 verified pilot evidence가 우선되므로, `promotion_eligible=False` 또는 `performance_repair=True`인 수리 record가 기존 검증 완료 pilot 증거를 가려 승격 일수를 줄이는 경로도 차단한다 |
| Target-weight canonical hash gate | 높음 | 완료 — target-weight `live_candidate` 판정에서 paper evidence package의 `target_weight_params_hash`가 canonical promotion bundle의 `strategy_specs[].params_hash`와 일치해야 한다. target-weight 후보 식별은 canonical `strategy_specs[].base_strategy=target_weight_rotation`을 우선 사용하고 prefix는 호환 fallback으로만 유지한다. package 생성 단계도 같은 metadata를 참조해 prefix 없는 target-weight 후보에 verified pilot proof와 canonical hash 일치를 요구하므로, 파라미터가 바뀐 뒤 예전 60일 pilot evidence가 현재 후보 승격에 재사용되거나 일반 paper evidence만으로 live 후보가 되는 경로를 fail-closed 차단 |
| Target-weight 기존 pilot evidence 재사용 검증 | 높음 | 완료 — `collect_daily_evidence()`가 중복 기록으로 `None`을 반환해 기존 `pilot_paper` evidence를 재사용할 때, candidate/date/params hash뿐 아니라 target list, 시작 수량, 목표 수량, target exposure, gross exposure, 최대 주문금액이 현재 plan과 일치해야 `already_recorded`로 인정한다. 손상된 수량 필드는 예외가 아니라 mismatch 사유로 남겨 fail-closed 처리한다 |
| Target-weight daily ops 실행 최신성 가드 | 높음 | 완료 — `READY_TO_EXECUTE`, `READY_TO_ENABLE_CAPS`, `WAITING_FOR_MARKET_SESSION`처럼 다음 액션이 실행/승인으로 이어질 수 있는 daily ops summary는 `trade_day`뿐 아니라 `generated_at`도 KST 기준 현재일·30분 이내여야 한다. 누락/형식 오류/미래 시각/30분 초과 요약이면 `paper_pilot_control.py --status`와 `evaluate_and_promote.py --current-blockers` 양쪽에서 operator command를 `# blocked:`로 정규화하고 daily ops summary 재생성을 요구한다 |
| Target-weight finalize 성과 진단 | 중간 | 완료 — `--finalize-pilot-evidence`가 `total_value`/`daily_return` 미확정으로 막힐 때 finalize report에 source record 성과 필드 보유 현황, 실제 사용 가능한 필드와 값은 존재하지만 쓸 수 없는 필드, portfolio metrics probe 수행 여부, probe에서 확인한 필드, 최종 누락 필드를 남긴다. probe는 당일 snapshot 존재 여부, 직전 snapshot 존재 여부와 시각, 당일/직전 이후 거래 수, `missing_snapshot_history`/`missing_current_snapshot_after_trades` 같은 원인 코드를 함께 남겨 portfolio 성과 snapshot이 왜 아직 확정되지 않았는지 바로 추적하게 한다. current blockers와 `paper_pilot_control.py --status`도 이 진단과 복구 힌트를 표시해 snapshot 이력 복구, 장마감 snapshot capture, final portfolio evidence 대기 중 어떤 조치가 필요한지 구분한다. 구버전 finalize report처럼 성과 진단 필드가 없으면 current blockers/status가 no-order finalize diagnostics refresh를 먼저 안내해 기존 리포트의 진단만 갱신한 뒤 실제 성과 snapshot 대기 상태를 판단하게 한다. finalize CLI 실패 출력도 artifact/report 경로와 missing performance fields, 대기 후 재실행 안내를 직접 표시한다. `target_weight_benchmark_status_not_final`, `target_weight_excess_metrics_missing`, `target_weight_daily_return_missing`, `target_weight_portfolio_value_missing`은 promotable finalize 가능성이 있는 사유로 취급해 repair보다 finalize를 먼저 안내한다 |
| Canonical benchmark coverage 승격 gate | 높음 | 완료 — `evaluate_and_promote`가 promotion_result를 계산하기 전에 canonical metadata integrity를 검증하고, artifact-driven promotion은 benchmark excess return/Sharpe 양수를 요구한다. benchmark coverage 누락, material fetch error, 평가 오류, benchmark excess 누락/0 이하가 있으면 provisional/live 승격이 생성되지 않는다 |
| Promotion metrics loader 원천 동기화 | 높음 | 완료 — `core.promotion_engine.load_metrics_from_artifact()`와 `load_promotion_artifact()`가 `metrics_summary.json`의 `benchmark_excess_*`/`wf_*` 값과 `benchmark_comparison.json`, `walk_forward_summary.json` 원천값을 비교해 불일치하면 빈 metrics 또는 `None`을 반환한다. `load_promotion_artifact()`는 저장된 `promotion_result.json`도 현재 metrics/evidence 기준 `promote()` 결과의 status/allowed_modes/reason과 다시 비교한 뒤 반환한다. live gate의 승격 재계산, artifact 기반 promotion 조회, adapter-only paper pilot eligibility가 stale metrics/promotion_result를 우선 신뢰하지 못하게 fail-closed 처리한다 |
| Promotion blocker summary | 중간 | 완료 — canonical promotion 실행 시 `promotion_blocker_summary.json/md`를 생성하고, `tools/evaluate_and_promote.py --blocker-summary`로 기존 artifact에서 요약을 재생성한다. 단, `--blocker-summary`와 public `load_promotion_blocker_summary_from_artifacts()` 모두 저장된 `promotion_result.json`이 metrics/evidence/metadata 재계산 결과와 일치할 때만 summary 생성을 허용한다. 내부 검증 비교처럼 의도적으로 원본 artifact만 읽어야 하는 경우는 `validate=False`를 명시해야 한다. 요약은 source artifact hash를 포함하고 `--blocker-summary-check`와 `--check-only`가 `promotion_result/metrics/run_metadata` 동기화뿐 아니라 metrics/evidence/metadata 기준 promotion_result 재계산 결과까지 fail-closed 검증한다. 재계산 전 `metrics_summary.json`의 `benchmark_excess_*`/`wf_*` 값이 `benchmark_comparison.json`, `walk_forward_summary.json` 원천값과 일치하는지도 확인하므로 stale metrics만으로 promotion을 갱신하는 경로를 차단한다. `--promotion-artifacts-refresh`는 기존 metrics/evidence/metadata에서 `promotion_result.json`, `metrics_summary.json`의 paper 필드, blocker summary, current blockers를 한 번에 재생성한다. 전략별 status, 주요 blocker, 핵심 metrics, 체결 품질 blocker, 다음 운영 조치를 요약해 긴 `promotion_result.reason`을 직접 해석하지 않아도 paper evidence 갱신, benchmark 재평가, target-weight proof 보강, 체결 왜곡 재점검 등 다음 작업을 바로 확인 가능. next action 분류는 live 차단 사유의 paper evidence/target-weight pilot proof/체결 품질 미충족을 benchmark 재생성보다 우선해 provisional 후보의 실제 운영 순서를 잘못 안내하지 않게 한다 |
| Current blockers live gate 연동 | 높음 | 완료 — canonical promotion 실행 시 `reports/current_blockers.json`을 함께 갱신하고, live gate가 `promotion_blocker_summary.json` source hash 재계산값과 current blockers의 `source_artifact_hash`/`promotion_summary`/`go_live`/`live_candidates`/`hard_blockers`/`operator_runbook`을 확인한다. 운영 요약이 NO-GO이거나 stale이면 `promotion_result`와 paper evidence가 좋아 보여도 live 진입을 fail-closed 차단. schema v3 current blockers는 next action마다 실제 명령과 `order_safety`를 포함해 no-order 점검, readiness audit, READY_TO_EXECUTE 당일 capped paper 증거 수집 순서를 운영자가 바로 확인하게 한다. 최신 target-weight daily ops summary가 없으면 daily ops summary 생성을 1순위로 두고, 최신 summary가 있으면 shadow 3일 수집, Discord test preflight, 추천 cap 승인, READY_TO_EXECUTE 실행 중 현재 blocker에 맞는 명령을 1순위 next action과 runbook current priority에 반영한다. pilot evidence 확정 리포트가 `total_value`/`daily_return` 부족으로 막힌 경우에는 finalize 명령을 즉시 재실행하라고 안내하지 않고, final portfolio performance evidence 대기 후 scheduled priority command를 다시 실행하도록 표시한다. Discord webhook 미설정은 단순 test-send가 아니라 `DISCORD_WEBHOOK_URL` 설정 후 preflight 실행이 필요한 액션으로 구분하고, READY가 아닌 summary의 실행 명령은 current blockers에서 `# blocked:`로 정규화한다. active target-weight pilot이 있으면 current blockers가 핵심 진입 점검 상태를 `operator_runbook.core_entry_check`에 기록하고, 알림 도달성 stale/unverified/missing은 DB 복구 등 현재 우선순위를 유지한 채 no-order 사전점검 재실행 액션으로 추가 노출한다. `--current-blockers`와 `--current-blockers-check`도 먼저 `promotion_blocker_summary.json`이 source artifact와 동기화됐고 저장된 `promotion_result.json`이 현재 metrics/evidence 재계산 결과와 일치하는지 확인하므로, 오래된 promotion_result에서 summary/current blockers만 재생성해 통과시키는 경로를 차단한다. `--check-only`도 기본 운영 점검에서 metrics/WF/benchmark 원천 동기화와 current blockers 누락/stale을 함께 확인한다. live gate도 blocker summary와 current blockers를 저장된 `promotion_result`가 아니라 전체 promotion artifact의 현재 재계산 결과로 다시 만든 값과 비교해, 대상 전략 외 다른 전략의 수동 승격 조작이나 stale 운영 파생 필드가 있어도 실전 진입을 막는다 |
| Snapshot DB 복구 후보 패키지 | 중간 | 완료 — target-weight portfolio snapshot diagnostics가 DB persistence proof 누락으로 복구가 필요한 경우에만 artifact 기반 trade_history/positions 후보 CSV와 manifest를 생성한다. 패키지는 `candidate_only`, `db_write_enabled=false`, `requires_authoritative_confirmation=true`를 명시해 자동 DB 반영이나 artifact-only snapshot 생성으로 오용되지 않게 하며, positions 후보에서는 0주 이하 수량을 제외한다. `--prepare-db-restore-review-bundle`은 후보 CSV 사본과 빈 reviewed authoritative CSV 템플릿, 검토 checklist/verify 명령을 no-write로 생성해 candidate row를 원장 증거로 오인하지 않게 한다. current blockers/status는 review bundle 생성 전에는 bundle 명령을, 생성 후에는 authoritative 템플릿 경로와 verify 명령, 실제 reviewed CSV 파일에서 다시 읽은 행 수/빈 템플릿/누락 컬럼 진단을 1순위로 노출한다. `--verify-db-restore-package`는 manifest 해시·CSV 행 수·현재 DB 상태·reviewed authoritative CSV 일치 여부를 no-write로 검증하고, 빈 reviewed 템플릿은 `authoritative_*_csv_empty_template`, 비교 컬럼 누락은 `authoritative_*_csv_columns_missing`, 행 수 불일치는 `authoritative_*_csv_row_count_mismatch`, 값 불일치는 `authoritative_*_csv_content_mismatch`로 나눠 표시한다. 최신 검증 상태와 blocker를 표시해 복구 담당자가 authoritative 원장 대조를 이어갈 수 있다 |
| Paper 체결 품질 리포트 | 중간 | 완료 — `tools/paper_trade_quality_report.py`가 paper `TradeHistory`의 `expected_price`, `price_gap`, `actual_slippage_pct`, `slippage`, `execution_session_id`, `order_id`를 집계해 불리한 체결 갭 비용, expected_price 누락, 주문/실행 세션 연결 누락, 일자/종목별 체결 품질을 JSON/Markdown으로 기록한다. 기본 CLI는 `review`와 `no_trades`를 non-zero로 종료하고, 체결 0건 리포트만 확인하려면 `--allow-no-trades`를 명시해야 한다. `generate_promotion_package()`는 같은 기간 `trade_quality`를 함께 저장하고 no_trades, expected_price 누락, 주문/실행 세션 연결 누락, evidence `total_trades` 및 BUY/SELL 합계와 TradeHistory 체결 구성 불일치, BUY/SELL 외 action 또는 50bp 초과 불리한 체결 갭을 `BLOCKED` 사유로 반영한다. `promotion_engine`도 `paper_trade_quality_status=ok`를 live 후보 조건으로 재확인해 paper evidence의 수익성과 증거 연결성이 실제 체결 왜곡을 견디는지 승격 전 운영 검토 가능 |
| Test artifact quarantine | 중간 | 완료 — `tools/quarantine_test_artifacts.py`가 `reports/paper_evidence`, `reports/paper_runtime`, `reports/promotion`에서 test/demo 전략 artifact를 감지해 `_quarantine`으로 이동한다. promotion JSON 내부 top-level 전략명과 `strategy`/`candidate_id` payload도 검사하며, paper preflight가 발견 건수와 정리 명령을 WARN으로 남겨 테스트 산출물이 canonical promotion/operator view에 섞이는 위험을 줄임 |
| Target-weight 실행 증거 E2E 회귀 | 높음 | 완료 — readiness audit → cap 승인 snapshot → capped paper 실행 증거 → promotion package → promotion engine → live gate까지 같은 params hash와 verified pilot proof가 끊기지 않는지 CI 테스트로 고정 |
| Target-weight liquidity preflight | 높음 | **완료 — 목표비중 plan 생성 시 주문 종목별 최근 20일 평균 거래대금을 diagnostics에 기록하고, `--readiness-audit`와 `--execute`가 주문별 평균 거래대금 대비 notional 비율을 확인. 기본 5% ADV 초과 또는 유동성 데이터 누락은 `target_weight_liquidity_preflight_failed`로 fail-closed 차단하며 session/readiness/pilot evidence snapshot에 결과를 남김** |
| Backtest/research liquidity universe filter | 높음 | **완료 — `WatchlistManager.liquidity_filter_report()`를 공통 진단 API로 분리하고, `PortfolioBacktester.run()`과 `tools/research_candidate_sweep.py`가 평가 시작일 기준 20일 평균 거래대금 하한 미만·strict 데이터 누락 종목을 universe에서 사전 제외** |
| Portfolio dynamic slippage | 높음 | **완료 — `PortfolioBacktester`가 종목별 20일 평균 거래량을 매수/매도 거래비용 계산에 전달하고 `participation_rate`, `slippage_multiplier`, `slippage_cost`를 거래 기록에 남김** |
| Target-weight research dynamic slippage | 높음 | **완료 — target-weight 리서치 백테스터가 종목별 20일 평균 거래량을 매수/매도 거래비용 계산에 전달하고 `avg_daily_volume`, `participation_rate`, `slippage_multiplier`, `slippage_cost_total` 진단값을 artifact metrics에 남김** |
| Paper/live 체결가 기준 현금 정산 | 높음 | 완료 — paper BUY는 예상 체결가 기준으로 수량·방어 가격을 산정하고, paper BUY/SELL은 모델 execution_price를 체결가로 기록한다. DB 현금 요약은 이미 체결가에 반영된 슬리피지를 다시 차감하지 않는다. slippage 필드는 비용 진단과 리포트용으로 유지 |
| 운영 손실 한도 신규 진입 차단 | 높음 | 완료 — `OrderExecutor` 주문 전 검사에서 `drawdown.max_portfolio_mdd`와 `max_daily_loss`를 확인해 신규 BUY만 fail-closed 차단한다. live 모드에서 KIS 잔고가 확인되지 않아 `PortfolioManager.get_portfolio_summary()`가 DB fallback을 사용한 경우에는 DB 평가금액으로 손실 한도를 판단하지 않고 신규 BUY를 차단한다. 일일 손실은 최근 포트폴리오 스냅샷 대비 평가금액 하락률로 본다. SELL/exit는 손절·청산 경로라 계속 허용 |
| Paper Evidence MDD 정규화 | 높음 | 완료 — PortfolioSnapshot은 MDD를 양수 퍼센트로 저장하고 기존 evidence/promotion은 음수 drawdown 기준으로 판정하던 불일치를 정리했다. 수집·finalize·promotion 집계에서 MDD를 `-abs(mdd)`로 정규화해 deep_drawdown anomaly와 `max_mdd` 승격 차단이 fail-open 되지 않게 했다 |
| Research sweep benchmark coverage guard | 높음 | 완료 — EW B&H 벤치마크 입력 universe 일부라도 수집 실패·기간 부족이면 초과수익 계산을 0으로 고정하고 `INSUFFICIENT_BENCHMARK_DATA` decision으로 canonical 평가 진행을 차단 |
| Live 체결 확인 guard | 높음 | 완료 — KIS 주문 ACK 후 체결가·체결수량 조회가 실패하거나 부분체결만 확인되면 예상가 기준 `FILLED` 처리 대신 `ACKED`/`PARTIAL_FILLED` pending으로 남기고 `requires_reconcile=True`로 운영 대조를 요구 |
| Live 체결보류 신규진입 중단 | 높음 | 완료 — 장중 신규 진입 루프에서 live 주문이 접수됐지만 체결 확인이 보류되면 브로커 잔고 동기화 전까지 같은 루프의 남은 신규 BUY 실행을 중단해 미확정 체결분을 무시한 추가 매수를 차단 |
| Live 미완료 주문 상태 영속화 | 높음 | 완료 — live 주문 `SUBMITTED`/`ACKED`/`PARTIAL_FILLED` 상태를 `order_records`에 저장하고, 재시작 또는 OrderGuard TTL 만료 후에도 DB에 미완료 주문이 남아 있으면 같은 종목 신규 주문을 fail-closed 차단 |
| Live 보류 주문 복구 대조 | 높음 | 완료 — 재시작 복구에서 KIS 미체결 조회가 성공했고 DB `ACKED`/`PARTIAL_FILLED` 주문번호가 브로커 미체결 목록에서 사라져도 체결 상세 조회가 확인될 때만 `order_records`를 `RECONCILED`로 닫고 종목별 OrderGuard를 해제. 체결 조회 실패/불명확 상태는 열린 주문과 중복 차단을 유지해 실제 체결 여부가 모호한 상태에서 신규 주문이 열리지 않게 한다 |
| Live 미체결 조회 fail-closed | 높음 | 완료 — KIS 미체결 조회 실패, `rt_cd != 0`, 응답 형식 이상을 주문 가능 상태로 보지 않고 live BUY/SELL을 제출 전 차단 |
| KIS Circuit Breaker HALF_OPEN 제한 | 높음 | 완료 — OPEN 쿨다운 후 `HALF_OPEN`에서 복구 확인용 단일 probe만 허용하고, 성공/실패 콜백 전 추가 요청은 차단한다. 장애 회복 직후 KIS 요청이 한꺼번에 재개되는 경로를 막는 단위 테스트 추가 |
| Auto-entry 시그널 재검증 fail-closed | 높음 | 완료 — 장중 `_execute_entry_candidates()`가 장전/이전 루프 후보를 주문하기 전에 최신 데이터와 전략 신호로 BUY를 재확인한다. 데이터 재조회, API, 전략 계산 오류가 나면 stale 후보로 주문하지 않고 `ENTRY_REVALIDATION_BLOCK` 이벤트를 남긴 뒤 후보를 다음 루프로 보류 |
| KIS 잔고 오류 응답 fail-closed | 높음 | 완료 — 잔고 조회 응답도 `rt_cd == 0`일 때만 정상 잔고로 해석한다. 오류 body는 빈 포지션/0원 잔고가 아니라 조회 실패로 반환해 연결 검증·포지션 동기화가 fail-closed 처리된다 |
| Live 시작 전 연결·잔고 동기화 차단 | 높음 | 완료 — `main.py --mode live`가 canonical live gate를 통과해도 KIS 연결 검증 또는 초기 KIS↔DB 잔고 동기화가 실패하면 실전 스케줄러를 시작하지 않고 종료. 불안정한 브로커 상태에서 live 루프가 뜨는 경로를 fail-closed 처리 |
| Live 긴급 청산 브로커 보유 반영 | 높음 | 완료 — live 설정의 `--mode liquidate`는 `ENABLE_LIVE_TRADING=true`와 `--confirm-live` 확인 후 KIS↔DB 잔고 동기화를 먼저 실행한다. KIS-only 포지션은 DB에 보정한 뒤 청산 대상을 다시 읽고, 동기화 실패가 남으면 stale DB 포지션만으로 청산하지 않고 종료 |
| HTTP 긴급 청산 live 확인 보강 | 높음 | 완료 — `monitoring.liquidate_trigger`는 live 긴급 청산을 `ENABLE_LIVE_TRADING=true`와 별도 `LIQUIDATE_TRIGGER_CONFIRM_LIVE=true`가 있을 때만 확인 플래그로 전달한다. 내부 guard가 `SystemExit`을 내도 HTTP 서버 프로세스를 종료하지 않고 실패 응답으로 변환 |
| HTTP 긴급 청산 실패 결과 전파 | 높음 | 완료 — `run_emergency_liquidate()`가 대상/성공/실패 summary를 반환하고, HTTP 트리거는 개별 매도 실패가 1건이라도 있으면 성공 응답으로 포장하지 않고 실패 응답으로 노출 |
| HTTP 긴급 청산 POST 전용화 | 높음 | 완료 — `/liquidate`는 POST 요청만 청산을 실행하고 GET은 405로 거부한다. 인증은 기본적으로 `X-Token` 또는 `Authorization: Bearer` 헤더만 허용하며, URL query token은 `LIQUIDATE_TRIGGER_ALLOW_QUERY_TOKEN=true`를 명시한 경우에만 허용 |
| HTTP 긴급 청산 로컬 바인드 기본값 | 높음 | 완료 — `monitoring.liquidate_trigger`는 기본적으로 `127.0.0.1`에만 바인드한다. 외부 호출이 꼭 필요할 때만 `LIQUIDATE_TRIGGER_HOST=0.0.0.0`처럼 명시해 노출 범위를 넓힌다 |
| HTTP 긴급 청산 토큰 강도 검증 | 높음 | 완료 — `LIQUIDATE_TRIGGER_TOKEN`은 기본 최소 16자 이상이어야 하며 `secret`, `your_secret`, `changeme` 같은 placeholder 값은 서버 시작과 요청 처리 양쪽에서 거부 |
| 긴급 청산 결과 통합 알림 | 높음 | 완료 — `run_emergency_liquidate()`는 대상/성공/실패 summary를 반환할 뿐 아니라 통합 `Notifier`로 결과를 전파한다. 실패 상세는 최대 5건까지 알림 본문에 포함하고 알림 실패는 청산 결과를 뒤집지 않고 로그에 남김 |
| 빈 KIS 잔고 자동보정 보호 | 높음 | 완료 — KIS 보유 목록이 빈 응답인데 DB 포지션이 남아 있으면 자동보정이 전체 포지션을 삭제하지 않도록 기본 보류. 확실한 무보유 계좌 정리만 `position_mismatch_allow_empty_broker_delete=true`로 명시 허용 |
| 브로커 포지션 자동보정 방어값 복구 | 높음 | 완료 — KIS-only 포지션을 DB에 복구하거나 수량 불일치를 KIS 기준으로 맞출 때 수량을 추가 매수처럼 더하지 않고 절대값으로 보정하며, 평균가 기준 손절·익절·트레일링 스탑을 함께 재생성 |
| Target-weight pre-trade risk | 높음 | **완료 — `RiskManager.calculate_transaction_costs()`를 plan-level로 재사용해 주문별 예상 체결가, 수수료, 거래세, 슬리피지, 선택 양도세를 반영한 projected cash/total value/exposure를 계산. 현금 부족, `min_cash_ratio`, `max_investment_ratio`, `max_position_ratio`, `max_positions` 위반은 `target_weight_pre_trade_risk_failed`로 readiness/execute를 fail-closed 차단하고 session/readiness/pilot evidence snapshot에 cost summary를 남김** |
| Target-weight execution-aware evidence | 높음 | **완료 — `run_pilot(execute=True, collect_evidence=True)`는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, 활성 pilot auth의 승인 snapshot이 현재 plan과 일치하며, KST 실행일과 KRX 정규장 주문 가능 시간을 통과하고, `run_pilot` 점검과 `execute_plan` 주문 제출 직전 재확인에서 실제 paper position이 계획 입력 장부 `position_quantities_before`와 모두 일치하고, 유동성 preflight와 pre-trade risk를 통과하고, `executed == planned`, failed=0, skipped=0, halted=false이며, 성공 주문 결과 payload와 당일 `TradeHistory` fill 집계가 plan의 종목/방향/수량과 일치하고, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 `pilot_paper` evidence를 수집. `collect_daily_evidence()`가 이미 기록된 날짜로 `None`을 반환해도 최신 canonical record가 `pilot_paper`/authorized/execution-backed/target-weight complete/trade-day-complete/market-session-complete/auth-snapshot-complete/pre-execution-complete/liquidity-complete/pre-trade-risk-complete/order-count/order-result/order-complete/fill-complete와 order/fill/position reconciliation complete 조건을 만족할 때만 `already_recorded` 또는 promotion proof로 인정한다. 중복 실행은 `target_weight_duplicate_execution_attempt`, 실행일 불일치는 `target_weight_execution_trade_day_mismatch`, 장 시간 외 실행은 `target_weight_execution_market_session_closed`, 실행 전 포지션 드리프트는 `target_weight_pre_execution_position_drift`, 승인 snapshot 불일치는 `target_weight_pilot_authorization_snapshot_mismatch`, 유동성 preflight 실패는 `target_weight_liquidity_preflight_failed`, pre-trade risk 실패는 `target_weight_pre_trade_risk_failed`, 부분 실행/중단은 `target_weight_execution_incomplete`, 주문 결과 불일치는 `target_weight_order_result_mismatch`, 체결 기록 불일치는 `target_weight_fill_reconciliation_mismatch`, 기존 evidence 불일치는 `target_weight_existing_evidence_invalid`, 실행 후 포지션 불일치는 `target_weight_position_mismatch`로 세션 artifact와 pilot session에 기록하고 non-zero CLI 종료로 자동화가 clean execution-backed evidence로 오인하지 못하게 차단** |
| Target-weight completed rerun block | 높음 | **완료 — same-candidate/trade-day session artifact가 `execution_complete=True`이고 실제 실행 주문이 있으면 `--allow-rerun`을 줘도 재실행 차단. `--allow-rerun`은 부분 실행/중단 세션 복구에만 허용해 완료된 execution-backed evidence와 실제 paper 주문이 중복되는 위험을 제거** |
| Target-weight authorization snapshot guard | 높음 | **완료 — target-weight pilot auth 승인 시 plan snapshot을 저장하고, target-weight 전용 실행 어댑터는 후보명 접두어와 무관하게 승인 snapshot을 요구한다. `--execute`, readiness audit, daily ops summary, promotion proof가 승인 snapshot과 현재 plan의 trade day/as-of date/params hash/targets/수량 장부 일치를 요구. cap 승인 후 plan drift가 생기면 주문 제출·idempotency·포지션 조회·세션 저장·evidence 수집 전에 fail-closed 차단** |
| Target-weight market-session guard | 높음 | **완료 — `--execute`, readiness audit, daily ops summary, promotion proof가 `TradingHours.can_place_order()` 기반 KRX 정규장/휴장일 검증을 요구. 같은 trade_day라도 장마감 후·주말·휴장일이면 주문 제출 전 `target_weight_execution_market_session_closed`로 차단하고 `execution_market_session_allowed=True`가 아닌 record는 승격 증거에서 제외** |
| Target-weight price freshness guard | 높음 | 완료 — 목표비중 plan 생성 시 종목별 마지막 실제 종가 날짜와 벤치마크 최신 날짜를 기록한다. ffill이 오래된 종가를 최신 `trade_day` 가격처럼 숨기면 stale 종목/벤치마크 오류로 fail-closed 차단해 paper 실행과 승격 증거 오염을 막는다. readiness audit과 daily ops summary도 같은 diagnostics를 `data_quality_check`로 재검증해 stale 가격, 보유 종목 가격 누락, 벤치마크 지연이 운영 READY 상태로 묻히지 않게 한다 |
| Target-weight 운영 리포트 상태 라벨 | 중간 | 완료 — readiness audit과 daily ops Markdown/JSON risk snapshot에서 `checked=False`인 실행일·장 시간·승인 snapshot 검사는 `PASS`가 아니라 `NOT CHECKED`로 표시한다. pilot entry가 먼저 차단되어 후속 검사를 못 한 상태를 통과로 오해하지 않게 한다 |
| Target-weight turnover-aware research | 중간 | 완료 — canonical risk-overlay target-weight 후보에 `target_tolerance_pct=3/5` 변형을 추가하고, research metrics에 tolerance 생략 거래 수·매수/매도 구분·생략 notional·자본 대비 비율을 기록한다. 높은 turnover 후보가 비용을 줄이는 대신 alpha를 유지하는지 sweep artifact에서 직접 비교 가능 |
| Portfolio backtest event guard | 높음 | 완료 — `backtest/portfolio_backtester.py`에 gap-up 신규 매수 차단, gap-down `GAP_DOWN` 청산, 어닝 윈도우 신규 매수 차단, BlackSwan 청산·쿨다운·recovery 사이징과 `gap_*`/`earnings_*`/`blackswan_*` 진단 카운터를 추가 |

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

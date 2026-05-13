# 백테스트 신뢰성 개선 내역

> **문서 버전**: v6.0
> **최종 수정**: 2026-05-13
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
| **pilot guard fail-open 위험** | pilot authorization 이후 runtime/evidence/notifier/cap guard 오류가 예외 처리로 삼켜지면 제한 주문이 허용될 수 있음 | **수정 완료** — `check_pilot_entry()` 모든 blocked/allowed 결과 audit, guard 예외는 fail-closed block, notifier health missing/corrupt도 차단 |
| **paper evidence append 순서 의존** | backfill/shadow/finalize 기록이 JSONL 뒤쪽에 섞이면 최신 evidence, 최근 10일, promotion period가 append 순서 기준으로 왜곡될 수 있음 | **수정 완료** — canonical view를 날짜별 최신 record + 날짜순 반환으로 고정하고, 중복 확인은 전체 파일의 해당 날짜 최신 record를 조회 |
| **legacy evidence E2E 수집 경로** | 오래된 v1 `save_daily_evidence`/`evidence_collector` 계약이 남아 pytest collection과 scheduler 장마감 legacy 호출을 깨뜨릴 수 있음 | **수정 완료** — E2E를 v2 JSONL/canonical/report 계약으로 교체, scheduler legacy 호출 제거, `core.evidence_collector`는 import 호환 no-op shim으로 축소 |
| **방어형 후보 raw benchmark 해석** | cash-switch처럼 평균 노출이 낮은 후보는 full B&H 대비 excess가 과도하게 나빠 보일 수 있음 | **진단 추가** — research sweep에 exposure-matched B&H return/sharpe/MDD/excess 기록. 단, promotion gate는 raw benchmark excess 유지 |
| **회전 전략의 sparse signal 한계** | 월간 상대강도 후보가 BUY/SELL 신호만 내면 목표 top-N을 지속적으로 채우지 못해 평균 노출이 낮게 측정될 수 있음 | **검증 완료** — target-weight top-N research backtester로 avg exposure 85%대까지 개선. 5종목 smoke는 raw excess 음수였지만 canonical top-20 full sweep은 alpha 후보 확인. `hold_rank_buffer`와 `benchmark_risk` overlay 적용 후 best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`가 next-open/결측 진단 재검증 기준 return=+171.20%, PF=4.24, Sharpe=1.41, MDD=-19.90%, WF positive=100%, WF Sharpe+=83.3%로 provisional gate 통과. 단, held stale valuation 21일과 보유 종목 시가 누락 리밸런싱 skip 1회가 남아 paper pilot에서 체결 가능성 검증 필요 |
| **target-weight 후보의 paper 연결 부재** | research-only evaluator에서 provisional 후보가 나와도 기존 canonical/paper 경로는 등록 전략만 평가 | **대부분 해결** — canonical bundle 재현 완료 + `core/target_weight_rotation.py`, `tools/target_weight_rotation_pilot.py`로 전용 paper/pilot adapter 추가. dry-run은 `--record-shadow-evidence`, `--shadow-days 3`, 또는 `--shadow-start-date/--shadow-end-date`로 non-promotable shadow readiness evidence, launch readiness artifact, pilot runbook을 남기고 cap preview와 plan 기반 최소/추천 cap + enable 명령으로 pilot 승인 전 캡 적합성을 확인. 이후 `--readiness-audit`가 주문 제출, evidence 기록, pilot session 저장 없이 clean shadow/launch readiness, active pilot auth와 cap validation, 중복 session idempotency, 실행일/장 시간, 실행 전 position drift, 추천 cap 충족 여부, 유동성 preflight, 비용 반영 pre-trade risk를 JSON artifact와 Markdown 운영 리포트로 판정한다. 유동성 preflight는 최근 20일 평균 거래대금 대비 주문 notional 비율을 계산하고 기본 5%(`--max-order-adv-pct`) 초과 주문은 readiness와 실행을 fail-closed 차단한다. pre-trade risk는 `RiskManager.calculate_transaction_costs()`의 수수료/세금/동적 슬리피지 예상 체결가를 재사용해 현금 부족, 최소 현금비중, 총투자비중, 종목별 비중, 보유 종목 수 위반을 주문 제출 전에 차단한다. Markdown 리포트는 shadow 수집, audit 재실행, 추천 cap 승인, capped paper 실행 명령을 함께 남긴다. `--shadow-days N`은 휴장/데이터 공백으로 같은 거래일에 매핑될 때 과거 평일을 추가 스캔해 N개 고유 resolved trade_day 충족을 목표로 하며, 목표 미달이나 날짜별 실패는 non-zero 종료로 fail-closed 처리한다. 실행형 `pilot_paper` evidence는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, 주문 제출 직전 실제 paper position이 계획 입력 장부 `position_quantities_before`와 일치하고, KST 실행일과 KRX 정규장 주문 가능 시간을 통과하고, 유동성 preflight와 pre-trade risk를 통과하고, 계획 주문 전부 성공 및 주문 결과 payload와 당일 `TradeHistory` fill 집계가 plan과 일치하며, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 수집한다. 같은 날짜가 이미 기록된 경우에도 기존 canonical evidence가 `pilot_paper`/authorized/execution-backed이고 target-weight plan hash와 complete/execution-market-session/liquidity/pre-trade-risk/order/fill/position 검증을 통과해야 재사용해 중복 실행/stale plan/장 외 실행/부분 실행/중단/주문 결과 불일치/체결 기록 불일치/기존 evidence 검증 실패/포지션 불일치일이 승격 증거로 섞이지 않게 한다. 명시 재시도는 `--allow-rerun`으로만 허용한다. 다음 과제는 shadow clean days 충족 후 정규장 capped pilot_paper execution-backed evidence 축적 |
| **target-weight stale 가격 ffill 위험** | 일부 종목 또는 벤치마크의 최신 데이터가 비었는데 panel ffill로 오래된 종가가 `trade_day`/research 평가일 최신 가격처럼 쓰이면 목표 수량·리스크·승격 증거·연구 성과가 왜곡될 수 있음 | **수정 완료** — `build_target_weight_plan()`은 운영 주문 계획이므로 종목별 `price_last_dates`와 `benchmark_last_date`를 diagnostics에 기록하고 stale 종목/벤치마크를 plan 생성 전 fail-closed 차단. `run_target_weight_rotation_backtest()`는 research-only 경로라 score day 실제 종가가 없는 종목은 해당 리밸런싱 후보에서 제외하고, 보유 종목의 일별 stale 평가는 `held_stale_valuation_*`로 기록하며, 보유 종목 시가가 없어 리밸런싱할 수 없는 날은 거래를 건너뛰고 `missing_held_open_*`/`skipped_rebalance_missing_held_open_count`로 기록. 벤치마크 stale과 신규 top-N 매수 후보의 시가 누락은 계속 fail-closed |
| **백테스트 BlackSwan/어닝/갭 필터 미적용** | 단일종목·포트폴리오 백테스터에 BlackSwan, 어닝 필터, 갭 리스크 체크가 없으면 paper/live보다 낙관적인 성과가 나올 수 있음 | **수정 완료** — `backtest/backtester.py`와 `backtest/portfolio_backtester.py`가 원본 `open`/이벤트 컬럼을 보존하고 `gap_risk` 갭다운 청산·갭업 신규 매수 차단, `earnings_date`/`next_earnings_date`/flag 기반 어닝 윈도우 신규 매수 차단, `risk_params.blackswan` 기반 긴급 청산·쿨다운·recovery 사이징을 반영 |
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
| target-weight portfolio drawdown guard 후보군 | 높음 | canonical 검증 완료 — `target_weight_drawdown_guard` family에 exp75/rankrisk90/pdd10/floor40 기반 `tol3`, `tol4`, `tol5`, `maxnew2`, `tol3_maxnew2`, tol4 guard-only 강화 4종, rank-risk 강화 2종, sector cap 3종, correlation cap 3종, correlation rank penalty 3종, loss re-entry guard 3종, position loss reduction 5종을 추가하고 `--top-n 200` full sweep으로 병목을 비교했다. 업종 매핑은 FDR Sector 누락 시 KRX KIND 상장법인 목록으로 2,766개 종목을 보강한다. `rankrisk90_tol4_corrcap85`와 `sectorcap2_corrcap85`는 top-200에서 각각 기존 tol4/sectorcap2와 동일한 return +81.19%/+82.57%, raw excess +49.30%p/+50.68%p, MDD -19.33%/-19.59%, turnover/year 886.7%/909.3%였고 관측 최대 선택 상관도는 0.8147. correlation rank penalty는 top-200에서는 개선됐지만 canonical top-20에서는 benchmark excess가 음수라 모두 `paper_only`. loss re-entry guard는 회전율만 낮추고 MDD를 악화시켜 canonical 제외. 포지션 손실 감산은 `sectorcap2_posloss8`이 top-200 return +105.79%, raw excess +73.90%p, MDD -18.96%, Sharpe 1.00으로 통과했으나 canonical turnover/year 1006.2%로 `paper_only`; `tol5` 결합 후 `target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1`가 top-200 return +112.16%, raw excess +80.27%p, MDD -19.23%, Sharpe 1.04로 통과했고 canonical top-20에서도 return +198.15%, benchmark excess +48.76%p, Sharpe 1.57, PF 5.58, MDD -17.18%, turnover/year 993.9%, WF positive/Sh+ 100%로 신규 `provisional_paper_candidate`. 다만 live는 60일 paper evidence와 target-weight verified proof가 없어 차단. paper/pilot plan builder는 rank penalty, sector cap, position-loss 감산 산식과 paper evidence 기반 portfolio drawdown guard 상태를 적용하도록 보강했으며, sector map 누락 또는 drawdown guard 명시 상태 누락은 fail-closed 차단한다. evidence snapshot은 guard cooldown을 저장해 다음 리밸런싱 상태 복원에 사용한다 |
| target-weight 변동성 예산 목표비중 | 높음 | canonical 검증 완료·승격 보류 — `target_allocation_mode=inverse_volatility`가 선택 종목의 목표 금액을 rolling 실현 변동성 역수로 배분하고 sleeve weight 상한을 적용한다. `--candidate-family target_weight_volatility_budget --top-n 200` full sweep에서 유동성 통과 164개, benchmark coverage 100% 기준 best=`target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35`가 return +78.77%, raw excess +46.88%p, exposure-matched excess +63.79%p, Sharpe 0.81, PF 1.96, MDD -18.96%, trades 107로 `provisional_paper_candidate` 통과. sectorcap2+volbudget60 후보도 return +77.18%, raw excess +45.29%p, MDD -19.19%로 통과했고, volbudget90 후보는 benchmark excess Sharpe<=0 병목. 하지만 canonical top-20 재평가에서는 volbudget60 후보가 return +170.73%, benchmark excess +21.34%p, Sharpe 1.44, PF 4.53, turnover/year 982.6%, WF positive/Sh+ 100%였으나 MDD -20.82%로 `paper_only`; sectorcap2+volbudget60도 return +173.80%, benchmark excess +24.41%p, Sharpe 1.46, PF 4.68, turnover/year 978.1%, WF positive/Sh+ 100%였으나 MDD -20.89%로 `paper_only`. 초과수익과 회전율은 유지됐지만 canonical MDD가 더 악화되어 paper/live 승격 대상은 아니며, 현재 배분 산식은 research backtest 경로에만 있으므로 향후 다시 통과 후보가 생기면 paper pilot 전 plan builder 이식과 params_hash 일치 검증이 필요하다. 다음은 포지션별 손실 감산 또는 더 보수적인 drawdown guard 결합으로 MDD를 -20% 안쪽에서 안정화하는 쪽 |
| Strategy Ablation Test (전략별 단독 성과 비교) | 높음 | **C-4/C-5 단독·sleeve 비교 완료** |
| 비용 반영 전/후 성과 비교 리포트 자동화 | 중간 | 완료 — `backtest.cost_impact`가 수수료·세금·슬리피지 explicit cost를 표준 집계하고, 단일/포트폴리오 백테스트 metrics와 txt/html 리포트에 비용 차감 전 추정 수익률, 비용 차감 후 수익률, 비용 드래그(bp), 비용/순손익, cost impact status를 자동 노출한다. 비용 때문에 gross profit이 net loss로 뒤집히거나 비용이 순이익을 초과하면 fail/warn으로 표시해 과매매·고회전 후보 착시를 줄임 |
| 월별 성과 분해 | 중간 | **C-5 반기별 분해 구현 완료** |
| 유동성 필터 (일평균 거래대금 기준 종목 제외) | 높음 | **완료** — watchlist 진입 대상, 포트폴리오 백테스트 입력 universe, research candidate sweep universe에 20일 평균 거래대금 하한 필터 적용. target-weight pilot 주문 전 평균 거래대금 대비 주문 비율 preflight도 유지 |
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
| 벤치마크 거래비용 반영 | 높음 | **완료 — `_buy_and_hold_metrics`에 commission/tax/slippage 적용** |
| Paper Evidence 수집 체계 | 높음 | **완료 — `core/paper_evidence.py` 일별 22개 지표, 6 anomaly rule, 9 approval gate** |
| Paper Evidence canonical 정렬 | 높음 | 완료 — append-only JSONL에서 같은 날짜의 최신 record만 canonical로 유지하고 날짜순으로 반환. backfill/finalize/shadow append 순서와 무관하게 freshness, 최근 10일, promotion period가 실제 날짜 기준으로 계산됨 |
| Paper Evidence 최신성 승격 gate | 높음 | 완료 — promotion package에 `earliest_evidence_date`/`latest_evidence_date`를 기록하고, live_candidate 판정은 canonical 평가 기준 최신 evidence age 14일 이내만 허용한다. 날짜 누락, 미래일, 14일 초과 package는 fail-closed로 막아 오래된 60영업일 증거 재사용을 차단 |
| Live gate Paper Evidence 최신성 검증 | 높음 | 완료 — `core/live_gate.py`가 live 진입 직전에 promotion evidence의 `latest_evidence_date` 또는 `period` 종료일을 확인한다. 최신 증거가 14일을 초과하거나 날짜가 누락/미래이면 promotion_result가 live_candidate여도 실전 전환을 차단 |
| Legacy evidence E2E 정리 | 높음 | 완료 — v1 helper API 기반 `tests/test_evidence_e2e.py`를 v2 smoke/E2E로 교체하고 scheduler의 deprecated v1 collector 호출 제거 |
| Paper Runtime State Machine | 높음 | **완료 — `core/paper_runtime.py` 5개 상태(normal/degraded/frozen/blocked/research_disabled), schema quarantine** |
| Paper Pilot Authorization | 높음 | **완료 — `core/paper_pilot.py` launch readiness + pilot auth + 리스크 캡 + fail-closed/audited entry guard** |
| Paper Preflight Check | 높음 | **완료 — `core/paper_preflight.py` 운영 준비 상태 점검** |
| Strategy Universe Registry | 높음 | **완료 — `core/strategy_universe.py` paper 대상 전략 canonical 목록** |
| Zero-return Semantics (deadlock 해소) | 높음 | **완료 — cash-only/no-position day에서 daily_return=0.0 추론, benchmark final 가능** |
| Paper 운영 도구 (tools/) | 높음 | **완료 — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI** |
| Entry filter 탐색 (market filter, abs momentum, cooling) | 중간 | **완료 — 모두 NO_MEANINGFUL_IMPROVEMENT 또는 ADVERSE EFFECT. 현행 유지** |
| 운영/백테스트 시장국면 설정 동기화 | 높음 | 완료 — `trading.market_regime_*`를 canonical 설정으로 두고 `resolve_market_regime_config()`로 paper/live와 백테스트가 같은 파라미터를 보게 했다. `backtest_regime_filter`는 명시 값만 실험용 override로 사용하며, 백테스트도 MA(20/60) 데드크로스 신호를 반영한다. 기본값은 검증 결과에 맞춰 OFF |
| Research sweep exposure-matched benchmark | 중간 | **완료 — 후보별 평균 노출/현금비중과 exposure-matched B&H excess 진단 추가. promotion gate는 raw benchmark 기준 유지** |
| Target-weight top-N rotation backtester | 높음 | **완료 — 월간 직전일 점수 기준 top-N 목표비중 리밸런싱, 다음 거래일 시가 체결, 종가 평가, delta 거래비용, 노출 진단 구현. score day 종가 stale 종목은 후보에서 제외하고, 보유 종목 stale 평가는 별도 metrics로 기록하며, 보유 종목 시가 누락 리밸런싱은 거래 없이 skip 기록. 벤치마크 stale과 신규 top-N 후보 시가 누락은 fail-closed. 기존 종가 체결 기반 target-weight research artifact는 재생성 또는 execution price mode 확인 후 사용** |
| Target-weight research next-open execution guard | 높음 | 완료 — 리서치 백테스트의 리밸런싱 체결가를 당일 종가에서 원본 `open` 기반 다음 거래일 시가로 변경하고, trade/metrics에 `execution_price_mode=next_open`, `execution_price_freshness_checked`, `avg_volume_lookback_lag_days=1`을 기록 |
| Target-weight score-floor 후보 | 중간 | **완료 — `min_score_floor_pct`로 약한 초과 모멘텀 슬롯을 현금으로 남김. best=`target_weight_rotation_top5_60_120_floor0`, +210.21%/Sharpe 1.41/WF positive 100%였으나 turnover/year=1081.5%라 turnover-aware 변형으로 후속 검증** |
| Target-weight rank-hysteresis 후보 | 높음 | **완료 — `hold_rank_buffer`로 보유 종목이 top-N 밖으로 소폭 밀려도 버퍼 안이면 유지. best=`target_weight_rotation_top5_60_120_floor0_hold3`, +278.57%/Sharpe 1.65/WF positive 100%/turnover 807.8%. 남은 병목은 MDD=-28.25%** |
| Target-weight shadow proof | 높음 | **완료 — dry-run plan을 `shadow_bootstrap` evidence로 기록하되 `execution_backed=False`, excess=null로 유지해 promotion을 오염시키지 않음. `--shadow-days` 기반 자동 날짜 선택은 N개 평일이 아니라 N개 고유 resolved trade_day를 목표로 과거 평일을 보충 스캔하며, 명시적 날짜 범위 batch도 지원. 목표 미달/날짜별 실패는 CLI non-zero 종료로 자동화가 불완전한 shadow proof를 성공 처리하지 못하게 차단. artifact와 runbook에 cap preview, plan 기반 최소/추천 cap, enable 명령, launch artifact 경로를 기록해 기본 pilot cap 부족(주문 수/포지션/1건 금액/총노출), clean day 부족, notifier/auth 누락을 사전 확인** |
| Target-weight readiness audit | 높음 | **완료 — `--readiness-audit`로 주문 제출/evidence 기록 없이 capped pilot 직전 상태를 JSON artifact와 Markdown 운영 리포트로 점검. clean shadow/launch readiness, active pilot auth와 cap validation, 추천 cap, 중복 session idempotency, 실행일/장 시간, 실행 전 position drift, 데이터 품질, 유동성 preflight, 비용 반영 pre-trade risk, 다음 조치를 함께 판정하고 shadow 수집/audit 재실행/추천 cap 승인/capped paper 실행 명령을 남긴다. 실행 준비/장 대기 상태는 실행일/장 시간 점검과 pilot authorization snapshot이 `checked=True`로 통과한 경우에만 표시해 미점검 placeholder를 실행 가능 상태로 오인하지 않게 한다. cap 승인 준비가 안 됐거나 장 외 실행이면 non-zero 종료** |
| Target-weight preflight/notifier 동기화 | 높음 | 완료 — readiness audit 시작 시 paper preflight를 먼저 갱신해 `preflight_refresh` artifact를 남긴다. Discord webhook 미설정이나 notifier health 비정상은 pilot auth/cap 상태와 별개로 주문 전 `BLOCKED` 처리하며, `.env`의 `DISCORD_WEBHOOK_URL` 설정 후 preflight/audit 재실행이 필요 |
| Target-weight pilot enable guard | 높음 | **완료 — `tools/paper_pilot_control.py --enable`이 target-weight 후보 승인 전에 readiness audit을 재실행하고, 운영자가 요청한 cap이 현재 plan/launch readiness/유동성 preflight/비용 반영 pre-trade risk를 만족할 때만 pilot auth를 기록. 승인 auth에는 plan의 trade day/as-of date/params hash/targets/시작·목표 수량 snapshot을 함께 저장한다. 추천 cap 미충족, stale plan, audit blocker가 있으면 승인 자체를 fail-closed 차단** |
| Target-weight cap validation artifact | 중간 | **완료 — `run_pilot(execute=True)`가 pilot cap validation에서 막혀도 주문 제출 전 session JSON artifact에 차단 사유와 skipped order를 기록. runtime pilot session/evidence/fill reconciliation은 쓰지 않아 cap 수정 후 같은 trade_day를 다시 점검 가능** |
| Target-weight promotion proof guard | 높음 | **완료 — promotion package가 target-weight 계열 strategy의 promotable day를 verified `pilot_paper` execution proof로만 계산. `pilot_authorized`, 승인 snapshot 일치, target-weight plan/execution params hash 일치, liquidity/pre-trade risk/order result/fill/position reconciliation complete가 모두 필요하며, `evaluate_and_promote` live_candidate 판정과 live gate 모두 proof summary 누락/invalid day/params hash drift를 fail-closed 차단** |
| Target-weight canonical hash gate | 높음 | 완료 — target-weight `live_candidate` 판정에서 paper evidence package의 `target_weight_params_hash`가 canonical promotion bundle의 `strategy_specs[].params_hash`와 일치해야 한다. 같은 candidate id라도 파라미터가 바뀐 뒤 예전 60일 pilot evidence가 현재 후보 승격에 재사용되는 경로를 fail-closed 차단 |
| Canonical benchmark coverage 승격 gate | 높음 | 완료 — `evaluate_and_promote`가 promotion_result를 계산하기 전에 canonical metadata integrity를 검증하고, artifact-driven promotion은 benchmark excess return/Sharpe 양수를 요구한다. benchmark coverage 누락, material fetch error, 평가 오류, benchmark excess 누락/0 이하가 있으면 provisional/live 승격이 생성되지 않는다 |
| Promotion blocker summary | 중간 | 완료 — canonical promotion 실행 시 `promotion_blocker_summary.json/md`를 생성하고, `tools/evaluate_and_promote.py --blocker-summary`로 기존 artifact에서 요약을 재생성한다. 요약은 source artifact hash를 포함하고 `--blocker-summary-check`가 `promotion_result/metrics/run_metadata`와 동기화 여부를 fail-closed 검증한다. 전략별 status, 주요 blocker, 핵심 metrics, 체결 품질 blocker, 다음 운영 조치를 요약해 긴 `promotion_result.reason`을 직접 해석하지 않아도 paper evidence 갱신, benchmark 재평가, target-weight proof 보강, 체결 왜곡 재점검 등 다음 작업을 바로 확인 가능 |
| Paper 체결 품질 리포트 | 중간 | 완료 — `tools/paper_trade_quality_report.py`가 paper `TradeHistory`의 `expected_price`, `price_gap`, `actual_slippage_pct`, `slippage`를 집계해 불리한 체결 갭 비용, expected_price 누락, 일자/종목별 체결 품질을 JSON/Markdown으로 기록한다. `generate_promotion_package()`는 같은 기간 `trade_quality`를 함께 저장하고 expected_price 누락 또는 50bp 초과 불리한 체결 갭을 `BLOCKED` 사유로 반영해 paper evidence의 수익성이 실제 체결 왜곡을 견디는지 승격 전 운영 검토 가능 |
| Test artifact quarantine | 중간 | 완료 — `tools/quarantine_test_artifacts.py`가 `reports/paper_evidence`, `reports/paper_runtime`, `reports/promotion`에서 test/demo 전략 artifact를 감지해 `_quarantine`으로 이동한다. promotion JSON 내부 top-level 전략명과 `strategy`/`candidate_id` payload도 검사하며, paper preflight가 발견 건수와 정리 명령을 WARN으로 남겨 테스트 산출물이 canonical promotion/operator view에 섞이는 위험을 줄임 |
| Target-weight 실행 증거 E2E 회귀 | 높음 | 완료 — readiness audit → cap 승인 snapshot → capped paper 실행 증거 → promotion package → promotion engine → live gate까지 같은 params hash와 verified pilot proof가 끊기지 않는지 CI 테스트로 고정 |
| Target-weight liquidity preflight | 높음 | **완료 — 목표비중 plan 생성 시 주문 종목별 최근 20일 평균 거래대금을 diagnostics에 기록하고, `--readiness-audit`와 `--execute`가 주문별 평균 거래대금 대비 notional 비율을 확인. 기본 5% ADV 초과 또는 유동성 데이터 누락은 `target_weight_liquidity_preflight_failed`로 fail-closed 차단하며 session/readiness/pilot evidence snapshot에 결과를 남김** |
| Backtest/research liquidity universe filter | 높음 | **완료 — `WatchlistManager.liquidity_filter_report()`를 공통 진단 API로 분리하고, `PortfolioBacktester.run()`과 `tools/research_candidate_sweep.py`가 평가 시작일 기준 20일 평균 거래대금 하한 미만·strict 데이터 누락 종목을 universe에서 사전 제외** |
| Portfolio dynamic slippage | 높음 | **완료 — `PortfolioBacktester`가 종목별 20일 평균 거래량을 매수/매도 거래비용 계산에 전달하고 `participation_rate`, `slippage_multiplier`, `slippage_cost`를 거래 기록에 남김** |
| Target-weight research dynamic slippage | 높음 | **완료 — target-weight 리서치 백테스터가 종목별 20일 평균 거래량을 매수/매도 거래비용 계산에 전달하고 `avg_daily_volume`, `participation_rate`, `slippage_multiplier`, `slippage_cost_total` 진단값을 artifact metrics에 남김** |
| Paper/live 체결가 기준 현금 정산 | 높음 | 완료 — paper BUY는 예상 체결가 기준으로 수량·방어 가격을 산정하고, paper BUY/SELL은 모델 execution_price를 체결가로 기록한다. DB 현금 요약은 이미 체결가에 반영된 슬리피지를 다시 차감하지 않는다. slippage 필드는 비용 진단과 리포트용으로 유지 |
| 운영 손실 한도 신규 진입 차단 | 높음 | 완료 — `OrderExecutor` 주문 전 검사에서 `drawdown.max_portfolio_mdd`와 `max_daily_loss`를 확인해 신규 BUY만 fail-closed 차단한다. MDD는 DB peak를 복구하는 `PortfolioManager.get_portfolio_summary()` 기준을 사용하고, 일일 손실은 최근 포트폴리오 스냅샷 대비 평가금액 하락률로 본다. SELL/exit는 손절·청산 경로라 계속 허용 |
| Paper Evidence MDD 정규화 | 높음 | 완료 — PortfolioSnapshot은 MDD를 양수 퍼센트로 저장하고 기존 evidence/promotion은 음수 drawdown 기준으로 판정하던 불일치를 정리했다. 수집·finalize·promotion 집계에서 MDD를 `-abs(mdd)`로 정규화해 deep_drawdown anomaly와 `max_mdd` 승격 차단이 fail-open 되지 않게 했다 |
| Research sweep benchmark coverage guard | 높음 | 완료 — EW B&H 벤치마크 입력 universe 일부라도 수집 실패·기간 부족이면 초과수익 계산을 0으로 고정하고 `INSUFFICIENT_BENCHMARK_DATA` decision으로 canonical 평가 진행을 차단 |
| Live 체결 확인 guard | 높음 | 완료 — KIS 주문 ACK 후 체결가·체결수량 조회가 실패하거나 부분체결만 확인되면 예상가 기준 `FILLED` 처리 대신 `ACKED`/`PARTIAL_FILLED` pending으로 남기고 `requires_reconcile=True`로 운영 대조를 요구 |
| Live 체결보류 신규진입 중단 | 높음 | 완료 — 장중 신규 진입 루프에서 live 주문이 접수됐지만 체결 확인이 보류되면 브로커 잔고 동기화 전까지 같은 루프의 남은 신규 BUY 실행을 중단해 미확정 체결분을 무시한 추가 매수를 차단 |
| Live 미완료 주문 상태 영속화 | 높음 | 완료 — live 주문 `SUBMITTED`/`ACKED`/`PARTIAL_FILLED` 상태를 `order_records`에 저장하고, 재시작 또는 OrderGuard TTL 만료 후에도 DB에 미완료 주문이 남아 있으면 같은 종목 신규 주문을 fail-closed 차단 |
| Live 보류 주문 복구 대조 | 높음 | 완료 — 재시작 복구에서 KIS 미체결 조회가 성공했고 DB `ACKED`/`PARTIAL_FILLED` 주문번호가 브로커 미체결 목록에서 사라졌으면 체결 조회 결과를 보강해 `order_records`를 `RECONCILED`로 닫고 종목별 OrderGuard를 해제. 잔고/포지션 정합성은 이어지는 KIS↔DB 동기화가 처리 |
| Live 미체결 조회 fail-closed | 높음 | 완료 — KIS 미체결 조회 실패, `rt_cd != 0`, 응답 형식 이상을 주문 가능 상태로 보지 않고 live BUY/SELL을 제출 전 차단 |
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
| Target-weight execution-aware evidence | 높음 | **완료 — `run_pilot(execute=True, collect_evidence=True)`는 같은 candidate/trade_day의 기존 pilot session artifact가 없고, 활성 pilot auth의 승인 snapshot이 현재 plan과 일치하며, KST 실행일과 KRX 정규장 주문 가능 시간을 통과하고, 주문 제출 직전 실제 paper position이 계획 입력 장부 `position_quantities_before`와 일치하고, 유동성 preflight와 pre-trade risk를 통과하고, `executed == planned`, failed=0, skipped=0, halted=false이며, 성공 주문 결과 payload와 당일 `TradeHistory` fill 집계가 plan의 종목/방향/수량과 일치하고, 실행 후 실제 paper position 전체가 리밸런싱 후 `target_quantities_after` 장부와 일치하고 계획 밖 양수 포지션이 없을 때만 `pilot_paper` evidence를 수집. `collect_daily_evidence()`가 이미 기록된 날짜로 `None`을 반환해도 기존 canonical record가 `pilot_paper`/authorized/execution-backed/target-weight complete/trade-day-complete/market-session-complete/auth-snapshot-complete/liquidity-complete/pre-trade-risk-complete/fill-complete 조건을 만족할 때만 `already_recorded`로 인정한다. 중복 실행은 `target_weight_duplicate_execution_attempt`, 실행일 불일치는 `target_weight_execution_trade_day_mismatch`, 장 시간 외 실행은 `target_weight_execution_market_session_closed`, 실행 전 포지션 드리프트는 `target_weight_pre_execution_position_drift`, 승인 snapshot 불일치는 `target_weight_pilot_authorization_snapshot_mismatch`, 유동성 preflight 실패는 `target_weight_liquidity_preflight_failed`, pre-trade risk 실패는 `target_weight_pre_trade_risk_failed`, 부분 실행/중단은 `target_weight_execution_incomplete`, 주문 결과 불일치는 `target_weight_order_result_mismatch`, 체결 기록 불일치는 `target_weight_fill_reconciliation_mismatch`, 기존 evidence 불일치는 `target_weight_existing_evidence_invalid`, 실행 후 포지션 불일치는 `target_weight_position_mismatch`로 세션 artifact와 pilot session에 기록하고 non-zero CLI 종료로 자동화가 clean execution-backed evidence로 오인하지 못하게 차단** |
| Target-weight completed rerun block | 높음 | **완료 — same-candidate/trade-day session artifact가 `execution_complete=True`이고 실제 실행 주문이 있으면 `--allow-rerun`을 줘도 재실행 차단. `--allow-rerun`은 부분 실행/중단 세션 복구에만 허용해 완료된 execution-backed evidence와 실제 paper 주문이 중복되는 위험을 제거** |
| Target-weight authorization snapshot guard | 높음 | **완료 — target-weight pilot auth 승인 시 plan snapshot을 저장하고, `--execute`, readiness audit, daily ops summary, promotion proof가 승인 snapshot과 현재 plan의 trade day/as-of date/params hash/targets/수량 장부 일치를 요구. cap 승인 후 plan drift가 생기면 주문 제출·idempotency·포지션 조회·세션 저장·evidence 수집 전에 fail-closed 차단** |
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

# 백테스트 신뢰성 개선 내역

> **문서 버전**: v3.1
> **최종 수정**: 2026-03-31
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
| **월간 왕복 제한** | `max_monthly_roundtrips: 5` (종목당) | `risk_params.yaml`, `core/order_executor.py` |
| **최소 보유 기간** | `min_holding_days: 3` | `risk_params.yaml`, `core/order_executor.py` |
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
| **멀티전략 강건성 미달** | breakout_volume+rotation 2-sleeve 비중 스윕에서 양 구간(2021-2023, 2024-2025) 모두 양수 수익을 내는 조합 없음 | 하락장 방어 로직 필요 |

---

## 4. 추가 과제 (미완료)

| 과제 | 우선순위 | 상태 |
|------|----------|------|
| 유니버스 전체 (코스피200) 백테스트 | 높음 | 미실행 |
| Strategy Ablation Test (전략별 단독 성과 비교) | 높음 | **C-4/C-5 단독·sleeve 비교 완료** |
| 비용 반영 전/후 성과 비교 리포트 자동화 | 중간 | 미실행 |
| 월별 성과 분해 | 중간 | **C-5 반기별 분해 구현 완료** |
| 유동성 필터 (일평균 거래대금 기준 종목 제외) | 높음 | 미구현 |
| Sortino Ratio 자동 계산 | 낮음 | 구현 완료 (리포트 미포함) |
| Calmar Ratio 자동 계산 | 낮음 | 구현 완료 (리포트 미포함) |
| Rotation 하락장 방어 (시장 국면 필터) | 높음 | 미구현. DEV 구간 MDD -7.90% 개선 필요 |
| 멀티전략 sleeve 비중 최적화 재검증 | 높음 | 대기 (하락장 방어 추가 후) |
| KR_CORE_10 유니버스 확장 | 중간 | 대기 (2-sleeve 강건성 통과 후) |

---

## 5. 참고 문서

| 문서 | 내용 |
|------|------|
| `quant_trader_design.md` §8 | 백테스팅 & 검증 전체 아키텍처 |
| `reports/strategy_promotion_policy.md` | 전략 승격 정량 기준표 |
| `reports/live_gate_policy.md` | Live 진입 5개 조건 |
| `reports/paper_experiment_manifest.json` | 60영업일 paper 실험 설정 |
| `reports/full_paper_lifecycle_test.json` | Lifecycle 테스트 4/4 PASS 결과 |

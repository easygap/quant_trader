# Ablation Study Runbook v1.0
> TICKET-01 / TICKET-02 / TICKET-05 변경 효과 분리 검증
> 작성일: 2026-03-28

---

## 1. 실험 목적

| # | 질문 | 판단 기준 |
|---|------|-----------|
| Q1 | ATR 배수 2.0→2.5 (TICKET-01)가 MDD를 실제로 줄였는가? | MDD 변화폭 ≥ 1%p |
| Q2 | 단일종목 regime 필터 (TICKET-02)가 bearish 구간 손실을 차단했는가? | regime_buy_blocks > 0 && MDD 개선 |
| Q3 | 포트폴리오 regime 필터 (TICKET-05)가 포트폴리오 MDD를 줄였는가? | 포트폴리오 MDD 개선 ≥ 2%p |
| Q4 | CAGR/Sharpe 희생이 과도하지 않은가? | CAGR 감소 ≤ 3%p, Sharpe 감소 ≤ 0.15 |
| Q5 | 세 변경의 상호작용 효과가 있는가? | 조합 효과 ≠ 개별 효과의 합 |

---

## 2. OOS 기간 및 종목군 고정

### 기간

| 구분 | 기간 | 용도 |
|------|------|------|
| IS (In-Sample) | 2021-01-01 ~ 2023-12-31 | 파라미터 튜닝 구간 — 이 실험에서는 사용 안 함 |
| **OOS (Out-of-Sample)** | **2024-01-01 ~ 2025-12-31** | **ablation 비교 전용** |
| 검증 보조 | 2023-01-01 ~ 2025-12-31 | 3년 풀 기간 (선택 실행) |

> OOS 2년이 최소 기준. bear/bull 구간이 모두 포함되어야 regime 필터 효과 측정 가능.

### 종목군

**포트폴리오 실험 (primary)**

| 바스켓명 | 종목코드 | 종목명 | 선정 이유 |
|----------|----------|--------|-----------|
| KR_CORE_5 | 005930,000660,035720,051910,006400 | 삼성전자,SK하이닉스,카카오,LG화학,삼성SDI | 대형주+성장주 혼합, 유동성 충분 |

**단일종목 실험 (secondary)**

| 종목코드 | 종목명 | 용도 |
|----------|--------|------|
| 005930 | 삼성전자 | 대형 가치주 대표 |
| 035720 | 카카오 | 변동성 높은 성장주 |

---

## 3. 실험 매트릭스

8개 조합 × 2 실험 유형 (포트폴리오 + 단일종목)

### 3.1 포트폴리오 실험 (primary)

| ID | 실험명 | TICKET-01 (ATR 2.5) | TICKET-02 (단일 regime) | TICKET-05 (포트 regime) | `atr_multiplier` | `backtest_regime_filter.enabled` |
|----|--------|:---:|:---:|:---:|---|---|
| P0 | **baseline** | OFF | OFF | OFF | 2.0 | false |
| P1 | ATR only | **ON** | OFF | OFF | 2.5 | false |
| P2 | single-regime only | OFF | **ON** | OFF | 2.0 | true |
| P3 | port-regime only | OFF | OFF | **ON** | 2.0 | true |
| P4 | ATR + single-regime | **ON** | **ON** | OFF | 2.5 | true |
| P5 | ATR + port-regime | **ON** | OFF | **ON** | 2.5 | true |
| P6 | both regimes (no ATR) | OFF | **ON** | **ON** | 2.0 | true |
| P7 | **all ON (current)** | **ON** | **ON** | **ON** | 2.5 | true |

> **중요**: TICKET-02(단일종목 backtester의 regime)와 TICKET-05(portfolio_backtester의 regime)는 **같은 config key** `backtest_regime_filter.enabled`를 공유한다.
> 포트폴리오 모드에서는 `portfolio_backtester.py`가 자체적으로 regime을 적용하므로, `enabled: true`이면 TICKET-05가 자동 활성화된다.
> 따라서 포트폴리오 실험에서 P2(TICKET-02만 ON, TICKET-05 OFF)는 **현재 config 구조로는 분리 불가** — 코드 수준 패치 필요.
>
> **실용적 단순화**: P2와 P3은 포트폴리오 모드에서 동일 결과. 아래 실행 계획에서는 P2=P3으로 병합하고 7개 실험으로 진행.

### 3.2 단일종목 실험 (secondary)

| ID | 실험명 | TICKET-01 (ATR 2.5) | TICKET-02 (단일 regime) | `atr_multiplier` | `backtest_regime_filter.enabled` |
|----|--------|:---:|:---:|---|---|
| S0 | baseline | OFF | OFF | 2.0 | false |
| S1 | ATR only | **ON** | OFF | 2.5 | false |
| S2 | regime only | OFF | **ON** | 2.0 | true |
| S3 | **all ON** | **ON** | **ON** | 2.5 | true |

> TICKET-05는 단일종목 backtester에 영향 없음 → 4개 조합이면 충분.

---

## 4. 각 케이스에서 변경할 설정값

`config/risk_params.yaml`의 두 키만 토글:

```yaml
# TICKET-01 ON:
stop_loss:
  atr_multiplier: 2.5

# TICKET-01 OFF (baseline):
stop_loss:
  atr_multiplier: 2.0

# TICKET-02/05 ON:
backtest_regime_filter:
  enabled: true

# TICKET-02/05 OFF:
backtest_regime_filter:
  enabled: false
```

자동화 스크립트가 `sed`로 토글한다 (섹션 9 참조).

---

## 5. 실행 순서

```
Phase 1: 포트폴리오 핵심 4개  (P0 → P1 → P3 → P7)
Phase 2: 포트폴리오 보조 3개  (P4 → P5 → P6)
Phase 3: 단일종목 4개         (S0-005930 → S1 → S2 → S3)
Phase 4: 단일종목 카카오 4개  (S0-035720 → S1 → S2 → S3)
```

**Phase 1을 먼저 돌리는 이유**: baseline(P0)과 현재상태(P7)의 차이가 유의미한지 빨리 확인. 유의미하지 않으면 나머지 실험 불필요.

---

## 6. 실제 실행 명령어

### 포트폴리오 모드

```bash
# --- P0: baseline (ATR 2.0, regime OFF) ---
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
sed -i 's/^  enabled: true\b/  enabled: false/' config/risk_params.yaml
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P0_baseline.log

# --- P1: ATR 2.5 only ---
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
# regime stays false
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P1_atr_only.log

# --- P3: regime ON only (ATR 2.0) ---
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
sed -i 's/^  enabled: false/  enabled: true/' config/risk_params.yaml
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P3_regime_only.log

# --- P7: all ON (ATR 2.5 + regime ON) = current state ---
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
# regime stays true
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P7_all_on.log

# --- P4: ATR 2.5 + regime ON (same as P7 for portfolio) ---
# 포트폴리오 모드에서 P4=P7. skip 가능.

# --- P5: ATR 2.5 + regime ON (same as P7 for portfolio) ---
# P5=P7. skip 가능.

# --- P6: regime ON + ATR 2.0 (same as P3 for portfolio) ---
# P6=P3. skip 가능.
```

> **참고**: 포트폴리오 모드에서는 TICKET-02/05가 분리 불가하므로 실질 유효 실험은 **P0, P1, P3, P7** 4개.

### 단일종목 모드

```bash
# --- S0: baseline (005930) ---
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
sed -i 's/^  enabled: true\b/  enabled: false/' config/risk_params.yaml
python main.py --mode backtest --strategy scoring \
  --symbol 005930 --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/S0_005930_baseline.log

# --- S1: ATR 2.5 only (005930) ---
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
python main.py --mode backtest --strategy scoring \
  --symbol 005930 --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/S1_005930_atr_only.log

# --- S2: regime ON only (005930) ---
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
sed -i 's/^  enabled: false/  enabled: true/' config/risk_params.yaml
python main.py --mode backtest --strategy scoring \
  --symbol 005930 --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/S2_005930_regime_only.log

# --- S3: all ON (005930) ---
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
python main.py --mode backtest --strategy scoring \
  --symbol 005930 --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/S3_005930_all_on.log

# --- 035720 카카오 반복 (S0~S3 동일 패턴) ---
# 위 명령어에서 --symbol 005930 → --symbol 035720, 로그명 변경
```

### 설정 복원

```bash
# 실험 완료 후 승인 기준선(P1)으로 복원
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
sed -i '/^backtest_regime_filter:/,/^[a-z]/ s/enabled: true/enabled: false/' config/risk_params.yaml
```

---

## 7. 반드시 수집할 지표

| # | 지표 | 키 (metrics dict) | 비고 |
|---|------|--------------------|------|
| 1 | CAGR (%) | `annual_return` | 연환산 수익률 |
| 2 | MDD (%) | `max_drawdown` | **1차 판단 기준** |
| 3 | Sharpe | `sharpe_ratio` | |
| 4 | Sortino | `sortino_ratio` | |
| 5 | Profit Factor | `profit_factor` | |
| 6 | total_trades | `total_trades` | 매도 거래 건수 |
| 7 | avg_positions | `avg_positions` | 포트폴리오 전용 |
| 8 | regime_buy_blocks | `regime_buy_blocks` | regime OFF일 때 0이어야 정상 |
| 9 | regime_caution_buys | `regime_caution_buys` | regime OFF일 때 0이어야 정상 |
| 10 | turnover (%) | `annual_turnover_pct` | 단일종목 backtester에만 존재. 포트폴리오는 **확인 필요** |
| 11 | 거래비용 반영 순수익 | `final_value - initial_capital` | final_value에 이미 commission+tax+slippage 반영됨 |
| 12 | Win Rate (%) | `win_rate` | |
| 13 | Calmar | `calmar_ratio` | |

> **주의**: `annual_turnover_pct`는 `portfolio_backtester.py`의 metrics dict에 **없음**. 포트폴리오에서 turnover가 필요하면 `total_trades × avg_trade_size / avg_equity`로 수동 계산하거나 코드 패치 필요.

---

## 8. 결과 기록 포맷

### 8.1 CSV 포맷 (`results.csv`)

```csv
exp_id,exp_name,mode,symbol,atr_mult,regime_enabled,start,end,cagr_pct,mdd_pct,sharpe,sortino,profit_factor,calmar,total_trades,win_rate,avg_positions,regime_buy_blocks,regime_caution_buys,final_value,net_profit,timestamp
P0,baseline,portfolio,KR_CORE_5,2.0,false,2024-01-01,2025-12-31,,,,,,,,,,,,,2026-03-28T10:00:00
```

### 8.2 Markdown 비교 표 (포트폴리오)

```markdown
| ID | 실험명 | ATR | Regime | CAGR% | MDD% | Sharpe | Sortino | PF | Trades | Blocks | Caution | Net₩ |
|----|--------|-----|--------|-------|------|--------|---------|----|--------|--------|---------|------|
| P0 | baseline | 2.0 | OFF | | | | | | | 0 | 0 | |
| P1 | ATR only | 2.5 | OFF | | | | | | | 0 | 0 | |
| P3 | regime only | 2.0 | ON | | | | | | | | | |
| P7 | all ON | 2.5 | ON | | | | | | | | | |
```

### 8.3 Delta 표 (baseline 대비)

```markdown
| ID | ΔCAGR | ΔMDD | ΔSharpe | ΔSortino | ΔPF | ΔTrades | 판정 |
|----|-------|------|---------|----------|-----|---------|------|
| P1 | | | | | | | |
| P3 | | | | | | | |
| P7 | | | | | | | |
```

---

## 9. 승인 / 보류 / 롤백 기준

### 9.1 승인 기준 (APPROVE) — 모든 조건 충족

| 조건 | 기준 | 적용 대상 |
|------|------|-----------|
| MDD 개선 | P7 MDD < P0 MDD (최소 1%p 차이) | 포트폴리오 |
| CAGR 허용 손실 | P7 CAGR ≥ P0 CAGR - 3%p | 포트폴리오 |
| Sharpe 허용 손실 | P7 Sharpe ≥ P0 Sharpe - 0.15 | 포트폴리오 |
| Profit Factor | P7 PF ≥ 1.0 | 포트폴리오 |
| 거래 건수 | P7 trades ≥ P0 trades × 0.5 (너무 적지 않을 것) | 포트폴리오 |
| Regime 작동 | regime_buy_blocks > 0 (필터가 실제로 작동했음) | 포트폴리오 |

### 9.2 보류 기준 (HOLD) — 하나라도 해당

| 조건 | 설명 |
|------|------|
| MDD 개선 없음 | P7 MDD ≥ P0 MDD |
| CAGR 과다 희생 | P7 CAGR < P0 CAGR - 3%p 이지만 MDD 개선은 있음 |
| 조합 효과 비선형 | P7 효과 ≠ P1+P3 개별 효과 합 (상호작용 분석 필요) |
| 거래 과소 | P7 trades < 10 (통계적 유의성 부족) |

→ 보류 시: 파라미터 미세조정 후 재실험 (예: caution_scale 0.3~0.7 그리드)

### 9.3 롤백 기준 (ROLLBACK) — 하나라도 해당

| 조건 | 설명 |
|------|------|
| MDD 악화 | P7 MDD > P0 MDD + 1%p |
| Sharpe 급락 | P7 Sharpe < P0 Sharpe - 0.30 |
| 거래 소멸 | P7 trades < 5 |
| Regime 미작동 | regime_buy_blocks = 0 AND regime_caution_buys = 0 (bearish 구간이 있었는데도) |

---

## 10. 실험 자동화 스크립트 초안

```bash
#!/bin/bash
# experiments/ablation_study_v1/run_ablation.sh
# 사용법: bash experiments/ablation_study_v1/run_ablation.sh
set -euo pipefail

PROJ_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="$PROJ_ROOT/config/risk_params.yaml"
LOGDIR="$PROJ_ROOT/experiments/ablation_study_v1/logs"
RESULTCSV="$PROJ_ROOT/experiments/ablation_study_v1/results.csv"

mkdir -p "$LOGDIR"

# CSV 헤더
echo "exp_id,exp_name,mode,symbols,atr_mult,regime_enabled,start,end,timestamp" > "$RESULTCSV"

# --- 헬퍼 함수 ---
set_atr() {
  # $1 = target value (2.0 or 2.5)
  sed -i "s/atr_multiplier: [0-9.]\+/atr_multiplier: $1/" "$CONFIG"
}

set_regime() {
  # $1 = true or false
  # backtest_regime_filter 블록의 enabled만 변경
  # 주의: 다른 enabled 키가 있으므로 정확한 줄을 타겟팅
  sed -i "/^backtest_regime_filter:/,/^[a-z]/ s/enabled: \(true\|false\)/enabled: $1/" "$CONFIG"
}

run_portfolio() {
  local exp_id=$1
  local exp_name=$2
  local atr=$3
  local regime=$4

  echo "========================================"
  echo "[$exp_id] $exp_name  ATR=$atr  REGIME=$regime"
  echo "========================================"

  set_atr "$atr"
  set_regime "$regime"

  python "$PROJ_ROOT/main.py" --mode portfolio_backtest --strategy scoring \
    --symbols 005930,000660,035720,051910,006400 \
    --start 2024-01-01 --end 2025-12-31 \
    2>&1 | tee "$LOGDIR/${exp_id}_${exp_name}.log"

  echo "$exp_id,$exp_name,portfolio,KR_CORE_5,$atr,$regime,2024-01-01,2025-12-31,$(date -Iseconds)" >> "$RESULTCSV"
}

run_single() {
  local exp_id=$1
  local exp_name=$2
  local symbol=$3
  local atr=$4
  local regime=$5

  echo "========================================"
  echo "[$exp_id] $exp_name  SYM=$symbol  ATR=$atr  REGIME=$regime"
  echo "========================================"

  set_atr "$atr"
  set_regime "$regime"

  python "$PROJ_ROOT/main.py" --mode backtest --strategy scoring \
    --symbol "$symbol" \
    --start 2024-01-01 --end 2025-12-31 \
    2>&1 | tee "$LOGDIR/${exp_id}_${exp_name}.log"

  echo "$exp_id,$exp_name,single,$symbol,$atr,$regime,2024-01-01,2025-12-31,$(date -Iseconds)" >> "$RESULTCSV"
}

# ============================================================
# Phase 1: 포트폴리오 핵심
# ============================================================
run_portfolio P0 baseline      2.0 false
run_portfolio P1 atr_only      2.5 false
run_portfolio P3 regime_only   2.0 true
run_portfolio P7 all_on        2.5 true

# ============================================================
# Phase 2: 단일종목 — 삼성전자
# ============================================================
run_single S0_005930 baseline    005930 2.0 false
run_single S1_005930 atr_only    005930 2.5 false
run_single S2_005930 regime_only 005930 2.0 true
run_single S3_005930 all_on      005930 2.5 true

# ============================================================
# Phase 3: 단일종목 — 카카오
# ============================================================
run_single S0_035720 baseline    035720 2.0 false
run_single S1_035720 atr_only    035720 2.5 false
run_single S2_035720 regime_only 035720 2.0 true
run_single S3_035720 all_on      035720 2.5 true

# ============================================================
# 설정 복원 (승인 기준선 = P1: ATR 2.5, regime OFF)
# ============================================================
set_atr 2.5
set_regime false

echo ""
echo "============================================"
echo "  Ablation study 완료. 로그: $LOGDIR"
echo "  결과 CSV: $RESULTCSV"
echo "============================================"
echo ""
echo "다음 단계: 각 로그에서 metrics를 추출하여 results.csv에 수동 기입"
echo "  또는 아래 명령으로 Claude에게 결과 분석 요청:"
echo '  cat experiments/ablation_study_v1/logs/*.log | grep -E "연환산|MDD|샤프|소르티노|손익비|거래 수|regime"'
```

---

## 11. 결과 요약 템플릿 (실험 후 붙여넣기용)

실험 완료 후 아래 템플릿을 채워서 붙여넣으면 분석을 바로 시작합니다.

````markdown
## Ablation Results — 포트폴리오 (KR_CORE_5, 2024-01-01 ~ 2025-12-31)

| ID | 실험명 | ATR | Regime | CAGR% | MDD% | Sharpe | Sortino | PF | Calmar | Trades | WinRate% | AvgPos | Blocks | Caution | FinalValue |
|----|--------|-----|--------|-------|------|--------|---------|-----|--------|--------|----------|--------|--------|---------|------------|
| P0 | baseline | 2.0 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| P1 | ATR only | 2.5 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| P3 | regime only | 2.0 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |
| P7 | all ON | 2.5 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |

## Ablation Results — 단일종목 삼성전자 (005930)

| ID | ATR | Regime | CAGR% | MDD% | Sharpe | Sortino | PF | Trades | Blocks | Caution | FinalValue |
|----|-----|--------|-------|------|--------|---------|-----|--------|--------|---------|------------|
| S0 | 2.0 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| S1 | 2.5 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| S2 | 2.0 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |
| S3 | 2.5 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |

## Ablation Results — 단일종목 카카오 (035720)

| ID | ATR | Regime | CAGR% | MDD% | Sharpe | Sortino | PF | Trades | Blocks | Caution | FinalValue |
|----|-----|--------|-------|------|--------|---------|-----|--------|--------|---------|------------|
| S0 | 2.0 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| S1 | 2.5 | OFF | ___ | ___ | ___ | ___ | ___ | ___ | 0 | 0 | ___ |
| S2 | 2.0 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |
| S3 | 2.5 | ON | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |

## 특이사항
- (여기에 실행 중 에러, 경고, 데이터 누락 등 기록)

## 내 판단
- 승인 / 보류 / 롤백: ___
- 이유: ___
````

---

## 12. 가장 먼저 돌릴 3개 실험

| 우선순위 | 실험 ID | 실험명 | 이유 |
|----------|---------|--------|------|
| **1st** | **P0** | 포트폴리오 baseline | 비교 기준. 이것 없이는 아무것도 판단 불가 |
| **2nd** | **P7** | 포트폴리오 all ON | 현재 상태. baseline과의 차이가 전체 효과 크기 |
| **3rd** | **P3** | 포트폴리오 regime only | ATR vs regime 중 MDD 개선의 주된 원인 식별 |

**실행 명령 (이 3개만 빠르게):**

```bash
# 먼저 로그 디렉토리 생성
mkdir -p experiments/ablation_study_v1/logs

# 1) P0 baseline
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
sed -i '/^backtest_regime_filter:/,/^[a-z]/ s/enabled: true/enabled: false/' config/risk_params.yaml
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P0_baseline.log

# 2) P7 all ON
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
sed -i '/^backtest_regime_filter:/,/^[a-z]/ s/enabled: false/enabled: true/' config/risk_params.yaml
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P7_all_on.log

# 3) P3 regime only
sed -i 's/atr_multiplier: 2.5/atr_multiplier: 2.0/' config/risk_params.yaml
python main.py --mode portfolio_backtest --strategy scoring \
  --symbols 005930,000660,035720,051910,006400 \
  --start 2024-01-01 --end 2025-12-31 \
  2>&1 | tee experiments/ablation_study_v1/logs/P3_regime_only.log

# 설정 복원 (승인 기준선 P1: ATR 2.5, regime OFF)
sed -i 's/atr_multiplier: 2.0/atr_multiplier: 2.5/' config/risk_params.yaml
sed -i '/^backtest_regime_filter:/,/^[a-z]/ s/enabled: true/enabled: false/' config/risk_params.yaml
```

> P0 vs P7로 전체 효과 크기 확인 → P3으로 regime 단독 기여 분리 → 나머지 실험 필요성 판단

---

## 부록: 가설 정리

| 변경 | 가설 효과 | 기대 수치 (가설) |
|------|-----------|-----------------|
| TICKET-01 (ATR 2.5) | 정상 조정에 의한 불필요한 손절 감소 → MDD 소폭 개선, 거래 수 감소 | MDD -0.5~1.5%p (가설) |
| TICKET-02 (단일 regime) | bearish 구간 진입 차단 → 손실 구간 회피 | MDD -2~5%p (가설) |
| TICKET-05 (포트 regime) | 포트폴리오 전체에 regime 적용 → 동시다발 진입 차단 | MDD -3~7%p (가설) |
| 조합 효과 | 상호 보완: ATR은 개별 포지션 보호, regime은 시장 수준 보호 | 비선형 상호작용 가능 (가설) |

**모든 기대 수치는 가설이며, 실험 결과로 대체됩니다.**

---

## 13. 실험 결과 (ATR 경로 수정 후 재실행)

> 실행일: 2026-03-28 | ATR 손절 경로 정상화(commit 685bbb6) 이후 재측정
> OOS 기간: 2024-01-01 ~ 2025-12-31 | 포트폴리오: KR_CORE_5 (005930,000660,035720,051910,006400)

| ID | 실험명 | ATR | Regime | Return% | CAGR% | MDD% | Sharpe | Sortino | PF | Calmar | Trades | WinRate% |
|----|--------|-----|--------|---------|-------|------|--------|---------|----|--------|--------|----------|
| P0 | baseline | 2.0 | OFF | 1.27 | 0.66 | -7.42 | -0.53 | -0.69 | 1.03 | 0.09 | 109 | 36.7 |
| **P1** | **ATR only** | **2.5** | **OFF** | **2.35** | **1.22** | **-5.44** | **-0.50** | **-0.68** | **1.11** | **0.22** | **108** | **36.1** |
| P7 | all ON | 2.5 | ON | 1.68 | 0.87 | -5.93 | -0.66 | -0.91 | 1.08 | 0.15 | 105 | 34.3 |

### Delta 표 (P0 baseline 대비)

| ID | ΔReturn | ΔCAGR | ΔMDD | ΔSharpe | ΔSortino | ΔPF | ΔTrades | 판정 |
|----|---------|-------|------|---------|----------|-----|---------|------|
| **P1** | **+1.08** | **+0.56** | **+1.98** | **+0.03** | **+0.01** | **+0.08** | **-1** | **ADOPT** |
| P7 | +0.41 | +0.21 | +1.49 | -0.13 | -0.22 | +0.05 | -4 | REJECT |

### P1 Exit Reason 분포

| Exit Reason | 건수 |
|-------------|------|
| TRAILING_STOP | 67 |
| TAKE_PROFIT | 34 |
| STOP_LOSS | 3 |
| SELL | 3 |
| MAX_HOLD | 1 |

---

## 14. 의사결정 로그

```
일자: 2026-03-28
결정: P7(regime ON) REJECTED → P1(ATR 2.5, regime OFF) ADOPTED as baseline
사유:
1. P7은 P1 대비 수익률 -0.67%p, MDD +0.49%p, Sharpe -0.16, Sortino -0.23 전 지표 열위.
2. regime 필터가 bearish 7건 차단, caution 39건 축소했으나 오히려 bullish 기회도 위축시켜 순효과 음수.
3. ATR 2.5 단독(P1)이 P0 대비 MDD 1.98%p 개선 + 수익률 1.08%p 상승 — 충분한 단독 기여.
4. regime 필터는 현재 MA200 단일지표 기반으로 정밀도 부족 — 재설계 없이 활성화할 근거 없음.
5. 코드는 feature flag(enabled: false)로 보존하여 향후 재실험 가능.
승인자: 프로젝트 오너
```

### RUNBOOK 최종 결론

> **Regime 필터(TICKET-02/05)는 현재 구현 수준에서 포트폴리오 성과를 개선하지 못한다. 기본값 OFF로 롤백하고, ATR 2.5(TICKET-01) 단독 적용을 승인 기준선(P1)으로 확정한다. Regime 로직은 삭제하지 않고 feature flag로 보존하며, 멀티팩터 국면 판별기 재설계 후 재실험한다.**

---

## 15. 승인 기준선 요약 — P1 (ATR 2.5, Regime OFF)

```
Approved Baseline: P1
Config: atr_multiplier=2.5, backtest_regime_filter.enabled=false
OOS Period: 2024-01-01 ~ 2025-12-31
Universe: KR_CORE_5 (005930, 000660, 035720, 051910, 006400)
Initial Capital: 10,000,000 KRW

total_return    :    2.35%
CAGR            :    1.22%
MDD             :   -5.44%
Sharpe          :   -0.50
Sortino         :   -0.68
Profit Factor   :    1.11
Calmar          :    0.22
total_trades    :  108
win_rate        :   36.1%
STOP_LOSS       :    3건 (2.8%)
TAKE_PROFIT     :   34건 (31.5%)
TRAILING_STOP   :   67건 (62.0%)
SELL            :    3건 (2.8%)
MAX_HOLD        :    1건 (0.9%)
regime_buy_blocks   : 0
regime_caution_buys : 0
```

---

## 16. 후속 Backlog

| # | 티켓 | 설명 | 우선순위 |
|---|------|------|----------|
| BL-01 | regime 재설계 | MA200 단일지표 → 멀티팩터(VIX+신용스프레드+breadth) 국면 판별기. IS/OOS 분리 재실험 | P2 |
| BL-02 | 전략 알파 개선 | 현재 Sharpe -0.50으로 위험조정수익 음수. 팩터 가중치 최적화 + 섹터 로테이션 검토 | P1 |
| BL-03 | turnover/노출 메트릭 보강 | portfolio_backtester에 annual_turnover_pct, gross/net exposure 추가. 과매매 진단 자동화 | P2 |

#!/bin/bash
# Ablation Study 자동화 스크립트
# 사용법: bash experiments/ablation_study_v1/run_ablation.sh
# 부분 실행: bash experiments/ablation_study_v1/run_ablation.sh --phase 1
set -euo pipefail

PROJ_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG="$PROJ_ROOT/config/risk_params.yaml"
LOGDIR="$PROJ_ROOT/experiments/ablation_study_v1/logs"
RESULTCSV="$PROJ_ROOT/experiments/ablation_study_v1/results.csv"
PYTHON="/c/ProgramData/anaconda3/envs/quant/python.exe"
PHASE="${1:-all}"  # --phase 인자 또는 "all"

mkdir -p "$LOGDIR"

# --- 설정 백업 ---
cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"

# CSV 헤더 (파일 없을 때만)
if [ ! -f "$RESULTCSV" ]; then
  echo "exp_id,exp_name,mode,symbols,atr_mult,regime_enabled,start,end,timestamp" > "$RESULTCSV"
fi

# --- 헬퍼 ---
set_atr() {
  sed -i "s/atr_multiplier: [0-9.]\+/atr_multiplier: $1/" "$CONFIG"
  echo "  → atr_multiplier = $1"
}

set_regime() {
  sed -i "/^backtest_regime_filter:/,/^[a-z]/ s/enabled: \(true\|false\)/enabled: $1/" "$CONFIG"
  echo "  → backtest_regime_filter.enabled = $1"
}

verify_config() {
  echo "  [검증] 현재 config:"
  grep "atr_multiplier" "$CONFIG" | head -1
  grep -A1 "backtest_regime_filter:" "$CONFIG" | grep enabled
}

run_portfolio() {
  local exp_id=$1 exp_name=$2 atr=$3 regime=$4
  echo ""
  echo "========================================"
  echo "  [$exp_id] $exp_name"
  echo "  mode=portfolio  ATR=$atr  REGIME=$regime"
  echo "========================================"
  set_atr "$atr"
  set_regime "$regime"
  verify_config

  local logfile="$LOGDIR/${exp_id}_${exp_name}.log"
  "$PYTHON" "$PROJ_ROOT/main.py" --mode portfolio_backtest --strategy scoring \
    --symbols 005930,000660,035720,051910,006400 \
    --start 2024-01-01 --end 2025-12-31 \
    2>&1 | tee "$logfile"

  echo "$exp_id,$exp_name,portfolio,KR_CORE_5,$atr,$regime,2024-01-01,2025-12-31,$(date -Iseconds)" >> "$RESULTCSV"
  echo "  → 로그 저장: $logfile"
}

run_single() {
  local exp_id=$1 exp_name=$2 symbol=$3 atr=$4 regime=$5
  echo ""
  echo "========================================"
  echo "  [$exp_id] $exp_name"
  echo "  mode=single  SYM=$symbol  ATR=$atr  REGIME=$regime"
  echo "========================================"
  set_atr "$atr"
  set_regime "$regime"
  verify_config

  local logfile="$LOGDIR/${exp_id}_${exp_name}.log"
  "$PYTHON" "$PROJ_ROOT/main.py" --mode backtest --strategy scoring \
    --symbol "$symbol" \
    --start 2024-01-01 --end 2025-12-31 \
    2>&1 | tee "$logfile"

  echo "$exp_id,$exp_name,single,$symbol,$atr,$regime,2024-01-01,2025-12-31,$(date -Iseconds)" >> "$RESULTCSV"
  echo "  → 로그 저장: $logfile"
}

# ============================================================
# Phase 1: 포트폴리오 핵심 4개 (가장 먼저)
# ============================================================
if [ "$PHASE" = "all" ] || [ "$PHASE" = "--phase" ] && [ "${2:-}" = "1" ] || [ "$PHASE" = "1" ]; then
  echo ""
  echo "████████████████████████████████████████"
  echo "  Phase 1: 포트폴리오 핵심 실험"
  echo "████████████████████████████████████████"
  run_portfolio P0 baseline    2.0 false
  run_portfolio P1 atr_only    2.5 false
  run_portfolio P3 regime_only 2.0 true
  run_portfolio P7 all_on      2.5 true
fi

# ============================================================
# Phase 2: 단일종목 — 삼성전자 (005930)
# ============================================================
if [ "$PHASE" = "all" ] || [ "${2:-}" = "2" ] || [ "$PHASE" = "2" ]; then
  echo ""
  echo "████████████████████████████████████████"
  echo "  Phase 2: 단일종목 삼성전자"
  echo "████████████████████████████████████████"
  run_single S0_005930 baseline    005930 2.0 false
  run_single S1_005930 atr_only    005930 2.5 false
  run_single S2_005930 regime_only 005930 2.0 true
  run_single S3_005930 all_on      005930 2.5 true
fi

# ============================================================
# Phase 3: 단일종목 — 카카오 (035720)
# ============================================================
if [ "$PHASE" = "all" ] || [ "${2:-}" = "3" ] || [ "$PHASE" = "3" ]; then
  echo ""
  echo "████████████████████████████████████████"
  echo "  Phase 3: 단일종목 카카오"
  echo "████████████████████████████████████████"
  run_single S0_035720 baseline    035720 2.0 false
  run_single S1_035720 atr_only    035720 2.5 false
  run_single S2_035720 regime_only 035720 2.0 true
  run_single S3_035720 all_on      035720 2.5 true
fi

# ============================================================
# 설정 복원
# ============================================================
echo ""
echo "========================================"
echo "  설정 복원 (현재 상태 = all ON)"
echo "========================================"
set_atr 2.5
set_regime true
verify_config

echo ""
echo "============================================"
echo "  Ablation study 완료!"
echo "  로그 디렉토리: $LOGDIR"
echo "  결과 CSV:     $RESULTCSV"
echo "============================================"
echo ""
echo "결과 추출 명령어:"
echo "  grep -E '연환산|최대 낙폭|샤프|소르티노|손익비|총 거래|regime|평균 보유' $LOGDIR/*.log"

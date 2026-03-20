"""
스코어링 지표 독립성 검증.
- 스코어링에 사용하는 지표(score_rsi, score_macd, score_bollinger, score_volume, score_ma) 간
  상관계수를 계산하고, 상관계수 0.7 이상인 쌍을 찾아 하나 제거 또는 가중치 축소를 권고.
- 대부분의 지표는 가격·이동평균 변형이므로 다중공선성 위험: 실질적으로 독립 정보는
  (1) 가격 모멘텀 (MA 또는 MACD 중 하나), (2) 변동성 (볼린저/ATR), (3) 거래량 — 3그룹 각 1개만 권장.
"""

from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import pandas as pd
from loguru import logger

# 스코어링에서 사용하는 개별 점수 컬럼 (상관 분석 대상)
SCORE_COLUMNS = ["score_rsi", "score_macd", "score_bollinger", "score_volume", "score_ma"]
# 사용자 노출용 이름
SCORE_LABELS = {
    "score_rsi": "RSI",
    "score_macd": "MACD",
    "score_bollinger": "볼린저",
    "score_volume": "거래량",
    "score_ma": "이동평균",
}

# 실질적 독립 정보 3그룹 (그룹당 대표 1개만 두면 다중공선성 완화)
# 가격 모멘텀: RSI, MACD, MA — 같은 "가격 추세/강도" 정보 변형. 스토캐스틱 추가 시 RSI와 고상관.
# 변동성: 볼린저 (ATR은 현재 스코어링 미사용)
# 거래량: volume — 가격 외 독립
INDICATOR_GROUPS = {
    "가격 모멘텀": ["score_rsi", "score_macd", "score_ma"],
    "변동성": ["score_bollinger"],
    "거래량": ["score_volume"],
}


def compute_score_correlation_matrix(df_scores: pd.DataFrame) -> pd.DataFrame:
    """
    스코어 컬럼만 추출해 상관계수 행렬 계산.
    df_scores: score_rsi, score_macd, score_bollinger, score_volume, score_ma 컬럼이 있는 DataFrame
    """
    cols = [c for c in SCORE_COLUMNS if c in df_scores.columns]
    if len(cols) < 2:
        return pd.DataFrame()
    return df_scores[cols].corr()


def get_high_correlation_pairs(
    corr_matrix: pd.DataFrame,
    threshold: float = 0.7,
) -> List[Tuple[str, str, float]]:
    """
    상관계수 행렬에서 |r| >= threshold 인 쌍을 (이름1, 이름2, r) 형태로 반환.
    중복 제거: (A,B) 만 반환하고 (B,A) 는 제외.
    """
    if corr_matrix.empty:
        return []
    pairs = []
    seen = set()
    for i, col1 in enumerate(corr_matrix.columns):
        for j, col2 in enumerate(corr_matrix.columns):
            if i >= j:
                continue
            r = corr_matrix.loc[col1, col2]
            if pd.isna(r):
                continue
            key = (min(col1, col2), max(col1, col2))
            if key in seen:
                continue
            if abs(r) >= threshold:
                seen.add(key)
                pairs.append((col1, col2, round(float(r), 3)))
    return pairs


def recommend_reduction(
    high_pairs: List[Tuple[str, str, float]],
) -> List[dict]:
    """
    고상관 쌍에 대해 '하나 제거 또는 가중치 축소' 권고를 생성.
    가격 모멘텀 그룹(RSI/MACD/MA) 내 쌍은 같은 정보를 세는 것이므로 그룹당 1개만 남기고
    나머지는 가중치 0 권장. 변동성(볼린저)·거래량은 독립이므로 유지.
    """
    recommendations = []
    for col1, col2, r in high_pairs:
        label1 = SCORE_LABELS.get(col1, col1)
        label2 = SCORE_LABELS.get(col2, col2)
        action = "제거 권고" if abs(r) >= 0.85 else "가중치 축소 권고"
        group_note = (
            "가격 모멘텀 그룹(RSI/MACD/MA) 내 고상관: 그룹당 대표 1개만 두고 나머지는 가중치 0 권장. "
            "실질적 독립 정보는 (1) 가격모멘텀 (2) 변동성 (3) 거래량 3그룹 각 1개만 두세요."
        )
        recommendations.append({
            "col1": col1,
            "col2": col2,
            "label1": label1,
            "label2": label2,
            "correlation": r,
            "action": action,
            "suggestion": (
                f"{label1}–{label2} 상관계수 {r:.2f}. "
                f"{action}. "
                f"{group_note}"
            ),
        })
    return recommendations


def run_indicator_correlation_check(
    df_or_scores: pd.DataFrame,
    threshold: float = 0.7,
    symbol: str = "005930",
    config=None,
    output_dir: str = "reports",
    save_report: bool = False,
    start: str = None,
    end: str = None,
) -> dict:
    """
    지표 독립성 검증 실행.
    - 입력이 원시 OHLCV면 IndicatorEngine + SignalGenerator로 score 시리즈 생성
    - 입력이 score DataFrame이면 그대로 사용
    - Pearson 상관계수(df.corr) 계산
    - |r|>=threshold 고상관 쌍 추출 및 권고 생성
    - save_report=True면 reports/indicator_correlation_{날짜}.txt 저장

    Returns:
        {
            "corr_matrix": DataFrame,
            "high_correlation_pairs": [(col1, col2, r), ...],
            "recommendations": [{"col1", "col2", "label1", "label2", "correlation", "suggestion"}, ...],
            "n_high": int,
        }
    """
    if df_or_scores is None or df_or_scores.empty:
        return {
            "corr_matrix": pd.DataFrame(),
            "high_correlation_pairs": [],
            "recommendations": [],
            "n_high": 0,
            "threshold": threshold,
            "symbol": symbol,
            "report_path": None,
        }

    has_score_cols = any(c in df_or_scores.columns for c in SCORE_COLUMNS)
    if has_score_cols:
        df_scores = df_or_scores.copy()
    else:
        from config.config_loader import Config
        from core.indicator_engine import IndicatorEngine
        from core.signal_generator import SignalGenerator

        cfg = config or Config.get()
        engine = IndicatorEngine(cfg)
        generator = SignalGenerator(cfg)
        df_ind = engine.calculate_all(df_or_scores.copy())
        # representative_only 등으로 generate() 후에는 RSI·MA 점수가 0 고정 → 상관 NaN.
        # 실매매 total_score와 무관하게, collinearity 적용 전 개별 점수로 Pearson 상관만 계산.
        df_scores = generator.compute_score_columns_for_correlation(df_ind)

    corr_matrix = compute_score_correlation_matrix(df_scores)
    high_pairs = get_high_correlation_pairs(corr_matrix, threshold=threshold)
    recommendations = recommend_reduction(high_pairs)
    for col1, col2, r in high_pairs:
        logger.info(
            "[indicator_correlation] 고상관 쌍 발견: {}-{} (r={:.3f})",
            SCORE_LABELS.get(col1, col1),
            SCORE_LABELS.get(col2, col2),
            r,
        )

    result = {
        "corr_matrix": corr_matrix,
        "high_correlation_pairs": high_pairs,
        "recommendations": recommendations,
        "n_high": len(high_pairs),
        "threshold": threshold,
        "symbol": symbol,
        "report_path": None,
    }
    if save_report:
        report_text = render_correlation_report(result, symbol=symbol, start=start, end=end)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = out_dir / f"indicator_correlation_{timestamp}.txt"
        report_path.write_text(report_text, encoding="utf-8")
        result["report_path"] = str(report_path)
    return result


SCORE_COL_TO_WEIGHT_KEY = {
    "score_rsi": "w_rsi",
    "score_macd": "w_macd",
    "score_bollinger": "w_bollinger",
    "score_volume": "w_volume",
    "score_ma": "w_ma",
}


def suggest_disable_weights(
    high_pairs: List[Tuple[str, str, float]],
) -> List[str]:
    """
    고상관 쌍에서 자동 비활성화할 가중치 키(w_*) 목록 반환.

    전략: 가격 모멘텀 그룹(RSI/MACD/MA) 내 고상관 쌍이 있으면
    가격 모멘텀 그룹에서 MACD를 대표로 남기고 나머지(RSI, MA)를 비활성화.
    (MACD는 추세 강도+방향+모멘텀을 함께 제공하므로 대표성이 가장 높음)
    """
    if not high_pairs:
        return []

    momentum_group = {"score_rsi", "score_macd", "score_ma"}
    momentum_representative = "score_macd"
    to_disable = set()

    for col1, col2, r in high_pairs:
        if col1 in momentum_group and col2 in momentum_group:
            for col in (col1, col2):
                if col != momentum_representative:
                    wk = SCORE_COL_TO_WEIGHT_KEY.get(col)
                    if wk:
                        to_disable.add(wk)
        else:
            for col in (col1, col2):
                if col in momentum_group and col != momentum_representative:
                    wk = SCORE_COL_TO_WEIGHT_KEY.get(col)
                    if wk:
                        to_disable.add(wk)
    return sorted(to_disable)


def build_next_step_commands(
    symbol: str,
    disable_weights: List[str],
    start: str = None,
    end: str = None,
) -> dict:
    """상관 분석 후 다음 단계 CLI 명령어 생성."""
    dw_arg = f" --disable-weights {','.join(disable_weights)}" if disable_weights else ""
    date_args = ""
    if start:
        date_args += f" --start {start}"
    if end:
        date_args += f" --end {end}"

    step2 = (
        f"python main.py --mode optimize --strategy scoring --include-weights"
        f" --symbol {symbol}{dw_arg}{date_args}"
    )
    step3 = (
        f"python main.py --mode validate --walk-forward --strategy scoring"
        f" --symbol {symbol} --validation-years 5"
    )
    step_auto = (
        f"python main.py --mode optimize --strategy scoring --include-weights"
        f" --auto-correlation --symbol {symbol}{date_args}"
    )
    return {
        "step2_optimize": step2,
        "step3_validate": step3,
        "step_auto": step_auto,
    }


def render_correlation_report(
    result: dict,
    symbol: str = "005930",
    start: str = None,
    end: str = None,
) -> str:
    """지표 독립성 검증 결과를 텍스트 리포트로 렌더링."""
    lines = [
        "=" * 70,
        "스코어링 지표 독립성 검증 (상관계수)",
        f"기준: |상관계수| >= {result['threshold']} 인 쌍을 고상관으로 판단",
        "=" * 70,
        "",
        "[참고] 상관계수는 실매매 total_score에 쓰이는 collinearity(representative_only 등) "
        "적용 **전** 개별 점수 시리즈(score_rsi, score_macd, …) 기준입니다.",
        "",
        "[다중공선성 안내] 대부분의 지표는 가격·이동평균 변형입니다. 스코어가 실질적으로",
        "1~2개 지표에 지배당하지 않으려면, 실질적 독립 정보 3그룹 각 대표 1개만 두는 것을 권장:",
        "  (1) 가격 모멘텀: MA 골든/데드크로스 또는 MACD (택 1)",
        "  (2) 변동성: 볼린저",
        "  (3) 거래량: volume_surge",
        "RSI·스토캐스틱은 둘 다 오실레이터로 고상관. MACD와 MA 크로스는 같은 추세 정보.",
        "",
    ]
    if result["corr_matrix"].empty:
        lines.append("스코어 컬럼 부족으로 상관계수 행렬을 계산할 수 없습니다.")
        return "\n".join(lines) + "\n"

    lines.append("\n[상관계수 행렬]")
    lines.append(result["corr_matrix"].to_string())
    lines.append(f"\n고상관 쌍 수: {result['n_high']}")

    if result["recommendations"]:
        lines.append("\n[권고] |r| >= 0.7 쌍 — 하나 제거(가중치 0) 또는 가중치 축소 권장")
        for rec in result["recommendations"]:
            lines.append(f"  - {rec['label1']}–{rec['label2']}: r={rec['correlation']:.2f}")
            lines.append(f"    → {rec.get('action', '권고')}")
            lines.append(f"    → {rec['suggestion']}")
    else:
        lines.append("\n고상관 쌍 없음. 현재 지표 조합은 독립성 기준을 만족합니다.")

    disable_weights = suggest_disable_weights(result.get("high_correlation_pairs", []))
    result["disable_weights"] = disable_weights
    cmds = build_next_step_commands(symbol, disable_weights, start, end)

    lines.append("")
    lines.append("=" * 70)
    lines.append("[자동 비활성화 대상 가중치]")
    if disable_weights:
        lines.append(f"  {', '.join(disable_weights)}")
        lines.append(f"  (가격 모멘텀 대표: MACD 유지, 나머지 제거)")
    else:
        lines.append("  없음 (모든 지표 유지)")

    lines.append("")
    lines.append("[다음 단계 — 가중치 최적화 파이프라인]")
    lines.append("  ① 상관 분석 (현재 단계) — 완료")
    lines.append(f"  ② 가중치 최적화 (OOS 샤프 ≥ 1.0 게이트):")
    lines.append(f"     {cmds['step2_optimize']}")
    lines.append(f"  ③ 워크포워드 검증 (여러 기간 안정성):")
    lines.append(f"     {cmds['step3_validate']}")
    lines.append("")
    lines.append("  [원스텝] 상관 분석 + 최적화를 한 번에 실행:")
    lines.append(f"     {cmds['step_auto']}")
    lines.append("")
    lines.append("=== 다음 단계 명령어 ===")
    lines.append(
        f"python main.py --mode optimize --strategy scoring --include-weights --auto-correlation --symbol {symbol}"
    )
    lines.append("=" * 70)

    return "\n".join(lines) + "\n"

"""
스코어링 지표 독립성 검증.
- 스코어링에 사용하는 지표(score_rsi, score_macd, score_bollinger, score_volume, score_ma) 간
  상관계수를 계산하고, 상관계수 0.7 이상인 쌍을 찾아 하나 제거 또는 가중치 축소를 권고.
"""

from typing import List, Tuple

import pandas as pd

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
    가격 강도 그룹(RSI/MACD/볼린저/MA) 내 쌍은 이미 다중공선성 완화로 그룹당 1개만 반영되므로,
    권고는 '가중치 축소' 또는 '둘 중 하나 비활성화(가중치 0)' 수준으로 제안.
    """
    recommendations = []
    for col1, col2, r in high_pairs:
        label1 = SCORE_LABELS.get(col1, col1)
        label2 = SCORE_LABELS.get(col2, col2)
        recommendations.append({
            "col1": col1,
            "col2": col2,
            "label1": label1,
            "label2": label2,
            "correlation": r,
            "suggestion": (
                f"{label1}–{label2} 상관계수 {r:.2f}. "
                "둘 중 하나 가중치 축소 또는 제거(0) 권장. "
                "가격 강도 그룹(RSI/MACD/볼린저/MA)은 이미 방향별 최대 1개만 반영되므로, "
                "동일 그룹 내 고상관은 strategies.yaml에서 해당 지표 가중치를 낮추는 것을 고려하세요."
            ),
        })
    return recommendations


def run_indicator_correlation_check(
    df_scores: pd.DataFrame,
    threshold: float = 0.7,
) -> dict:
    """
    스코어 DataFrame에 대해 상관계수 계산 및 고상관 쌍·권고 반환.

    Returns:
        {
            "corr_matrix": DataFrame,
            "high_correlation_pairs": [(col1, col2, r), ...],
            "recommendations": [{"col1", "col2", "label1", "label2", "correlation", "suggestion"}, ...],
            "n_high": int,
        }
    """
    corr_matrix = compute_score_correlation_matrix(df_scores)
    high_pairs = get_high_correlation_pairs(corr_matrix, threshold=threshold)
    recommendations = recommend_reduction(high_pairs)
    return {
        "corr_matrix": corr_matrix,
        "high_correlation_pairs": high_pairs,
        "recommendations": recommendations,
        "n_high": len(high_pairs),
        "threshold": threshold,
    }


def render_correlation_report(result: dict) -> str:
    """지표 독립성 검증 결과를 텍스트 리포트로 렌더링."""
    lines = [
        "=" * 70,
        "스코어링 지표 독립성 검증 (상관계수)",
        f"기준: |상관계수| >= {result['threshold']} 인 쌍을 고상관으로 판단",
        "=" * 70,
    ]
    if result["corr_matrix"].empty:
        lines.append("스코어 컬럼 부족으로 상관계수 행렬을 계산할 수 없습니다.")
        return "\n".join(lines) + "\n"

    lines.append("\n[상관계수 행렬]")
    lines.append(result["corr_matrix"].to_string())
    lines.append(f"\n고상관 쌍 수: {result['n_high']}")

    if result["recommendations"]:
        lines.append("\n[권고] 상관계수 0.7 이상 쌍 — 하나 제거 또는 가중치 축소 권장")
        for rec in result["recommendations"]:
            lines.append(f"  - {rec['label1']}–{rec['label2']}: r={rec['correlation']:.2f}")
            lines.append(f"    → {rec['suggestion']}")
    else:
        lines.append("\n고상관 쌍 없음. 현재 지표 조합은 독립성 기준을 만족합니다.")

    return "\n".join(lines) + "\n"

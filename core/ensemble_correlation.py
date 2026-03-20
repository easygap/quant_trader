"""
앙상블 구성 전략 신호 간 독립성 검증.
- technical / momentum_factor / volatility_condition / fundamental_factor(선택) 의 최종 신호(BUY=1, SELL=-1, HOLD=0) 시리즈에 대해
  상관계수를 계산하고, |r| >= threshold 인 쌍이 있으면 다수결 의미 퇴색 → conservative 모드 또는 전략 재구성 권고.
- technical–fundamental_factor 상관이 |r|<=0.4 이면 "독립성 확인됨" 로그·리포트에 반영.
- BUY/SELL 동시 발생률: Pearson 상관계수는 직관적이지 않으므로 "두 전략이 같은 날 BUY한 비율" 등을 추가 제공.
"""

from __future__ import annotations

from typing import List, Tuple

import pandas as pd
from loguru import logger

from config.config_loader import Config

SIGNAL_TO_NUM = {"BUY": 1, "HOLD": 0, "SELL": -1}
ENSEMBLE_SIGNAL_COLS = [
    "signal_technical",
    "signal_momentum_factor",
    "signal_volatility_condition",
    "signal_fundamental_factor",
]
ENSEMBLE_LABELS = {
    "signal_technical": "technical(스코어링)",
    "signal_momentum_factor": "momentum_factor(N일 수익률)",
    "signal_volatility_condition": "volatility_condition(실현변동성)",
    "signal_fundamental_factor": "fundamental_factor(펀더멘털)",
}

STRATEGY_ALTERNATIVES = {
    ("signal_technical", "signal_momentum_factor"): (
        "technical과 momentum_factor는 모두 '최근 가격 상승' 정보에 의존합니다. "
        "대안: momentum_factor를 제거하고 mean_reversion(평균 회귀) 또는 "
        "fundamental_factor(PER·PBR·ROE 등)로 교체하면 정보 소스가 분리됩니다."
    ),
    ("signal_technical", "signal_volatility_condition"): (
        "technical의 볼린저밴드/ATR과 volatility_condition은 모두 변동성 정보를 공유합니다. "
        "대안: volatility_condition을 제거하고 volume_factor(거래량 이상 급증) 또는 "
        "sentiment_factor(외국인·기관 순매수) 등으로 교체를 검토하세요."
    ),
    ("signal_momentum_factor", "signal_volatility_condition"): (
        "momentum_factor와 volatility_condition의 고상관은 드물지만, 모멘텀 상승 구간이 "
        "저변동성 구간과 겹칠 수 있습니다. 두 전략의 look-back 기간을 분리하면 개선 가능합니다."
    ),
    ("signal_fundamental_factor", "signal_technical"): (
        "fundamental_factor는 재무 지표, technical은 가격·거래량 기반입니다. "
        "일반적으로 상관이 낮으면 정보 소스가 잘 분리된 것입니다."
    ),
    ("signal_fundamental_factor", "signal_momentum_factor"): (
        "펀더멘털은 느리게 변하고 모멘텀은 단기 가격입니다. 고상관이면 동일 국면(성장주 랠리 등)에 둘 다 민감할 수 있습니다."
    ),
    ("signal_fundamental_factor", "signal_volatility_condition"): (
        "펀더멘털과 변동성 조건의 고상관은 상대적으로 드뭅니다. 발생 시 기간·유니버스를 점검하세요."
    ),
}

# technical vs fundamental: 이 임계 이하이면 정보원이 가격 대비 분리된 것으로 간주
TECH_FUND_INDEPENDENCE_ABS_MAX = 0.4


def _compute_agreement_rates(numeric: pd.DataFrame, cols: list[str]) -> list[dict]:
    """전략 쌍별 BUY/SELL 동시 발생률을 계산한다."""
    rates = []
    for i, c1 in enumerate(cols):
        for j, c2 in enumerate(cols):
            if i >= j:
                continue
            total = len(numeric)
            if total == 0:
                continue
            s1, s2 = numeric[c1], numeric[c2]
            both_buy = int(((s1 == 1) & (s2 == 1)).sum())
            both_sell = int(((s1 == -1) & (s2 == -1)).sum())
            any_buy_1 = int((s1 == 1).sum())
            any_buy_2 = int((s2 == 1).sum())
            buy_rate_1 = both_buy / any_buy_1 if any_buy_1 > 0 else 0
            buy_rate_2 = both_buy / any_buy_2 if any_buy_2 > 0 else 0
            rates.append({
                "col1": c1,
                "col2": c2,
                "label1": ENSEMBLE_LABELS.get(c1, c1),
                "label2": ENSEMBLE_LABELS.get(c2, c2),
                "both_buy_days": both_buy,
                "both_sell_days": both_sell,
                "buy_agreement_pct": round(max(buy_rate_1, buy_rate_2) * 100, 1),
                "total_days": total,
            })
    return rates


def should_force_conservative(result: dict) -> bool:
    """고상관 쌍이 있으면 conservative 자동 전환이 필요한지 판단한다."""
    return result.get("n_high", 0) > 0


def run_ensemble_signal_correlation_check(
    df: pd.DataFrame,
    threshold: float = 0.6,
    config: Config = None,
) -> dict:
    """
    앙상블 세 전략의 일별 신호(BUY/SELL/HOLD)를 수치화한 뒤 상관계수 행렬·고상관 쌍·권고 반환.

    Returns:
        corr_matrix, high_correlation_pairs, recommendations, n_high, threshold,
        recommendation_summary, agreement_rates, should_force_conservative
    """
    config = config or Config.get()
    from core.strategy_ensemble import StrategyEnsemble

    # skip_independence_check=True: StrategyEnsemble.analyze() 내부 독립성 검사와의 재귀 방지
    empty_result = {
        "corr_matrix": pd.DataFrame(),
        "high_correlation_pairs": [],
        "recommendations": [],
        "n_high": 0,
        "threshold": threshold,
        "recommendation_summary": "",
        "agreement_rates": [],
        "should_force_conservative": False,
        "technical_fundamental_correlation": None,
        "technical_fundamental_independence_note": "",
    }

    ensemble = StrategyEnsemble(config, skip_independence_check=True)
    analyzed = ensemble.analyze(df.copy())
    if analyzed.empty or len(analyzed) < 30:
        empty_result["recommendation_summary"] = "데이터 부족(30일 미만)으로 앙상블 신호 상관계수를 계산할 수 없습니다."
        return empty_result

    cols = [c for c in ENSEMBLE_SIGNAL_COLS if c in analyzed.columns]
    if len(cols) < 2:
        empty_result["recommendation_summary"] = "앙상블 신호 컬럼이 2개 미만입니다."
        return empty_result

    numeric = pd.DataFrame(index=analyzed.index)
    for c in cols:
        numeric[c] = analyzed[c].map(lambda s: SIGNAL_TO_NUM.get(str(s).strip().upper(), 0))

    corr_matrix = numeric[cols].corr()
    agreement_rates = _compute_agreement_rates(numeric, cols)

    tech_fund_note = ""
    tech_fund_r = None
    c_tech, c_fun = "signal_technical", "signal_fundamental_factor"
    if c_tech in corr_matrix.columns and c_fun in corr_matrix.columns:
        tech_fund_r = corr_matrix.loc[c_tech, c_fun]
        if pd.notna(tech_fund_r) and abs(float(tech_fund_r)) <= TECH_FUND_INDEPENDENCE_ABS_MAX:
            tech_fund_note = (
                f"fundamental_factor–technical 신호 상관계수 {float(tech_fund_r):.2f} — "
                f"독립성 확인됨 (|r|≤{TECH_FUND_INDEPENDENCE_ABS_MAX})"
            )
            logger.info(tech_fund_note)

    high_pairs: List[Tuple[str, str, float]] = []
    seen = set()
    for i, c1 in enumerate(corr_matrix.columns):
        for j, c2 in enumerate(corr_matrix.columns):
            if i >= j:
                continue
            r = corr_matrix.loc[c1, c2]
            if pd.isna(r):
                continue
            key = (min(c1, c2), max(c1, c2))
            if key in seen:
                continue
            if abs(r) >= threshold:
                seen.add(key)
                high_pairs.append((c1, c2, round(float(r), 3)))

    recommendations = []
    for c1, c2, r in high_pairs:
        label1 = ENSEMBLE_LABELS.get(c1, c1)
        label2 = ENSEMBLE_LABELS.get(c2, c2)
        alt_key = (min(c1, c2), max(c1, c2))
        alternative = STRATEGY_ALTERNATIVES.get(alt_key, STRATEGY_ALTERNATIVES.get((c1, c2), ""))
        recommendations.append({
            "col1": c1,
            "col2": c2,
            "label1": label1,
            "label2": label2,
            "correlation": r,
            "suggestion": (
                f"{label1}–{label2} 신호 상관계수 {r:.2f}. "
                "두 전략이 같은 상황에서 동시에 BUY/SELL하는 경향이 있어 다수결 의미가 퇴색합니다."
            ),
            "alternative": alternative,
        })

    force_conservative = len(high_pairs) > 0
    if high_pairs:
        summary = (
            f"앙상블 신호 간 고상관 쌍 {len(high_pairs)}개 (|r|>={threshold}). "
            "majority_vote 모드에서는 다수결 의미가 퇴색합니다. "
            "→ conservative 모드 자동 전환 권장 또는 전략 구성 재검토."
        )
    else:
        summary = f"앙상블 신호 간 고상관 쌍 없음 (기준 {threshold}). 현재 구성은 독립성 기준을 만족합니다."

    return {
        "corr_matrix": corr_matrix,
        "high_correlation_pairs": high_pairs,
        "recommendations": recommendations,
        "n_high": len(high_pairs),
        "threshold": threshold,
        "recommendation_summary": summary,
        "agreement_rates": agreement_rates,
        "should_force_conservative": force_conservative,
        "technical_fundamental_correlation": None if tech_fund_r is None or pd.isna(tech_fund_r) else float(tech_fund_r),
        "technical_fundamental_independence_note": tech_fund_note,
    }


def quick_independence_check(config: Config, df: pd.DataFrame, threshold: float = 0.6) -> dict | None:
    """
    StrategyEnsemble 초기화 또는 validate 모드에서 호출하는 경량 독립성 검사.
    데이터가 충분하면 결과를 반환하고, 부족하면 None.
    """
    if df is None or df.empty or len(df) < 60:
        return None
    try:
        result = run_ensemble_signal_correlation_check(df, threshold=threshold, config=config)
        return result if not result["corr_matrix"].empty else None
    except Exception as e:
        logger.debug("앙상블 독립성 경량 검사 실패: {}", e)
        return None


def render_ensemble_correlation_report(result: dict) -> str:
    """앙상블 신호 독립성 검증 결과를 텍스트 리포트로 렌더링."""
    lines = [
        "=" * 70,
        "앙상블 구성 전략 신호 독립성 검증",
        f"기준: |신호 상관계수| >= {result['threshold']} 인 쌍을 고상관으로 판단",
        "  (BUY=1, HOLD=0, SELL=-1 로 수치화 후 일별 시리즈 상관계수)",
        "=" * 70,
        "",
        "[배경] technical(스코어링)은 RSI·MACD·MA 등을 쓰고, momentum_factor는 N일 수익률을 씁니다. "
        "N일 수익률이 좋은 구간은 이동평균 골든크로스도 발생했을 가능성이 높아, 두 전략이 동시에 BUY하는 경향이 있으면 다수결 의미가 퇴색합니다.",
        "",
    ]
    if result["corr_matrix"].empty:
        lines.append(result.get("recommendation_summary", "상관계수 행렬을 계산할 수 없습니다."))
        return "\n".join(lines) + "\n"

    lines.append("[상관계수 행렬]")
    lines.append(result["corr_matrix"].to_string())
    lines.append("")
    lines.append(f"고상관 쌍 수: {result['n_high']}")

    tf_note = result.get("technical_fundamental_independence_note") or ""
    if tf_note:
        lines.append("")
        lines.append(f"[technical ↔ fundamental] {tf_note}")

    # BUY/SELL 동시 발생률
    rates = result.get("agreement_rates", [])
    if rates:
        lines.append("")
        lines.append("[BUY/SELL 동시 발생률]  (전체 기간 기준)")
        for r in rates:
            lines.append(
                f"  {r['label1']} × {r['label2']}: "
                f"BUY 동시 발생 {r['both_buy_days']}일, SELL 동시 발생 {r['both_sell_days']}일 "
                f"(BUY 일치율 {r['buy_agreement_pct']:.1f}%, 전체 {r['total_days']}일)"
            )

    if result["recommendations"]:
        lines.append("")
        lines.append(f"[권고] |r| >= {result['threshold']} 쌍 — conservative 모드 또는 전략 재구성 권장")
        for rec in result["recommendations"]:
            lines.append(f"  - {rec['label1']}–{rec['label2']}: r={rec['correlation']:.2f}")
            lines.append(f"    → {rec['suggestion']}")
            if rec.get("alternative"):
                lines.append(f"    💡 대안: {rec['alternative']}")
    else:
        lines.append("")
        lines.append("고상관 쌍 없음. 현재 앙상블 구성은 신호 독립성 기준을 만족합니다.")

    if result.get("should_force_conservative"):
        lines.append("")
        lines.append(
            "[자동 전환] 고상관 감지 → ensemble.mode가 majority_vote/weighted_sum이면 "
            "conservative로 자동 전환됩니다 (ensemble.auto_downgrade: true, 기본값)."
        )

    lines.append("")
    lines.append("[요약] " + result["recommendation_summary"])
    return "\n".join(lines) + "\n"

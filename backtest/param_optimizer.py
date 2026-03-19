"""
전략 파라미터 자동 최적화 (Grid Search / Bayesian Optimization).

- 오버피팅 주의: train_ratio로 in-sample 구간에서만 최적화하고,
  best params에 대해 out-of-sample 구간 성과를 함께 보고해 과적합 여부를 확인할 수 있음.
- 스코어링 가중치(weights): --include-weights 사용 시 탐색. 대칭(매수 가중치 = -매도 가중치)으로
  탐색해 차원을 줄이고, OOS 샤프 ≥ 1.0 게이트를 반드시 통과해야 채택. 미통과 시 채택 불가 경고.
- 권장 파이프라인: check_correlation → optimize --include-weights → validate --walk-forward (설계서 §4.1 참고).
"""

from copy import deepcopy
from itertools import product
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from config.config_loader import Config
from backtest.backtester import Backtester


# 전략별 기본 검색 공간 (Grid Search용: 파라미터명 -> 값 목록)
DEFAULT_SEARCH_SPACES = {
    "scoring": {
        "buy_threshold": [2, 3, 4, 5],
        "sell_threshold": [-2, -3, -4, -5],
    },
    "mean_reversion": {
        "z_score_buy": [-2.5, -2.0, -1.5],
        "z_score_sell": [1.5, 2.0, 2.5],
        "lookback_period": [15, 20, 25, 40, 60],
    },
    "trend_following": {
        "adx_threshold": [20, 25, 30],
        "trend_ma_period": [120, 200],
        "atr_stop_multiplier": [1.5, 2.0, 2.5],
    },
}

# 스코어링 가중치 탐색 공간 (대칭 탐색: 매수 = +w, 매도 = -w)
# 각 지표의 "절대 가중치"를 탐색. 0이면 해당 지표 비활성화.
SCORING_WEIGHT_SEARCH_SPACE = {
    "w_rsi": [0, 1, 2, 3],
    "w_macd": [0, 1, 2, 3],
    "w_bollinger": [0, 1, 2],
    "w_volume": [0, 1, 2],
    "w_ma": [0, 1, 2],
}

OOS_SHARPE_GATE = 1.0


def _grid_candidates(search_space: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """검색 공간에서 모든 조합 생성."""
    keys = list(search_space.keys())
    values = list(search_space.values())
    candidates = []
    for combo in product(*values):
        candidates.append(dict(zip(keys, combo)))
    return candidates


def _expand_symmetric_weights(combo: dict) -> dict:
    """
    w_rsi=2 → rsi_oversold=2, rsi_overbought=-2 등 대칭 전개.
    volume_surge는 단방향(양수만).
    """
    return {
        "rsi_oversold": combo["w_rsi"],
        "rsi_overbought": -combo["w_rsi"],
        "macd_golden_cross": combo["w_macd"],
        "macd_dead_cross": -combo["w_macd"],
        "bollinger_lower": combo["w_bollinger"],
        "bollinger_upper": -combo["w_bollinger"],
        "volume_surge": combo["w_volume"],
        "ma_golden_cross": combo["w_ma"],
        "ma_dead_cross": -combo["w_ma"],
    }


def _weight_combo_to_override(weights: dict, threshold_pair: tuple) -> dict:
    """가중치+임계값 조합을 Backtester param_overrides['scoring'] 형식으로 변환."""
    buy_t, sell_t = threshold_pair
    return {
        "buy_threshold": buy_t,
        "sell_threshold": sell_t,
        "weights": weights,
    }


def _run_single(
    df: pd.DataFrame,
    strategy_name: str,
    param_overrides: dict,
    config: Config,
    strict_lookahead: bool,
    initial_capital: float,
) -> Optional[dict]:
    """한 번의 백테스트 실행. 실패 시 None."""
    try:
        backtester = Backtester(config=config)
        result = backtester.run(
            df,
            strategy_name=strategy_name,
            initial_capital=initial_capital,
            strict_lookahead=strict_lookahead,
            param_overrides={strategy_name: param_overrides},
        )
        return result.get("metrics") if result else None
    except Exception as e:
        logger.debug("백테스트 실패 (params={}): {}", param_overrides, e)
        return None


def grid_search(
    df: pd.DataFrame,
    strategy_name: str = "scoring",
    search_space: Dict[str, List[Any]] = None,
    metric: str = "sharpe_ratio",
    train_ratio: float = 0.7,
    strict_lookahead: bool = True,
    config: Config = None,
    initial_capital: float = None,
) -> Dict[str, Any]:
    """
    Grid Search로 전략 파라미터 최적화.

    Args:
        df: OHLCV 데이터 (일봉, 인덱스 날짜 순)
        strategy_name: 전략명
        search_space: {파라미터명: [값1, 값2, ...]}. None이면 DEFAULT_SEARCH_SPACES[strategy_name]
        metric: 최대화할 지표 (sharpe_ratio, total_return, calmar_ratio 등)
        train_ratio: 학습 구간 비율 (0~1). 이 구간에서만 최적화하고, 나머지 구간은 OOS 검증용
        strict_lookahead: Backtester strict_lookahead
        config: Config 인스턴스
        initial_capital: 초기 자본

    Returns:
        best_params, best_score, all_results, oos_metrics(오버피팅 확인용)
    """
    config = config or Config.get()
    risk = config.risk_params
    initial_capital = initial_capital or risk.get("position_sizing", {}).get("initial_capital", 10_000_000)
    search_space = search_space or DEFAULT_SEARCH_SPACES.get(strategy_name, {})
    if not search_space:
        return {
            "best_params": {},
            "best_score": None,
            "all_results": [],
            "oos_metrics": None,
            "message": "검색 공간이 비어 있거나 해당 전략에 정의되지 않았습니다.",
        }

    # train / OOS 분할
    n = len(df)
    if n < 252:
        logger.warning("데이터가 1년 미만이면 최적화 결과 신뢰도가 낮을 수 있습니다.")
    train_end = int(n * train_ratio)
    if train_end < 100:
        train_end = min(100, n)
    df_train = df.iloc[:train_end]
    df_oos = df.iloc[train_end:] if train_end < n else None

    # scoring 전략: 임계값 대칭만 탐색 (buy_threshold와 sell_threshold를 동일 인덱스로 짝지음)
    if strategy_name == "scoring" and "buy_threshold" in search_space and "sell_threshold" in search_space:
        buy_vals = search_space["buy_threshold"]
        sell_vals = search_space["sell_threshold"]
        if len(buy_vals) == len(sell_vals):
            candidates = [
                {"buy_threshold": b, "sell_threshold": s}
                for b, s in zip(buy_vals, sell_vals)
            ]
        else:
            candidates = _grid_candidates(search_space)
    else:
        candidates = _grid_candidates(search_space)
    logger.info(
        "Grid Search 시작: 전략={}, 조합 수={}, 학습 구간={}~{} ({}일)",
        strategy_name, len(candidates),
        df_train.index[0], df_train.index[-1], len(df_train),
    )

    results = []
    for i, params in enumerate(candidates):
        metrics = _run_single(
            df_train, strategy_name, params, config,
            strict_lookahead, initial_capital,
        )
        if metrics is not None and metric in metrics:
            score = metrics[metric]
            if score is None:
                score = float("-inf") if metric != "max_drawdown" else 0
            results.append({"params": params, "metrics": metrics, "score": score})
        if (i + 1) % 20 == 0:
            logger.info("진행: {}/{}", i + 1, len(candidates))

    if not results:
        return {
            "best_params": {},
            "best_score": None,
            "all_results": [],
            "oos_metrics": None,
            "message": "유효한 백테스트 결과가 없습니다.",
        }

    # max: sharpe, total_return, calmar / min: max_drawdown(보통 음수이므로 절대값 기준으로 최소화)
    higher_is_better = metric != "max_drawdown"
    best = max(results, key=lambda x: x["score"]) if higher_is_better else min(results, key=lambda x: x["score"])
    best_params = best["params"]
    best_score = best["score"]

    oos_metrics = None
    if df_oos is not None and len(df_oos) >= 30:
        oos_metrics = _run_single(
            df_oos, strategy_name, best_params, config,
            strict_lookahead, initial_capital,
        )
        logger.info(
            "OOS 구간 성과 (오버피팅 확인용): {} ~ {} | {}={}",
            df_oos.index[0], df_oos.index[-1], metric, oos_metrics.get(metric) if oos_metrics else "N/A",
        )
        if oos_metrics and best_score is not None and oos_metrics.get(metric) is not None:
            oos_val = oos_metrics[metric]
            if higher_is_better and oos_val < best_score * 0.5:
                logger.warning(
                    "⚠️ OOS {} ({:.2f})가 학습 구간({:.2f}) 대비 크게 낮습니다. 오버피팅 가능성을 점검하세요.",
                    metric, oos_val, best_score,
                )
            elif not higher_is_better and oos_val > best_score * 1.5:
                logger.warning("⚠️ OOS max_drawdown이 학습 구간 대비 더 나쁩니다. 오버피팅 가능성 점검하세요.")

    return {
        "best_params": best_params,
        "best_score": best_score,
        "all_results": sorted(results, key=lambda x: x["score"], reverse=higher_is_better)[:20],
        "oos_metrics": oos_metrics,
        "train_period": f"{df_train.index[0]} ~ {df_train.index[-1]}",
        "oos_period": f"{df_oos.index[0]} ~ {df_oos.index[-1]}" if df_oos is not None and len(df_oos) > 0 else None,
        "metric": metric,
    }


def grid_search_scoring_weights(
    df: pd.DataFrame,
    weight_search_space: Dict[str, List[Any]] = None,
    threshold_pairs: List[Tuple[int, int]] = None,
    metric: str = "sharpe_ratio",
    train_ratio: float = 0.7,
    strict_lookahead: bool = True,
    config: Config = None,
    initial_capital: float = None,
    oos_sharpe_gate: float = OOS_SHARPE_GATE,
    disabled_weights: List[str] = None,
) -> Dict[str, Any]:
    """
    스코어링 가중치 + 임계값 동시 Grid Search. 대칭 탐색으로 차원 축소.

    권장 파이프라인:
    1. --mode check_correlation → 고상관 지표 확인
    2. disabled_weights에 제거 대상 전달 (또는 strategies.yaml에서 0으로 설정)
    3. 이 함수로 최적화 → OOS 샤프 ≥ oos_sharpe_gate 게이트 통과 확인
    4. --mode validate --walk-forward 로 최종 안정성 검증

    Args:
        disabled_weights: ['w_rsi', 'w_ma'] 등 탐색에서 0으로 고정할 가중치 키 목록 (상관 분석 후 제거 대상).
        oos_sharpe_gate: OOS 구간 샤프 최소치. 미달 시 채택 불가 경고.

    Returns:
        best_params, best_weights, oos_passed, oos_metrics 등.
    """
    config = config or Config.get()
    risk = config.risk_params
    initial_capital = initial_capital or risk.get("position_sizing", {}).get("initial_capital", 10_000_000)
    weight_search_space = weight_search_space or dict(SCORING_WEIGHT_SEARCH_SPACE)
    disabled = set(disabled_weights or [])
    for dk in disabled:
        if dk in weight_search_space:
            weight_search_space[dk] = [0]

    if threshold_pairs is None:
        threshold_pairs = [(2, -2), (3, -3), (4, -4), (5, -5)]

    weight_combos = _grid_candidates(weight_search_space)
    # 모든 가중치가 0인 조합은 무의미 → 제외
    weight_combos = [c for c in weight_combos if sum(c.values()) > 0]
    total = len(weight_combos) * len(threshold_pairs)
    logger.info(
        "스코어링 가중치 Grid Search: 가중치 조합 {}개 × 임계값 {}세트 = 총 {}회",
        len(weight_combos), len(threshold_pairs), total,
    )

    n = len(df)
    if n < 252:
        logger.warning("데이터 1년 미만. 최적화 결과 신뢰도 낮음.")
    train_end = max(100, int(n * train_ratio))
    train_end = min(train_end, n)
    df_train = df.iloc[:train_end]
    df_oos = df.iloc[train_end:] if train_end < n else None

    results = []
    idx = 0
    for wc in weight_combos:
        weights = _expand_symmetric_weights(wc)
        for bt, st in threshold_pairs:
            override = _weight_combo_to_override(weights, (bt, st))
            metrics = _run_single(
                df_train, "scoring", override, config, strict_lookahead, initial_capital,
            )
            idx += 1
            if metrics and metric in metrics:
                score = metrics[metric]
                if score is None:
                    score = float("-inf")
                results.append({
                    "weight_combo": dict(wc),
                    "weights": dict(weights),
                    "threshold": (bt, st),
                    "params": override,
                    "metrics": metrics,
                    "score": score,
                })
            if idx % 200 == 0:
                logger.info("가중치 최적화 진행: {}/{}", idx, total)

    if not results:
        return {
            "best_params": {},
            "best_weights": {},
            "best_score": None,
            "oos_metrics": None,
            "oos_passed": False,
            "all_results": [],
            "message": "유효한 백테스트 결과가 없습니다.",
        }

    results.sort(key=lambda x: x["score"], reverse=True)
    best = results[0]

    oos_metrics = None
    oos_passed = False
    if df_oos is not None and len(df_oos) >= 30:
        oos_metrics = _run_single(
            df_oos, "scoring", best["params"], config, strict_lookahead, initial_capital,
        )
        if oos_metrics:
            oos_sharpe = oos_metrics.get("sharpe_ratio", 0) or 0
            oos_passed = oos_sharpe >= oos_sharpe_gate
            if oos_passed:
                logger.info(
                    "OOS 샤프 {:.2f} ≥ {:.1f} → 게이트 통과. 가중치 채택 가능.",
                    oos_sharpe, oos_sharpe_gate,
                )
            else:
                logger.warning(
                    "OOS 샤프 {:.2f} < {:.1f} → 게이트 미달. 이 가중치를 채택하지 마세요. "
                    "과적합 가능성이 높습니다.",
                    oos_sharpe, oos_sharpe_gate,
                )
    else:
        logger.warning("OOS 구간 부족. 가중치 채택 여부를 판단할 수 없습니다.")

    top20 = results[:20]
    yaml_snippet = _render_weights_yaml_snippet(best["weights"], best["threshold"])

    return {
        "best_params": best["params"],
        "best_weights": best["weights"],
        "best_weight_combo": best["weight_combo"],
        "best_threshold": best["threshold"],
        "best_score": best["score"],
        "train_metrics": best["metrics"],
        "oos_metrics": oos_metrics,
        "oos_passed": oos_passed,
        "oos_sharpe_gate": oos_sharpe_gate,
        "all_results": [
            {"weight_combo": r["weight_combo"], "threshold": r["threshold"], "score": r["score"]}
            for r in top20
        ],
        "train_period": f"{df_train.index[0]} ~ {df_train.index[-1]}",
        "oos_period": f"{df_oos.index[0]} ~ {df_oos.index[-1]}" if df_oos is not None and len(df_oos) > 0 else None,
        "metric": metric,
        "yaml_snippet": yaml_snippet,
        "disabled_weights": list(disabled),
        "total_evaluated": total,
    }


def _render_weights_yaml_snippet(weights: dict, threshold: tuple) -> str:
    """최적화된 가중치를 strategies.yaml에 붙여넣기 할 수 있는 YAML 스니펫으로 렌더링."""
    bt, st = threshold
    lines = [
        "# --- 최적화된 스코어링 파라미터 (OOS 검증 통과 시에만 사용) ---",
        "scoring:",
        f"  buy_threshold: {bt}",
        f"  sell_threshold: {st}",
        "  weights:",
    ]
    for k, v in sorted(weights.items()):
        lines.append(f"    {k}: {v}")
    return "\n".join(lines)


def bayesian_optimize(
    df: pd.DataFrame,
    strategy_name: str = "scoring",
    param_bounds: Dict[str, Tuple[float, float]] = None,
    n_calls: int = 30,
    metric: str = "sharpe_ratio",
    train_ratio: float = 0.7,
    strict_lookahead: bool = True,
    config: Config = None,
    initial_capital: float = None,
) -> Dict[str, Any]:
    """
    Bayesian Optimization으로 전략 파라미터 최적화 (선택 의존성: scikit-optimize).

    Args:
        df: OHLCV 데이터
        strategy_name: 전략명
        param_bounds: {파라미터명: (하한, 상한)}. 정수 파라미터는 반올림 적용
        n_calls: 목적 함수 호출 횟수
        metric: 최대화할 지표
        train_ratio, strict_lookahead, config, initial_capital: grid_search와 동일

    Returns:
        best_params, best_score, oos_metrics 등 (형식은 grid_search와 유사)
    """
    try:
        from skopt import gp_minimize
        from skopt.space import Integer, Real
    except ImportError:
        logger.warning(
            "Bayesian 최적화를 위해 pip install scikit-optimize 가 필요합니다. Grid Search만 사용합니다."
        )
        # fallback: grid from bounds with coarse grid
        if param_bounds:
            search_space = {}
            for k, (lo, hi) in param_bounds.items():
                step = max(1, (hi - lo) / 4)
                search_space[k] = list(range(int(lo), int(hi) + 1, int(step)))
            return grid_search(
                df, strategy_name, search_space, metric, train_ratio,
                strict_lookahead, config, initial_capital,
            )
        return grid_search(df, strategy_name, None, metric, train_ratio, strict_lookahead, config, initial_capital)

    config = config or Config.get()
    risk = config.risk_params
    initial_capital = initial_capital or risk.get("position_sizing", {}).get("initial_capital", 10_000_000)

    # scoring: 대칭 권장. 아래는 구간만 정의하며, 실전에서는 sell_threshold = -buy_threshold 로 맞추는 것을 권장.
    default_bounds = {
        "scoring": {"buy_threshold": (2, 6), "sell_threshold": (-6, -2)},
        "mean_reversion": {"z_score_buy": (-3.0, -1.0), "z_score_sell": (1.0, 3.0), "lookback_period": (10, 60)},
        "trend_following": {"adx_threshold": (15, 35), "trend_ma_period": (100, 250), "atr_stop_multiplier": (1.0, 3.0)},
    }
    bounds = param_bounds or default_bounds.get(strategy_name, {})
    if not bounds:
        return grid_search(df, strategy_name, None, metric, train_ratio, strict_lookahead, config, initial_capital)

    n = len(df)
    train_end = int(n * train_ratio)
    if train_end < 100:
        train_end = min(100, n)
    df_train = df.iloc[:train_end]
    df_oos = df.iloc[train_end:] if train_end < n else None

    dims = []
    param_names = []
    for name, (lo, hi) in bounds.items():
        param_names.append(name)
        if isinstance(lo, int) and isinstance(hi, int):
            dims.append(Integer(lo, hi, name=name))
        else:
            dims.append(Real(lo, hi, name=name))

    def objective(x):
        params = {}
        for i, k in enumerate(param_names):
            v = x[i]
            params[k] = int(round(v)) if isinstance(dims[i], Integer) else v
        metrics = _run_single(
            df_train, strategy_name, params, config,
            strict_lookahead, initial_capital,
        )
        if metrics is None or metric not in metrics:
            return 1e9 if metric == "max_drawdown" else -1e9
        score = metrics[metric]
        if score is None:
            return 1e9 if metric == "max_drawdown" else -1e9
        return -score if metric != "max_drawdown" else score

    res = gp_minimize(objective, dims, n_calls=n_calls, random_state=42, verbose=False)
    best_x = res.x
    best_params = dict(zip(param_names, [int(round(v)) if isinstance(dims[i], Integer) else v for i, v in enumerate(best_x)]))
    best_score = -res.fun if metric != "max_drawdown" else res.fun

    oos_metrics = None
    if df_oos is not None and len(df_oos) >= 30:
        oos_metrics = _run_single(
            df_oos, strategy_name, best_params, config,
            strict_lookahead, initial_capital,
        )

    return {
        "best_params": best_params,
        "best_score": best_score,
        "all_results": [],  # Bayesian은 후보 목록 비유지
        "oos_metrics": oos_metrics,
        "train_period": f"{df_train.index[0]} ~ {df_train.index[-1]}",
        "oos_period": f"{df_oos.index[0]} ~ {df_oos.index[-1]}" if df_oos is not None and len(df_oos) > 0 else None,
        "metric": metric,
        "n_calls": n_calls,
    }

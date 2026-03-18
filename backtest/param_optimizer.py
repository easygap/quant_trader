"""
전략 파라미터 자동 최적화 (Grid Search / Bayesian Optimization).

- 오버피팅 주의: train_ratio로 in-sample 구간에서만 최적화하고,
  best params에 대해 out-of-sample 구간 성과를 함께 보고해 과적합 여부를 확인할 수 있음.
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
        "buy_threshold": [3, 4, 5, 6, 7],
        "sell_threshold": [-6, -5, -4, -3],
    },
    "mean_reversion": {
        "z_score_buy": [-2.5, -2.0, -1.5],
        "z_score_sell": [1.5, 2.0, 2.5],
        "lookback_period": [15, 20, 25],
    },
    "trend_following": {
        "adx_threshold": [20, 25, 30],
        "trend_ma_period": [120, 200],
        "atr_stop_multiplier": [1.5, 2.0, 2.5],
    },
}


def _grid_candidates(search_space: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """검색 공간에서 모든 조합 생성."""
    keys = list(search_space.keys())
    values = list(search_space.values())
    candidates = []
    for combo in product(*values):
        candidates.append(dict(zip(keys, combo)))
    return candidates


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

    default_bounds = {
        "scoring": {"buy_threshold": (3, 8), "sell_threshold": (-8, -2)},
        "mean_reversion": {"z_score_buy": (-3.0, -1.0), "z_score_sell": (1.0, 3.0), "lookback_period": (10, 40)},
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

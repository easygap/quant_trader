"""
momentum_top 워치리스트와 동일한 정의(12개월 수익률 상위 N, 시총 후보 풀)로
거래일마다 동일비중 리밸런싱하는 멀티종목 백테스트.

scoring 등 다른 전략 신호는 사용하지 않는다(리스트 멤버십만).
"""

from __future__ import annotations

import time
from typing import Any, Callable

import pandas as pd
from loguru import logger

from config.config_loader import Config
from core.data_collector import (
    DataCollectionError,
    DataCollector,
    _fdr_stock_listing_table,
    get_kospi_tickers_fdr,
)
from core.risk_manager import RiskManager
from core.watchlist_manager import WatchlistManager


def _norm_idx(series: pd.Series) -> pd.Series:
    s = series.astype(float).dropna()
    if s.empty:
        return s
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s = s.copy()
    s.index = idx.normalize()
    return s.sort_index()


def _close_on_or_before(series: pd.Series, day: pd.Timestamp) -> float | None:
    if series is None or series.empty:
        return None
    d = pd.Timestamp(day).normalize()
    sub = series.loc[:d].dropna()
    if sub.empty:
        return None
    v = float(sub.iloc[-1])
    return v if v > 0 else None


def _index_20d_return_pct(bench_close: pd.Series, d_ts: pd.Timestamp) -> float | None:
    """리밸런스일 d_ts 종가 기준 직전 20 '거래일' 수익률(%). 데이터 부족 시 None."""
    sub = bench_close.loc[: pd.Timestamp(d_ts).normalize()].dropna()
    if len(sub) < 21:
        return None
    c_old = float(sub.iloc[-21])
    c_now = float(sub.iloc[-1])
    if c_old <= 0:
        return None
    return (c_now / c_old - 1.0) * 100.0


def _equal_weight_buy_all(
    cash: float,
    targets: list[str],
    prices: dict[str, float],
    rm: RiskManager,
) -> tuple[dict[str, float], float, float]:
    """남은 현금으로 targets에 동일 예산 배분 매수. (종목별 float 수량, 총 현금 사용액, 잔여 현금)."""
    n = len(targets)
    if n == 0 or cash <= 0:
        return {}, 0.0, cash

    alpha = 1.0
    details: list[tuple[str, float, dict]] = []
    for _ in range(16):
        details.clear()
        tot_out = 0.0
        ok = True
        for s in targets:
            p = prices[s]
            if p <= 0:
                ok = False
                break
            budget = cash * alpha / n
            q = budget / p
            if q <= 0:
                ok = False
                break
            bc = rm.calculate_transaction_costs(p, q, "BUY")
            outlay = q * bc["execution_price"] + bc["commission"]
            details.append((s, q, bc))
            tot_out += outlay
        if not ok:
            alpha *= 0.5
            continue
        if tot_out <= cash:
            break
        alpha *= max(cash / tot_out * 0.998, 1e-6)

    spent = 0.0
    positions: dict[str, float] = {}
    for s, q, bc in details:
        outlay = q * bc["execution_price"] + bc["commission"]
        if spent + outlay > cash:
            break
        positions[s] = q
        spent += outlay
    remaining = max(0.0, cash - spent)
    return positions, spent, remaining


def _liquidate_positions_eod(
    positions: dict[str, float],
    cash: float,
    d_ts: pd.Timestamp,
    series_for,
    rm: RiskManager,
) -> tuple[float, dict[str, float]]:
    """종가 기준 전량 매도 후 (현금, 빈 포지션)."""
    for sym in list(positions.keys()):
        q = positions.pop(sym)
        if q <= 0:
            continue
        p = _close_on_or_before(series_for(sym), d_ts)
        if p is None:
            continue
        c = rm.calculate_transaction_costs(p, q, "SELL", avg_price=None)
        cash += q * c["execution_price"] - c["commission"] - (
            c["tax"] + float(c.get("capital_gains_tax") or 0)
        )
    return cash, {}


def _buy_targets(
    cash: float,
    targets_ok: list[str],
    d_ts: pd.Timestamp,
    series_for: Callable[[str], pd.Series],
    rm: RiskManager,
    cb: float,
) -> tuple[dict[str, float], float]:
    """targets_ok 종목을 현금 버퍼 적용해 동일비중 매수. (positions, remaining_cash)."""
    prices: dict[str, float] = {}
    for sym in targets_ok:
        px = _close_on_or_before(series_for(sym), d_ts)
        if px is not None:
            prices[sym] = px
    buyable = [s for s in targets_ok if s in prices]
    if not buyable:
        return {}, cash
    invest_cap = cash * (1.0 - cb)
    reserved = cash * cb
    new_pos, spent, rem = _equal_weight_buy_all(invest_cap, buyable, prices, rm)
    return new_pos, reserved + rem


def _simulate_momentum_top_equity(
    *,
    trading_days: list,
    rebalance_days: set,
    n_rebalance: int,
    bench_close: pd.Series,
    initial_capital: float,
    cb: float,
    ps: float,
    stop_cooldown: int = 0,
    use_market_filter: bool,
    top_n_override: int | None,
    collector: DataCollector,
    wm: WatchlistManager,
    rm: RiskManager,
    series_for: Callable[[str], pd.Series],
    log_progress: bool,
) -> list[float]:
    """리밸런스·일별 손절·쿨다운 재진입까지 반영한 일별 NAV 시퀀스.

    stop_cooldown: 0이면 손절 후 다음 리밸런스일까지 현금(기존).
                   양수이면 손절 후 N거래일 쿨다운 뒤 직전 타겟으로 재진입.
    """
    t_backtest0 = time.perf_counter()
    next_progress_pct = 10
    rebalance_done = 0
    cash = float(initial_capital)
    positions: dict[str, float] = {}
    equity_list: list[float] = []
    peak_nav = float(initial_capital)

    last_targets_ok: list[str] = []
    in_stop_cash = False
    stop_fired_idx = -999999

    for day_idx, d in enumerate(trading_days):
        d_ts = pd.Timestamp(d).normalize()

        # --- 쿨다운 재진입 (리밸런스일이 아니고, 손절 후 cooldown 경과) ---
        if (
            stop_cooldown > 0
            and in_stop_cash
            and day_idx >= stop_fired_idx + stop_cooldown
            and d_ts not in rebalance_days
            and last_targets_ok
        ):
            positions, cash = _buy_targets(
                cash, last_targets_ok, d_ts, series_for, rm, cb,
            )
            in_stop_cash = False
            peak_nav = cash + sum(
                q * (_close_on_or_before(series_for(s), d_ts) or 0)
                for s, q in positions.items()
            )
            logger.debug(
                "쿨다운 재진입: {}일 경과 → {} 종목 매수 ({})",
                stop_cooldown, len(positions), d_ts.strftime("%Y-%m-%d"),
            )

        # --- 리밸런스 ---
        if d_ts in rebalance_days:
            in_stop_cash = False
            day_str = d_ts.strftime("%Y-%m-%d")
            r20 = (
                _index_20d_return_pct(bench_close, d_ts)
                if use_market_filter
                else None
            )

            if use_market_filter and r20 is not None and r20 <= -10.0:
                cash, positions = _liquidate_positions_eod(positions, cash, d_ts, series_for, rm)
                logger.debug(
                    "국면필터: KS11 20일 {:.2f}% ≤ -10% → 전량 현금 ({})",
                    r20, day_str,
                )
            elif use_market_filter and r20 is not None and r20 <= -5.0:
                logger.debug(
                    "국면필터: KS11 20일 {:.2f}% ≤ -5% → 보유 유지·리밸런스·신규 진입 스킵 ({})",
                    r20, day_str,
                )
            else:
                target_list = wm.build_momentum_top_as_of(
                    day_str,
                    data_collector=collector,
                    top_n_override=top_n_override,
                )

                cash, positions = _liquidate_positions_eod(positions, cash, d_ts, series_for, rm)

                prices: dict[str, float] = {}
                for sym in target_list:
                    px = _close_on_or_before(series_for(sym), d_ts)
                    if px is not None:
                        prices[sym] = px

                targets_ok = [s for s in target_list if s in prices]
                last_targets_ok = targets_ok
                if targets_ok:
                    invest_cap = cash * (1.0 - cb)
                    reserved = cash * cb
                    new_pos, spent, rem = _equal_weight_buy_all(
                        invest_cap, targets_ok, prices, rm,
                    )
                    positions = new_pos
                    cash = reserved + rem
                else:
                    positions = {}

            rebalance_done += 1
            if log_progress:
                while next_progress_pct <= 100 and n_rebalance > 0:
                    if rebalance_done / n_rebalance >= next_progress_pct / 100.0 - 1e-15:
                        logger.info(
                            "[{}%] {} 리밸런스 완료 (경과 {:.1f}초, {}/{})",
                            next_progress_pct,
                            day_str,
                            time.perf_counter() - t_backtest0,
                            rebalance_done,
                            n_rebalance,
                        )
                        next_progress_pct += 10
                    else:
                        break

        # --- 일별 평가 + 포트폴리오 손절 ---
        mv = 0.0
        for sym, q in positions.items():
            p = _close_on_or_before(series_for(sym), d_ts)
            if p is not None:
                mv += q * p
        nav = cash + mv
        if ps > 0 and nav > 0:
            peak_nav = max(peak_nav, nav)
            if positions and peak_nav > 0 and nav <= peak_nav * (1.0 - ps):
                cash, positions = _liquidate_positions_eod(positions, cash, d_ts, series_for, rm)
                mv = 0.0
                nav = cash
                peak_nav = nav
                in_stop_cash = True
                stop_fired_idx = day_idx
                logger.debug(
                    "포트폴리오 손절: NAV {:.0f} ≤ 고점 대비 -{:.0f}% 기준 ({})",
                    nav, ps * 100, d_ts.strftime("%Y-%m-%d"),
                )

        equity_list.append(cash + mv)

    return equity_list


def _bench_metrics_for_trading_days(
    bench_close: pd.Series,
    trading_days: list,
    initial_capital: float,
) -> tuple[dict, float]:
    from backtest.strategy_validator import _portfolio_metrics_from_equity

    eq_idx = pd.DatetimeIndex(trading_days)
    b0 = float(bench_close.iloc[0])
    bench_equity = (bench_close.astype(float) / b0) * initial_capital
    bench_aligned = bench_equity.reindex(eq_idx).ffill().bfill()
    bench_f = bench_aligned.astype(float)
    bench_metrics = _portfolio_metrics_from_equity(bench_f, initial_capital)
    years_bt = max(len(trading_days) / 252.0, 1e-9)
    bench_cagr = ((float(bench_f.iloc[-1]) / initial_capital) ** (1.0 / years_bt) - 1.0) * 100.0
    bench_metrics["cagr"] = round(bench_cagr, 2)
    return bench_metrics, years_bt


def _pack_momentum_top_result(
    *,
    start_date: str,
    end_date: str,
    rebalance_every: int,
    initial_capital: float,
    benchmark_symbol: str,
    bench_close: pd.Series,
    trading_days: list,
    equity_list: list[float],
    bench_metrics: dict,
    years_bt: float,
    top_n_eff: int,
    use_market_filter: bool,
    cb: float,
    ps: float,
    stop_cooldown: int = 0,
) -> dict:
    from backtest.strategy_validator import _portfolio_metrics_from_equity

    eq_series = pd.Series(equity_list, index=pd.DatetimeIndex(trading_days))
    eq_f = eq_series.astype(float)
    strat_metrics = _portfolio_metrics_from_equity(eq_f, initial_capital)
    strat_cagr = ((float(eq_f.iloc[-1]) / initial_capital) ** (1.0 / years_bt) - 1.0) * 100.0
    strat_metrics["cagr"] = round(strat_cagr, 2)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "rebalance_every": rebalance_every,
        "initial_capital": initial_capital,
        "strategy": strat_metrics,
        "benchmark_symbol": benchmark_symbol,
        "benchmark": bench_metrics,
        "equity": eq_series,
        "bench_close": bench_close,
        "watchlist_top_n": top_n_eff,
        "use_market_filter": use_market_filter,
        "cash_buffer": cb,
        "portfolio_stop": ps,
        "stop_cooldown": stop_cooldown,
    }


def _momentum_top_build_context(
    start_date: str,
    end_date: str,
    rebalance_every: int,
    initial_capital: float,
    benchmark_symbol: str,
    top_n_override: int | None,
    use_market_filter: bool,
    config: Config,
) -> dict[str, Any] | None:
    collector = DataCollector(config)
    rm = RiskManager(config)
    wm = WatchlistManager(config)

    if not get_kospi_tickers_fdr(1):
        logger.warning(
            "get_kospi_tickers_fdr(1) 결과 없음 — FDR KOSPI 미설치·조회 실패 시 유니버스·백테스트가 비어 있을 수 있습니다.",
        )

    bench_df = collector.fetch_stock(benchmark_symbol, start_date, end_date)
    if bench_df is None or bench_df.empty or "close" not in bench_df.columns:
        logger.error("벤치마크 {} 데이터 없음 ({} ~ {})", benchmark_symbol, start_date, end_date)
        return None

    bench_close = _norm_idx(bench_df["close"])
    trading_days = bench_close.index.sort_values().tolist()
    if len(trading_days) < 2:
        logger.error("거래일 부족")
        return None

    rebalance_idxs = list(range(0, len(trading_days), max(1, int(rebalance_every))))
    rebalance_days = {trading_days[i] for i in rebalance_idxs}
    n_rebalance = len(rebalance_idxs)

    settings = config.watchlist_settings
    top_n_cfg = max(1, int(settings.get("top_n", 20)))
    top_n_eff = max(1, int(top_n_override)) if top_n_override is not None else top_n_cfg
    pool = max(top_n_eff + 20, 60)
    u_mode = wm._get_universe_mode()
    cap_prefetch = max(pool * 4, 300)
    prefetch_syms: set[str] = set(get_kospi_tickers_fdr(cap_prefetch))
    if u_mode == "historical":
        kd = _fdr_stock_listing_table("KOSDAQ", cap_prefetch)
        if not kd.empty:
            prefetch_syms.update(str(c).strip().zfill(6) for c in kd["Code"].tolist())
    prefetch_syms.add(str(benchmark_symbol).strip())

    first_rb = trading_days[rebalance_idxs[0]]
    wide_start = (pd.Timestamp(first_rb) - pd.Timedelta(days=430)).strftime("%Y-%m-%d")
    wide_end = end_date

    collector.quiet_ohlcv_log = True
    t_pref0 = time.perf_counter()
    logger.info(
        "OHLCV 프리패치 시작: {}종목, 구간 {} ~ {} (공유 캐시·리밸런스 재사용)",
        len(prefetch_syms), wide_start, wide_end,
    )
    for sym in sorted(prefetch_syms):
        try:
            collector.fetch_korean_stock(sym, wide_start, wide_end)
        except DataCollectionError as exc:
            logger.debug("OHLCV 프리패치 생략 {}: {}", sym, exc)
    logger.info(
        "OHLCV 프리패치 완료: 경과 {:.1f}초",
        time.perf_counter() - t_pref0,
    )

    price_cache: dict[str, pd.Series] = {}

    def series_for(sym: str) -> pd.Series:
        if sym not in price_cache:
            df = collector.fetch_korean_stock(sym, start_date, end_date)
            if df is None or df.empty or "close" not in df.columns:
                price_cache[sym] = pd.Series(dtype=float)
            else:
                price_cache[sym] = _norm_idx(df["close"])
        return price_cache[sym]

    return {
        "collector": collector,
        "rm": rm,
        "wm": wm,
        "bench_close": bench_close,
        "trading_days": trading_days,
        "rebalance_days": rebalance_days,
        "n_rebalance": n_rebalance,
        "rebalance_every": rebalance_every,
        "top_n_eff": top_n_eff,
        "series_for": series_for,
        "initial_capital": initial_capital,
        "benchmark_symbol": benchmark_symbol,
        "start_date": start_date,
        "end_date": end_date,
        "use_market_filter": use_market_filter,
        "top_n_override": top_n_override,
    }


def run_momentum_top_portfolio_scenarios(
    start_date: str,
    end_date: str,
    rebalance_every: int,
    scenarios: list[tuple],
    initial_capital: float | None = None,
    benchmark_symbol: str = "KS11",
    top_n_override: int | None = None,
    use_market_filter: bool = False,
) -> list[dict]:
    """
    프리패치·유니버스 로딩을 1회만 수행한 뒤 조합별로 시뮬레이션.
    scenarios: [(cb, ps), ...] 또는 [(cb, ps, stop_cooldown), ...].
    """
    config = Config.get()
    if initial_capital is None:
        initial_capital = float((config.risk_params or {}).get("initial_capital", 10_000_000))

    ctx = _momentum_top_build_context(
        start_date=start_date,
        end_date=end_date,
        rebalance_every=rebalance_every,
        initial_capital=initial_capital,
        benchmark_symbol=benchmark_symbol,
        top_n_override=top_n_override,
        use_market_filter=use_market_filter,
        config=config,
    )
    if ctx is None:
        return []

    collector = ctx["collector"]
    multi = len(scenarios) > 1
    out: list[dict] = []
    try:
        bench_metrics, years_bt = _bench_metrics_for_trading_days(
            ctx["bench_close"], ctx["trading_days"], ctx["initial_capital"],
        )
        for sc in scenarios:
            cb_in, ps_in = float(sc[0]), float(sc[1])
            cd_in = int(sc[2]) if len(sc) > 2 else 0
            cb = max(0.0, min(cb_in, 0.999))
            ps = max(0.0, min(ps_in, 0.999))
            cd = max(0, cd_in)
            equity_list = _simulate_momentum_top_equity(
                trading_days=ctx["trading_days"],
                rebalance_days=ctx["rebalance_days"],
                n_rebalance=ctx["n_rebalance"],
                bench_close=ctx["bench_close"],
                initial_capital=ctx["initial_capital"],
                cb=cb,
                ps=ps,
                stop_cooldown=cd,
                use_market_filter=ctx["use_market_filter"],
                top_n_override=ctx["top_n_override"],
                collector=ctx["collector"],
                wm=ctx["wm"],
                rm=ctx["rm"],
                series_for=ctx["series_for"],
                log_progress=not multi,
            )
            out.append(
                _pack_momentum_top_result(
                    start_date=ctx["start_date"],
                    end_date=ctx["end_date"],
                    rebalance_every=ctx["rebalance_every"],
                    initial_capital=ctx["initial_capital"],
                    benchmark_symbol=ctx["benchmark_symbol"],
                    bench_close=ctx["bench_close"],
                    trading_days=ctx["trading_days"],
                    equity_list=equity_list,
                    bench_metrics=bench_metrics,
                    years_bt=years_bt,
                    top_n_eff=ctx["top_n_eff"],
                    use_market_filter=ctx["use_market_filter"],
                    cb=cb,
                    ps=ps,
                    stop_cooldown=cd,
                ),
            )
    finally:
        collector.quiet_ohlcv_log = False

    return out


def run_momentum_top_portfolio_backtest(
    start_date: str,
    end_date: str,
    rebalance_every: int = 20,
    initial_capital: float | None = None,
    benchmark_symbol: str = "KS11",
    top_n_override: int | None = None,
    use_market_filter: bool = False,
    cash_buffer: float = 0.0,
    portfolio_stop: float = 0.0,
    stop_cooldown: int = 0,
) -> dict | None:
    """
    Args:
        start_date, end_date: YYYY-MM-DD
        rebalance_every: 거래일 기준 리밸런싱 간격
        initial_capital: None이면 risk_params.initial_capital
        benchmark_symbol: 비교 지수(기본 KS11)
        top_n_override: None이면 config watchlist top_n, 지정 시 모멘텀 보유 종목 수만 덮어씀
        use_market_filter: True면 KS11 20거래일 수익률로 리밸런스 축소(≤-5% 신규매수 금지, ≤-10% 전량 현금)
        cash_buffer: 0~1, 주식에 넣지 않고 항상 현금으로 남길 비율(리밸런스 시 매수가능액 = 현금×(1-buffer))
        portfolio_stop: 0~1, 직전 고점 대비 종가 NAV 하락률이 이 값 이상이면 당일 전량 매도 후 다음 리밸런스까지 현금
        stop_cooldown: 0이면 손절 후 다음 리밸런스까지 대기(기존). 양수면 N거래일 뒤 재진입.

    Returns:
        strategy/benchmark 메트릭, 일별 equity 시리즈 등 dict 또는 실패 시 None
    """
    config = Config.get()
    if initial_capital is None:
        initial_capital = float((config.risk_params or {}).get("initial_capital", 10_000_000))

    cb = max(0.0, min(float(cash_buffer), 0.999))
    ps = max(0.0, min(float(portfolio_stop), 0.999))
    cd = max(0, int(stop_cooldown))

    ctx = _momentum_top_build_context(
        start_date=start_date,
        end_date=end_date,
        rebalance_every=rebalance_every,
        initial_capital=initial_capital,
        benchmark_symbol=benchmark_symbol,
        top_n_override=top_n_override,
        use_market_filter=use_market_filter,
        config=config,
    )
    if ctx is None:
        return None

    collector = ctx["collector"]
    try:
        bench_metrics, years_bt = _bench_metrics_for_trading_days(
            ctx["bench_close"], ctx["trading_days"], ctx["initial_capital"],
        )
        equity_list = _simulate_momentum_top_equity(
            trading_days=ctx["trading_days"],
            rebalance_days=ctx["rebalance_days"],
            n_rebalance=ctx["n_rebalance"],
            bench_close=ctx["bench_close"],
            initial_capital=ctx["initial_capital"],
            cb=cb,
            ps=ps,
            stop_cooldown=cd,
            use_market_filter=ctx["use_market_filter"],
            top_n_override=ctx["top_n_override"],
            collector=ctx["collector"],
            wm=ctx["wm"],
            rm=ctx["rm"],
            series_for=ctx["series_for"],
            log_progress=True,
        )
        return _pack_momentum_top_result(
            start_date=ctx["start_date"],
            end_date=ctx["end_date"],
            rebalance_every=ctx["rebalance_every"],
            initial_capital=ctx["initial_capital"],
            benchmark_symbol=ctx["benchmark_symbol"],
            bench_close=ctx["bench_close"],
            trading_days=ctx["trading_days"],
            equity_list=equity_list,
            bench_metrics=bench_metrics,
            years_bt=years_bt,
            top_n_eff=ctx["top_n_eff"],
            use_market_filter=ctx["use_market_filter"],
            cb=cb,
            ps=ps,
            stop_cooldown=cd,
        )
    finally:
        collector.quiet_ohlcv_log = False


def print_momentum_top_portfolio_report(result: dict, config: Config | None = None) -> None:
    cfg = config or Config.get()
    u_mode = str(((cfg.risk_params or {}).get("backtest_universe") or {}).get("mode", "current")).lower()
    s = result.get("strategy") or {}
    b = result.get("benchmark") or {}
    sym = result.get("benchmark_symbol", "KS11")
    _ps = float(result.get("portfolio_stop") or 0)
    _cd = int(result.get("stop_cooldown") or 0)
    _pstop_txt = "OFF" if _ps <= 0 else f"고점 대비 -{_ps * 100:.0f}%"
    if _ps > 0 and _cd > 0:
        _pstop_txt += f" (쿨다운 {_cd}일 후 재진입)"
    elif _ps > 0:
        _pstop_txt += " (다음 리밸런스까지 대기)"
    lines = [
        "",
        "=" * 60,
        " momentum_top 포트폴리오 백테스트 (리스트 기반 동일비중, scoring 미사용)",
        "=" * 60,
        f"  기간: {result.get('start_date')} ~ {result.get('end_date')}",
        f"  리밸런싱: 매 {result.get('rebalance_every')} 거래일",
        f"  보유 종목 수(top_n): {result.get('watchlist_top_n', '-')}",
        f"  KS11 국면 필터: {'ON (20일 ≤-5% 신규금지, ≤-10% 현금)' if result.get('use_market_filter') else 'OFF'}",
        f"  현금 버퍼: {float(result.get('cash_buffer') or 0)*100:.1f}% (비투자 비율)",
        f"  포트폴리오 손절: {_pstop_txt}",
        f"  초기자본: {result.get('initial_capital', 0):,.0f} 원",
    ]
    if u_mode == "historical":
        lines.append(
            "  [주의] pykrx 티커 목록이 비면 historical/kospi200는 FDR 시총순으로 대체 — 과거 시점과 불일치·편향 가능.",
        )
    lines.extend(
        [
        "",
        "  [전략]",
        f"    총수익률: {s.get('total_return', 0):.2f}%",
        f"    연환산(단순): {s.get('annual_return', 0):.2f}%  (total/연환산연수, backtester와 동일)",
        f"    연환산(CAGR): {s.get('cagr', 0):.2f}%",
        f"    MDD:          {s.get('max_drawdown', 0):.2f}%",
        "",
        f"  [{sym} 매수·보유 동기간]",
        f"    총수익률: {b.get('total_return', 0):.2f}%",
        f"    연환산(단순): {b.get('annual_return', 0):.2f}%",
        f"    연환산(CAGR): {b.get('cagr', 0):.2f}%",
        f"    MDD:          {b.get('max_drawdown', 0):.2f}%",
        "=" * 60,
        "",
        ]
    )
    text = "\n".join(lines)
    print(text)
    logger.info(text)

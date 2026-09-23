"""
시장 국면 필터 (단계적 대응)

200일선 단독은 급락 후 몇 주가 지나서야 이탈하므로 반응이 느리다.
단기 모멘텀(기본 20일 수익률)을 병행해 단계적으로 대응한다.
선택적으로 단기 MA 크로스(20/60일선 데드크로스)를 추가 빠른 감지 신호로 사용 가능.

국면 판별 (3가지 독립 신호):
  A. 200일선 이탈: 종가 < MA(200)
  B. 단기 모멘텀 하락: N일 수익률 ≤ threshold (기본 -5%)
  C. 단기 MA 데드크로스: MA(short) < MA(mid) (기본 20 < 60, 선택적)

  - bearish : A+B 또는 A+C 또는 B+C 중 2개 이상 → 신규 매수 전면 중단
  - caution : A, B, C 중 1개만 충족 → 포지션 사이징 축소 (기본 50%)
  - bullish : 모두 미충족 → 정상
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from loguru import logger

_KST = ZoneInfo("Asia/Seoul")


def _cfg_dict(config, name: str) -> dict:
    """Config 객체와 dict 설정을 모두 지원한다."""
    if hasattr(config, name):
        return getattr(config, name) or {}
    if isinstance(config, dict):
        return config.get(name, {}) or {}
    return {}


def resolve_market_regime_config(config, *, for_backtest: bool = False) -> dict:
    """
    운영과 백테스트의 시장 국면 설정을 같은 키 체계로 정규화한다.

    기본값은 settings.trading.market_regime_* 를 따른다. 백테스트에서는
    risk_params.backtest_regime_filter에 명시된 값만 실험용 override로 사용하고,
    enabled가 null/미지정이면 운영 설정을 그대로 mirror한다.
    """
    trading = _cfg_dict(config, "trading")
    risk_params = _cfg_dict(config, "risk_params")
    backtest_cfg = (risk_params.get("backtest_regime_filter") or {}) if for_backtest else {}

    def _override(key: str, runtime_key: str, default):
        if for_backtest and key in backtest_cfg and backtest_cfg.get(key) is not None:
            return backtest_cfg.get(key)
        return trading.get(runtime_key, default)

    enabled_override = backtest_cfg.get("enabled") if for_backtest else None
    if for_backtest and enabled_override is not None:
        enabled = bool(enabled_override)
    else:
        enabled = bool(trading.get("market_regime_filter", False))

    return {
        "enabled": enabled,
        "index_symbol": _override("index_symbol", "market_regime_index", "KS11"),
        "ma_days": max(20, int(_override("ma_days", "market_regime_ma_days", 200))),
        "short_momentum_days": max(
            1,
            int(_override("short_momentum_days", "market_regime_short_momentum_days", 20)),
        ),
        "short_momentum_threshold": float(
            _override("short_momentum_threshold", "market_regime_short_momentum_threshold", -5.0)
        ),
        "caution_scale": float(_override("caution_scale", "market_regime_caution_scale", 0.5)),
        "ma_cross_enabled": bool(
            _override("ma_cross_enabled", "market_regime_ma_cross_enabled", False)
        ),
        "ma_short": max(5, int(_override("ma_short", "market_regime_ma_short", 20))),
        "ma_mid": max(10, int(_override("ma_mid", "market_regime_ma_mid", 60))),
    }


def _completed_index_bars(df: pd.DataFrame, today) -> pd.DataFrame:
    """날짜 오름차순 일봉에서 오늘(KST) 이후 날짜의 봉을 뺀다.

    장중에 받은 오늘 봉은 확정 종가가 아니다. 백테스트 국면도 T-1까지의 종가로
    판단하므로(backtester._precompute_regime_series) 운영도 전일까지만 쓴다.
    """
    frame = df
    if "date" in frame.columns:
        frame = frame.set_index("date")
    dates = pd.to_datetime(frame.index)
    if dates.tz is not None:
        dates = dates.tz_convert(_KST).tz_localize(None)
    frame = frame.set_axis(dates.normalize(), axis=0).sort_index()
    return frame.loc[frame.index < pd.Timestamp(today)]


def _missing_sessions(last_bar_date, today) -> int | None:
    """마지막 봉 다음 날부터 어제까지 빠진 거래일 수 (0이면 최신). 달력 확인 불가면 None."""
    from core.trading_hours import _load_holidays

    try:
        holidays = sorted(_load_holidays())
        return int(np.busday_count(
            (pd.Timestamp(last_bar_date) + pd.Timedelta(days=1)).date(),
            pd.Timestamp(today).date(),
            holidays=holidays,
        ))
    except (TypeError, ValueError) as e:
        logger.warning("시장 국면 필터: 휴장일 달력 확인 실패 — 지수 신선도 미표시: {}", e)
        return None


def check_market_regime(config, collector=None) -> dict:
    """
    시장 국면을 판별하고 단계적 position_scale 을 반환한다.

    지수 일봉은 전일(KST 기준 오늘 이전)까지의 확정 봉만 쓴다. 조회 구간은
    필요한 거래일 수를 달력일로 환산해(약 1.5배 + 30일) 잡는다.

    Returns:
        {
            "regime": "bullish" | "caution" | "bearish" | "unknown",
            "position_scale": float,  # 1.0 / caution_scale(기본 0.5) / 0.0
            "allow_buys": bool,       # bearish 이면 False, 나머지 True (하위 호환)
            "details": {
                "below_ma": bool,
                "short_momentum_pct": float | None,
                "momentum_triggered": bool,
                "last_close": float,
                "ma_value": float,
                "last_bar_date": "YYYY-MM-DD",   # 판정에 쓴 마지막 확정 봉 날짜
                "missing_sessions": int | None,  # 그 뒤 빠진 거래일 수 (지수 자료 지연)
            },
        }
    """
    regime_cfg = resolve_market_regime_config(config)
    default_result = {
        "regime": "bullish",
        "position_scale": 1.0,
        "allow_buys": True,
        "details": {},
    }

    if not regime_cfg["enabled"]:
        return default_result

    index_symbol = regime_cfg["index_symbol"]

    def fail_closed(reason: str, *, error: str = "") -> dict:
        details = {
            "reason": reason,
            "index_symbol": index_symbol,
            "data_unavailable": True,
            "fail_closed": True,
        }
        if error:
            details["error"] = error
        return {
            "regime": "unknown",
            "position_scale": 0.0,
            "allow_buys": False,
            "details": details,
        }

    ma_days = regime_cfg["ma_days"]
    short_days = regime_cfg["short_momentum_days"]
    short_threshold = regime_cfg["short_momentum_threshold"]
    caution_scale = regime_cfg["caution_scale"]
    ma_cross_enabled = regime_cfg["ma_cross_enabled"]
    ma_short = regime_cfg["ma_short"]
    ma_mid = regime_cfg["ma_mid"]

    # 필요한 거래일 수(여유 50봉 포함). 조회는 달력일이라 환산이 필요하다 — 예전에는
    # 거래일 수를 그대로 달력일로 써서(250+30=280일 ≈ 190거래일 < 200) 필터를 켜면
    # 항상 'index_data_insufficient'로 모든 매수가 막혔다.
    need_days = max(ma_days, ma_mid, short_days + 1) + 50
    lookback_calendar_days = int(need_days * 1.5) + 30

    if collector is None:
        try:
            from core.data_collector import DataCollector
            collector = DataCollector()
        except Exception as e:
            logger.warning("시장 국면 필터: DataCollector 생성 실패 — 신규 매수 차단: {}", e)
            return fail_closed("data_collector_unavailable", error=str(e))

    now_kst = datetime.now(_KST)
    today = now_kst.date()
    start_d = (now_kst - timedelta(days=lookback_calendar_days)).strftime("%Y-%m-%d")
    end_str = now_kst.strftime("%Y-%m-%d")

    try:
        df = collector.fetch_korean_stock(index_symbol, start_date=start_d, end_date=end_str)
        if df is None or df.empty or "close" not in df.columns:
            logger.warning("시장 국면 필터: 지수 {} 데이터 없음 — 신규 매수 차단", index_symbol)
            return fail_closed("index_data_insufficient")
        df = _completed_index_bars(df, today)
        if len(df) < ma_days:
            logger.warning(
                "시장 국면 필터: 지수 {} 확정 봉 {}개 < MA {}일 — 신규 매수 차단",
                index_symbol, len(df), ma_days,
            )
            return fail_closed("index_data_insufficient")

        close = df["close"].astype(float)
        ma = close.rolling(ma_days, min_periods=ma_days).mean()
        last_close = float(close.iloc[-1])
        last_ma = float(ma.iloc[-1])

        if not (math.isfinite(last_ma) and math.isfinite(last_close)):
            logger.warning("시장 국면 필터: 지수 {} MA 계산 실패 — 신규 매수 차단", index_symbol)
            return fail_closed("ma_unavailable")

        last_bar_date = df.index[-1].date()
        missing_sessions = _missing_sessions(last_bar_date, today)
        if missing_sessions:
            logger.warning(
                "시장 국면 필터: 지수 {} 최근 봉 {} — 그 뒤 거래일 {}일 자료 없음 (지연된 종가로 판정)",
                index_symbol, last_bar_date, missing_sessions,
            )

        below_ma = last_close < last_ma

        # 신호 B: 단기 모멘텀 하락
        short_momentum_pct = None
        momentum_triggered = False
        if len(close) > short_days:
            prev_close = float(close.iloc[-(short_days + 1)])
            if prev_close > 0:
                short_momentum_pct = (last_close - prev_close) / prev_close * 100.0
                momentum_triggered = short_momentum_pct <= short_threshold

        # 신호 C: 단기 MA 데드크로스 (선택적)
        ma_cross_triggered = False
        ma_short_val = None
        ma_mid_val = None
        if ma_cross_enabled and len(close) >= ma_mid:
            ma_s = close.rolling(ma_short, min_periods=ma_short).mean()
            ma_m = close.rolling(ma_mid, min_periods=ma_mid).mean()
            ma_short_val = float(ma_s.iloc[-1]) if ma_s.iloc[-1] == ma_s.iloc[-1] else None
            ma_mid_val = float(ma_m.iloc[-1]) if ma_m.iloc[-1] == ma_m.iloc[-1] else None
            if ma_short_val is not None and ma_mid_val is not None:
                ma_cross_triggered = ma_short_val < ma_mid_val

        details = {
            "below_ma": below_ma,
            "short_momentum_pct": round(short_momentum_pct, 2) if short_momentum_pct is not None else None,
            "momentum_triggered": momentum_triggered,
            "ma_cross_enabled": ma_cross_enabled,
            "ma_cross_triggered": ma_cross_triggered,
            "ma_short_val": round(ma_short_val, 1) if ma_short_val is not None else None,
            "ma_mid_val": round(ma_mid_val, 1) if ma_mid_val is not None else None,
            "last_close": round(last_close, 1),
            "ma_value": round(last_ma, 1),
            "last_bar_date": str(last_bar_date),
            "missing_sessions": missing_sessions,
        }

        # 국면 판별: 3개 신호 중 2개 이상 → bearish, 1개 → caution
        signals = [below_ma, momentum_triggered, ma_cross_triggered]
        triggered_count = sum(signals)

        if triggered_count >= 2:
            reasons = []
            if below_ma:
                reasons.append(f"{ma_days}일선 이탈")
            if momentum_triggered:
                reasons.append(f"{short_days}일 수익률 {short_momentum_pct:.1f}%")
            if ma_cross_triggered:
                reasons.append(f"MA({ma_short})<MA({ma_mid}) 데드크로스")
            logger.info(
                "시장 국면 [bearish]: {} — {} — 신규 매수 전면 중단",
                index_symbol, " + ".join(reasons),
            )
            return {"regime": "bearish", "position_scale": 0.0, "allow_buys": False, "details": details}

        if triggered_count == 1:
            if below_ma:
                reason = f"{ma_days}일선 이탈"
            elif momentum_triggered:
                reason = f"{short_days}일 수익률 {short_momentum_pct:.1f}%"
            else:
                reason = f"MA({ma_short})<MA({ma_mid}) 데드크로스"
            logger.info(
                "시장 국면 [caution]: {} — {} → 포지션 사이징 {:.0f}% 축소",
                index_symbol, reason, caution_scale * 100,
            )
            return {"regime": "caution", "position_scale": caution_scale, "allow_buys": True, "details": details}

        return {"regime": "bullish", "position_scale": 1.0, "allow_buys": True, "details": details}

    except Exception as e:
        logger.warning("시장 국면 필터 조회 실패 — 신규 매수 차단: {}", e)
        return fail_closed("market_regime_query_failed", error=str(e))


def allow_new_buys_by_market_regime(config, collector=None) -> bool:
    """하위 호환: 기존 bool 반환 인터페이스."""
    return check_market_regime(config, collector)["allow_buys"]


def get_regime_adjusted_params(config, collector=None) -> dict:
    """
    시장 국면에 따라 전략 파라미터 조정값을 반환.

    국면별로 조정하는 것은 손절·익절 배수뿐이다. 매수 진입 임계값은 국면에 따라
    바뀌지 않는다 — 국면별 매수 억제는 allow_buys·position_scale이 맡는다.
    (예전 regime_adaptive.*.buy_threshold_offset 키는 계산만 되고 어디서도 읽히지
    않아 '진입이 국면별로 엄격해진다'는 오해를 만들었으므로 제거했다.)

    Returns:
        {
            "regime": "bullish" | "caution" | "bearish" | "unknown",
            "allow_buys": bool,
            "position_scale": float,
            "stop_loss_multiplier": float,
            "take_profit_multiplier": float,
        }
    """
    regime_result = check_market_regime(config, collector)
    regime = regime_result["regime"]
    base = {
        "regime": regime,
        "allow_buys": bool(regime_result.get("allow_buys", True)),
        "position_scale": float(regime_result.get("position_scale", 1.0)),
        "details": regime_result.get("details", {}),
    }

    strategies_cfg = config.strategies if hasattr(config, "strategies") else {}
    ra_cfg = strategies_cfg.get("regime_adaptive", {})

    if not ra_cfg.get("enabled", False):
        return {
            **base,
            "stop_loss_multiplier": 1.0,
            "take_profit_multiplier": 1.0,
        }

    regime_params = ra_cfg.get(regime, {})
    return {
        **base,
        "stop_loss_multiplier": float(regime_params.get("stop_loss_multiplier", 1.0)),
        "take_profit_multiplier": float(regime_params.get("take_profit_multiplier", 1.0)),
    }

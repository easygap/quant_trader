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

from datetime import datetime, timedelta

from loguru import logger


def check_market_regime(config, collector=None) -> dict:
    """
    시장 국면을 판별하고 단계적 position_scale 을 반환한다.

    Returns:
        {
            "regime": "bullish" | "caution" | "bearish",
            "position_scale": float,  # 1.0 / caution_scale(기본 0.5) / 0.0
            "allow_buys": bool,       # bearish 이면 False, 나머지 True (하위 호환)
            "details": {
                "below_ma": bool,
                "short_momentum_pct": float | None,
                "momentum_triggered": bool,
                "last_close": float,
                "ma_value": float,
            },
        }
    """
    trading = config.trading if hasattr(config, "trading") else config.get("trading", {})
    default_result = {
        "regime": "bullish",
        "position_scale": 1.0,
        "allow_buys": True,
        "details": {},
    }

    if not trading.get("market_regime_filter", False):
        return default_result

    index_symbol = trading.get("market_regime_index", "KS11")
    ma_days = max(20, int(trading.get("market_regime_ma_days", 200)))
    short_days = max(1, int(trading.get("market_regime_short_momentum_days", 20)))
    short_threshold = float(trading.get("market_regime_short_momentum_threshold", -5.0))
    caution_scale = float(trading.get("market_regime_caution_scale", 0.5))

    # 단기 MA 크로스 (선택적): 200일선보다 빠르게 하락 감지
    ma_cross_enabled = trading.get("market_regime_ma_cross_enabled", False)
    ma_short = max(5, int(trading.get("market_regime_ma_short", 20)))
    ma_mid = max(10, int(trading.get("market_regime_ma_mid", 60)))

    need_days = max(ma_days, ma_mid, short_days) + 50

    if collector is None:
        try:
            from core.data_collector import DataCollector
            collector = DataCollector()
        except Exception as e:
            logger.warning("시장 국면 필터: DataCollector 생성 실패 — 신규 매수 허용: {}", e)
            return default_result

    end_d = datetime.now()
    start_d = (end_d - timedelta(days=need_days + 30)).strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    try:
        df = collector.fetch_korean_stock(index_symbol, start_date=start_d, end_date=end_str)
        if df.empty or len(df) < ma_days:
            logger.warning("시장 국면 필터: 지수 {} 데이터 부족 — 신규 매수 허용", index_symbol)
            return default_result

        close = df["close"].astype(float)
        ma = close.rolling(ma_days, min_periods=ma_days).mean()
        last_close = float(close.iloc[-1])
        last_ma = ma.iloc[-1]

        if last_ma is None or (hasattr(last_ma, "item") and (last_ma != last_ma)):
            return default_result
        last_ma = float(last_ma)

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
        logger.warning("시장 국면 필터 조회 실패 — 신규 매수 허용: {}", e)
        return default_result


def allow_new_buys_by_market_regime(config, collector=None) -> bool:
    """하위 호환: 기존 bool 반환 인터페이스."""
    return check_market_regime(config, collector)["allow_buys"]

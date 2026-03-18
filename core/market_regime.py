"""
시장 국면 필터
- 지수(코스피 등)가 200일선 위에 있을 때만 신규 매수 허용
- 200일선 아래(하락장)면 신규 매수 전면 중단 (기존 포지션 매도·손절은 정상)
"""

from datetime import datetime, timedelta

from loguru import logger


def allow_new_buys_by_market_regime(config, collector=None) -> bool:
    """
    현재 시장 국면이 신규 매수를 허용하는지 여부.

    지수(기본 KS11) 종가가 200일 이동평균 이상이면 True, 미만이면 False.
    필터 비활성화 또는 데이터 조회 실패 시 True 반환(매수 차단하지 않음).

    Args:
        config: Config 인스턴스 (config.trading.market_regime_* 사용)
        collector: DataCollector 인스턴스. None이면 내부에서 생성.

    Returns:
        True = 신규 매수 허용, False = 하락장으로 신규 매수 중단
    """
    trading = config.trading if hasattr(config, "trading") else config.get("trading", {})
    if not trading.get("market_regime_filter", False):
        return True

    index_symbol = trading.get("market_regime_index", "KS11")
    ma_days = max(20, int(trading.get("market_regime_ma_days", 200)))
    need_days = ma_days + 50

    if collector is None:
        try:
            from core.data_collector import DataCollector
            collector = DataCollector()
        except Exception as e:
            logger.warning("시장 국면 필터: DataCollector 생성 실패 — 신규 매수 허용: {}", e)
            return True

    end_d = datetime.now()
    start_d = (end_d - timedelta(days=need_days + 30)).strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    try:
        df = collector.fetch_korean_stock(index_symbol, start_date=start_d, end_date=end_str)
        if df.empty or len(df) < ma_days:
            logger.warning("시장 국면 필터: 지수 {} 데이터 부족 — 신규 매수 허용", index_symbol)
            return True

        close = df["close"].astype(float)
        ma = close.rolling(ma_days, min_periods=ma_days).mean()
        last_close = close.iloc[-1]
        last_ma = ma.iloc[-1]

        if last_ma is None or (hasattr(last_ma, "item") and (last_ma != last_ma)):  # NaN
            return True
        if last_close >= last_ma:
            return True
        logger.info(
            "시장 국면: {} 종가 {:.1f} < 200일선 {:.1f} — 하락장, 신규 매수 중단",
            index_symbol, last_close, last_ma,
        )
        return False
    except Exception as e:
        logger.warning("시장 국면 필터 조회 실패 — 신규 매수 허용: {}", e)
        return True

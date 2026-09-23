"""
지수(벤치마크) 종가 캐시 — 전략 인스턴스당 넓은 구간을 한 번 받아 두고 잘라 쓴다.

strict 백테스트는 analyze()를 봉마다 df.iloc[:i+1]로 부른다. 예전 캐시는 호출의
(시작, 끝) 날짜를 키로 써서 끝 날짜가 바뀌는 매 봉마다 지수를 새로 내려받았고
(호출마다 새 DataCollector라 수집기 캐시도 못 탔다), 시장 필터는 반대로 첫 호출
구간(strict에서는 첫 1일)으로 굳어 버렸다.

규칙:
  - 첫 요청 때 [요청 시작 - warmup_days, max(오늘, 요청 끝)] 구간을 한 번 받는다.
  - 이후 요청이 받아 둔 구간 안이면 네트워크 없이 캐시를 쓴다.
  - 요청 시작이 더 이르거나(다른 종목) 요청 끝이 받아 둔 끝보다 늦으면(날짜가 넘어간
    장기 실행 프로세스) 합친 구간으로 다시 받는다.
  - 호출하는 쪽은 파생 시리즈(SMA·수익률 등 과거만 보는 계산)를 전체 캐시로 한 번
    계산해 두고(version이 바뀔 때만 재계산), 매 호출마다 요청 끝 날짜 이하로 잘라 쓴다.
    그래서 요청 시점 이후 봉은 결과에 섞이지 않는다.
  - 조회 실패도 같은 구간 동안 기억해 봉마다 재시도하지 않는다. 사유는 last_error.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from loguru import logger


class IndexCloseCache:
    """한 지수의 일봉 종가를 넓게 캐시하고 요청 끝 날짜 이하로 잘라 준다."""

    def __init__(self, symbol: str, warmup_days: int, *, label: str = ""):
        self.symbol = str(symbol)
        self.warmup_days = max(0, int(warmup_days))
        self.label = label or self.symbol
        self.fetch_count = 0          # 실제 수집 시도 횟수 (테스트·진단용)
        self.version = 0              # 수집할 때마다 증가 — 파생 시리즈 재계산 판단용
        self.last_error: str | None = None
        self._closes: pd.Series | None = None
        self._span: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self._collector = None

    def ensure(self, first, last) -> pd.Series | None:
        """[first - warmup, last]를 덮도록 캐시를 채우고 캐시된 종가 전체를 반환.

        first는 계산이 필요한 가장 이른 날짜, last는 요청 끝 날짜다. 반환 시리즈는
        last 이후 봉을 포함할 수 있으므로 호출하는 쪽이 last 이하로 잘라 써야 한다.
        받을 수 없으면 None (사유는 last_error).
        """
        first = pd.Timestamp(first).normalize()
        last = pd.Timestamp(last).normalize()
        if not self._covers(first, last):
            self._fetch(first, last)
        return self._closes

    def _covers(self, first: pd.Timestamp, last: pd.Timestamp) -> bool:
        if self._span is None:
            return False
        span_first, span_last = self._span
        return span_first <= first and last <= span_last

    def _get_collector(self):
        if self._collector is None:
            from core.data_collector import DataCollector

            self._collector = DataCollector()
            self._collector.quiet_ohlcv_log = True
        return self._collector

    def _fetch(self, first: pd.Timestamp, last: pd.Timestamp) -> None:
        if self._span is not None:
            first = min(first, self._span[0])
            last = max(last, self._span[1])
        today = pd.Timestamp(datetime.now().date())
        end = max(last, today)
        start = first - pd.Timedelta(days=self.warmup_days)
        # 실패해도 같은 구간은 다시 시도하지 않도록 구간을 먼저 기록한다.
        self._span = (first, end)
        self.fetch_count += 1
        self.version += 1
        try:
            df = self._get_collector().fetch_korean_stock(
                self.symbol,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
        except Exception as e:  # 수집기 예외 종류가 소스별로 달라 넓게 받되, 사유를 남긴다
            self._closes = None
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "{}: 지수 {} 조회 실패 ({} ~ {}) — {}",
                self.label, self.symbol, start.date(), end.date(), self.last_error,
            )
            return

        if df is None or df.empty or "close" not in df.columns:
            self._closes = None
            self.last_error = "empty"
            logger.warning(
                "{}: 지수 {} 데이터 없음 ({} ~ {})",
                self.label, self.symbol, start.date(), end.date(),
            )
            return

        if "date" in df.columns:
            df = df.set_index("date")
        closes = df["close"].astype(float)
        closes.index = pd.to_datetime(closes.index)
        if closes.index.tz is not None:
            closes.index = closes.index.tz_localize(None)
        closes = closes[~closes.index.duplicated(keep="last")].sort_index()
        self._closes = closes
        self.last_error = None

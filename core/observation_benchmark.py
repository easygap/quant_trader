"""Recent NAVER public price-index observations for paper validation only.

This is NAVER's public web feed, not a licensed/direct KRX feed. Index levels are
price indices, not dividend-reinvested total returns. No database or order access.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from core.market_research import _calendar, _sessions

KST = ZoneInfo("Asia/Seoul")
SOURCE = "NAVERPublicIndex"
SERIES = {"KS11": "KOSPI", "KS200": "KPI200", "KQ11": "KOSDAQ"}
ENDPOINT = "https://m.stock.naver.com/api/index/{series}/price"


class BenchmarkUnavailable(RuntimeError):
    """Sanitized source failure; messages never contain response bodies."""


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BenchmarkUnavailable("invalid_benchmark_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BenchmarkUnavailable("invalid_benchmark_date") from None


def _price(value):
    if isinstance(value, bool):
        raise BenchmarkUnavailable("invalid_benchmark_price")
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
            raise BenchmarkUnavailable("invalid_benchmark_price")
        value = value.replace(",", "")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise BenchmarkUnavailable("invalid_benchmark_price") from None
    if not math.isfinite(value) or value <= 0:
        raise BenchmarkUnavailable("invalid_benchmark_price")
    return value


class BenchmarkCollector:
    """One bounded recent-history request; missing exact dates never forward-fill.

    Optional transport/clock injection is for isolated tests. A clock override is
    explicitly marked replay in source metadata. Real collection uses server time.
    """
    def __init__(self, *, transport=None, clock=None):
        self._transport = transport or requests
        self._clock = clock
        self._last_info = {"source": SOURCE, "status": "not_collected"}

    def _now(self):
        value = self._clock() if self._clock else datetime.now(KST)
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BenchmarkUnavailable("aware_collection_clock_required")
        return value.astimezone(KST)

    def get_last_source_info(self):
        return dict(self._last_info)

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        self._last_info = {"source": SOURCE, "status": "unavailable", "symbol": str(symbol)}
        try:
            if symbol not in SERIES:
                raise BenchmarkUnavailable("unsupported_benchmark_symbol")
            started = self._now()
            # A small publication buffer after the 15:30 regular-market close.
            # Today's provisional row is ignored when querying an earlier day.
            cutoff = started.date() if started.time() >= time(16) else started.date() - timedelta(days=1)
            start = _date(start_date)
            end = _date(end_date)
            if start > end or end > cutoff:
                raise BenchmarkUnavailable("benchmark_session_not_completed")
            if (started.date() - start).days > 120:
                raise BenchmarkUnavailable("benchmark_recent_history_only")
            holidays = _calendar()
            years = {value[:4] for value in holidays}
            if any(str(year) not in years for year in range(start.year, started.year + 1)):
                raise BenchmarkUnavailable("benchmark_calendar_year_missing")
            dates = [str(day.date()) for day in _sessions(start.isoformat(), end.isoformat(), holidays)]
            if not dates or len(dates) > 60:
                raise BenchmarkUnavailable("invalid_benchmark_session_window")
            url = ENDPOINT.format(series=SERIES[symbol])
            try:
                response = self._transport.get(url, params={"pageSize": 60, "page": 1}, timeout=10,
                                               allow_redirects=False,
                                               headers={"User-Agent": "quant-trader-research/1.0", "Accept": "application/json"})
                if response.status_code != 200:
                    raise BenchmarkUnavailable(f"benchmark_http_{int(response.status_code)}")
                rows = response.json()
            except requests.RequestException as exc:
                raise BenchmarkUnavailable("benchmark_request_failed:" + type(exc).__name__) from None
            except (ValueError, TypeError):
                raise BenchmarkUnavailable("invalid_benchmark_json") from None
            observed = self._now()
            if observed < started:
                raise BenchmarkUnavailable("benchmark_clock_rollback")
            if not isinstance(rows, list) or not rows or len(rows) > 60:
                raise BenchmarkUnavailable("invalid_benchmark_schema")
            prices, seen = {}, set()
            for row in rows:
                if not isinstance(row, dict):
                    raise BenchmarkUnavailable("invalid_benchmark_row")
                day = _date(row.get("localTradedAt"))
                if day > cutoff:
                    continue  # A valid current/provisional response is not an error.
                if day in seen:
                    raise BenchmarkUnavailable("duplicate_benchmark_session")
                seen.add(day)
                if start <= day <= end:
                    prices[day.isoformat()] = _price(row.get("closePrice"))
            if any(day not in prices for day in dates):
                raise BenchmarkUnavailable("exact_benchmark_session_missing")
            self._last_info = {"source": SOURCE, "status": "ready", "symbol": symbol, "series": SERIES[symbol],
                               "provider": "NAVER Securities public web", "source_url": url,
                               "price_basis": "price_index", "direct_krx_feed": False,
                               "observed_at": observed.isoformat(), "quote_dates": dates,
                               "publication_timestamp": None, "replay": self._clock is not None,
                               "response_hash": hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
            frame = pd.DataFrame({"close": [prices[day] for day in dates]}, index=pd.to_datetime(dates))
            frame.index.name = "date"
            return frame
        except BenchmarkUnavailable as exc:
            self._last_info["reason"] = str(exc)
            raise
        except Exception as exc:
            self._last_info["reason"] = "benchmark_unavailable:" + type(exc).__name__
            raise BenchmarkUnavailable(self._last_info["reason"]) from None

"""
관심 종목 자동 구성 모듈.
- 수동 목록 사용
- 시가총액 상위 N개 자동 선정 (top_market_cap)
- 코스피200 구성 종목과 유사한 시총 상위 200개 (kospi200)
- 모멘텀 팩터: 12개월 수익률 상위 종목 (momentum_top)
- 저변동성 팩터: 60일 실현변동성 하위 = 저변동성 상위 (low_vol_top)
- 모멘텀+저변동성 복합: 12개월 수익률 상위이면서 저변동성 필터 (momentum_lowvol)
- 미국: S&P 500 구성 중 시총 상위 N (us_sp500_top20), NASDAQ-100 구성 중 시총 상위 N (us_nasdaq_top20)
- 유동성 필터: 20일 평균 거래대금(원) 하한으로 저유동 종목 진입 대상에서 제외 (risk_params.liquidity_filter)
- 리밸런싱 주기: 팩터·미국 지수 모드는 rebalance_interval_days(기본 20)마다
  재계산하고 그 사이에는 캐시를 사용. 매일 재계산 시 종목 교체가 잦아 거래비용이 불필요하게 증가하고,
  너무 드물면 팩터 효과가 희석됨(Jegadeesh & Titman 1993 기준 월 1회 리밸런싱이 일반적).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from config.config_loader import Config
from core.data_collector import DataCollector

_FACTOR_MODES = {
    "momentum_top",
    "low_vol_top",
    "momentum_lowvol",
    "us_sp500_top20",
    "us_nasdaq_top20",
}
_CACHE_FILENAME = "watchlist_cache.json"


class WatchlistManager:
    """watchlist 설정을 실제 종목 리스트로 해석한다.

    as_of_date를 지정하면 해당 시점 기준 종목 유니버스를 사용하여 생존자 편향을 완화한다.
    (backtest_universe.mode 설정에 따라 historical/kospi200/current 모드 적용)
    """

    def __init__(self, config: Config = None, as_of_date: str = None):
        self.config = config or Config.get()
        self.as_of_date = as_of_date

    # ------------------------------------------------------------------
    # 리밸런싱 캐시
    # ------------------------------------------------------------------

    def _cache_path(self) -> Path:
        db_path = (self.config.database or {}).get("sqlite_path", "data/quant_trader.db")
        return Path(db_path).parent / _CACHE_FILENAME

    def _load_cache(self, mode: str) -> dict | None:
        """캐시 파일에서 해당 mode의 엔트리를 반환. 없거나 파싱 실패 시 None."""
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data.get(mode)
            if entry and isinstance(entry.get("symbols"), list) and entry.get("date"):
                return entry
        except Exception as e:
            logger.debug("watchlist 캐시 로드 실패: {}", e)
        return None

    def _save_cache(self, mode: str, symbols: list[str]):
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[mode] = {
            "symbols": symbols,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug("watchlist 캐시 저장: mode={}, {}개 종목, {}", mode, len(symbols), data[mode]["date"])

    def _is_cache_valid(self, mode: str) -> bool:
        """캐시가 존재하고 rebalance_interval_days 이내인지 확인."""
        entry = self._load_cache(mode)
        if entry is None:
            return False
        interval = max(1, int(self.config.watchlist_settings.get("rebalance_interval_days", 20)))
        try:
            cached_date = datetime.strptime(entry["date"], "%Y-%m-%d")
            return (datetime.now() - cached_date).days < interval
        except Exception:
            return False

    def _get_cached_symbols(self, mode: str) -> list[str] | None:
        entry = self._load_cache(mode)
        if entry and self._is_cache_valid(mode):
            symbols = entry["symbols"]
            interval = max(1, int(self.config.watchlist_settings.get("rebalance_interval_days", 20)))
            logger.info(
                "watchlist 캐시 사용: mode={}, {}개 종목, 갱신일={} (리밸런싱 주기 {}일)",
                mode, len(symbols), entry["date"], interval,
            )
            return symbols
        return None

    def _resolve_factor_mode(self, mode: str, settings: dict, builder) -> list[str]:
        """팩터 모드 공통: 캐시 유효하면 캐시 반환, 아니면 재계산 후 캐시 저장."""
        cached = self._get_cached_symbols(mode)
        if cached:
            return cached
        symbols = builder(settings)
        if symbols:
            self._save_cache(mode, symbols)
        return symbols

    # ------------------------------------------------------------------
    # resolve
    # ------------------------------------------------------------------

    def resolve(self) -> list[str]:
        """설정 기준으로 관심 종목 리스트를 생성한다. 유동성 필터 적용 시 저유동 종목은 제외된다."""
        settings = self.config.watchlist_settings
        mode = str(settings.get("mode", "manual")).lower()
        manual_symbols = self._normalize_symbols(settings.get("symbols", []))

        if mode == "manual" and manual_symbols:
            out = self._apply_liquidity_filter(manual_symbols)
            return out if out else manual_symbols

        if mode == "top_market_cap":
            auto_symbols = self._build_top_market_cap_watchlist(settings)
            if auto_symbols:
                return auto_symbols

        if mode == "kospi200":
            kospi_settings = {**settings, "market": "KOSPI", "top_n": settings.get("kospi200_top_n", 200)}
            auto_symbols = self._build_top_market_cap_watchlist(kospi_settings)
            if auto_symbols:
                logger.info(
                    "watchlist 자동 생성 완료: mode=kospi200 (시총 상위 {}개)",
                    len(auto_symbols),
                )
                return auto_symbols

        if mode in _FACTOR_MODES:
            builder_map = {
                "momentum_top": self._build_momentum_top_watchlist,
                "low_vol_top": self._build_low_vol_top_watchlist,
                "momentum_lowvol": self._build_momentum_lowvol_watchlist,
                "us_sp500_top20": self._build_us_sp500_top20,
                "us_nasdaq_top20": self._build_us_nasdaq_top20,
            }
            auto_symbols = self._resolve_factor_mode(mode, settings, builder_map[mode])
            if auto_symbols:
                return auto_symbols

        if manual_symbols:
            out = self._apply_liquidity_filter(manual_symbols)
            return out if out else manual_symbols

        logger.warning("watchlist 설정이 비어 있어 기본 종목 005930을 사용합니다.")
        return ["005930"]

    def _get_universe_mode(self) -> str:
        """backtest_universe.mode 설정 반환. as_of_date가 있고 mode가 current면 historical로 자동 전환."""
        risk_params = getattr(self.config, "risk_params", {}) or {}
        universe = risk_params.get("backtest_universe") or {}
        mode = (universe.get("mode") or "current").strip().lower()
        if self.as_of_date and mode == "current":
            return "historical"
        return mode

    def _build_top_market_cap_watchlist(self, settings: dict) -> list[str]:
        market = str(settings.get("market", "KOSPI")).upper()
        top_n = max(1, int(settings.get("top_n", 20)))
        risk_params = getattr(self.config, "risk_params", {}) or {}
        liq = risk_params.get("liquidity_filter") or {}
        use_liq = liq.get("enabled", False)
        candidate_n = (min(100, top_n * 2) if use_liq else top_n)

        risk_params = getattr(self.config, "risk_params", {}) or {}
        universe = risk_params.get("backtest_universe") or {}
        exclude_admin = universe.get("exclude_administrative", True)
        u_mode = self._get_universe_mode()
        try:
            stocks = DataCollector.get_krx_stock_list(
                as_of_date=self.as_of_date,
                exclude_administrative=exclude_admin,
                universe_mode=u_mode,
            )
        except Exception as exc:
            logger.warning("KRX 종목 리스트 조회 실패 — 수동 watchlist로 대체: {}", exc)
            return []

        if stocks.empty:
            return []

        df = stocks.copy()
        market_col = self._pick_column(df, "Market", "market")
        code_col = self._pick_column(df, "Code", "code", "Symbol", "symbol")
        marcap_col = self._pick_column(df, "Marcap", "marcap", "Amount", "amount", "Close", "close")

        if code_col is None or marcap_col is None:
            logger.warning("watchlist 자동 생성 실패 — 필수 컬럼(Code/Marcap)을 찾을 수 없습니다.")
            return []

        if market_col is not None:
            df = df[df[market_col].astype(str).str.upper() == market]

        df = df.dropna(subset=[code_col, marcap_col]).copy()
        if df.empty:
            return []

        df[marcap_col] = df[marcap_col].astype(float)
        df = df.sort_values(marcap_col, ascending=False)
        symbols = self._normalize_symbols(df[code_col].head(candidate_n).tolist())
        if use_liq:
            symbols = self._apply_liquidity_filter(symbols)
        symbols = symbols[:top_n]

        logger.info(
            "watchlist 자동 생성 완료: mode=top_market_cap market={} top_n={} 실제={}개",
            market,
            top_n,
            len(symbols),
        )
        return symbols

    def _get_candidate_symbols(self, settings: dict, max_candidates: int) -> list[str]:
        """팩터 계산용 후보 종목 리스트 (시총 상위 N개). 유동성 필터는 상위 호출에서 적용."""
        s = {**settings, "market": settings.get("market", "KOSPI"), "top_n": max_candidates}
        return self._build_top_market_cap_watchlist(s)

    def _apply_liquidity_filter(self, symbols: list[str]) -> list[str]:
        """
        20일 평균 거래대금(원) 하한으로 저유동 종목을 제외한다.
        risk_params.liquidity_filter.enabled=false 이면 원본 그대로 반환.

        strict 모드 (기본 true): 거래대금 데이터를 조회할 수 없는 종목도 제외.
          데이터 없는 종목을 통과시키면 거래대금 1억 미만 종목이 진입 대상에 포함될 위험.
        strict=false: 데이터 없는 종목은 통과(기존 동작). 수동 watchlist에서 직접 지정한 종목 유지용.
        """
        risk_params = getattr(self.config, "risk_params", {}) or {}
        liq = risk_params.get("liquidity_filter") or {}
        if not liq.get("enabled", False):
            return list(symbols) if symbols else []
        min_krw = float(liq.get("min_avg_trading_value_20d_krw", 5_000_000_000))
        if min_krw <= 0:
            return list(symbols) if symbols else []
        strict = liq.get("strict", True)

        collector = DataCollector(self.config)
        passed = []
        skipped_no_data = []
        for sym in symbols:
            avg_val = self._compute_avg_trading_value_20d(collector, sym)
            if avg_val is not None and avg_val >= min_krw:
                passed.append(sym)
            elif avg_val is None:
                if strict:
                    skipped_no_data.append(sym)
                else:
                    passed.append(sym)
            else:
                logger.debug(
                    "유동성 필터 제외: {} (20일 평균 거래대금 {:.0f}억 원 < {:.0f}억 원)",
                    sym, avg_val / 1e8, min_krw / 1e8,
                )
        if skipped_no_data:
            logger.warning(
                "유동성 필터(strict): 거래대금 데이터 없어 제외된 종목 {}개: {}",
                len(skipped_no_data), skipped_no_data[:10],
            )
        excluded = len(symbols) - len(passed)
        if excluded > 0:
            logger.info(
                "유동성 필터 적용: {}개 중 {}개 통과 (20일 평균 거래대금 >= {:.0f}억 원, strict={})",
                len(symbols), len(passed), min_krw / 1e8, strict,
            )
        return passed

    @staticmethod
    def _compute_avg_trading_value_20d(collector: DataCollector, symbol: str) -> float | None:
        """최근 20거래일 평균 거래대금(원). close * volume 합계/일수. 데이터 부족 시 None."""
        try:
            end_d = datetime.now()
            start_d = (end_d - timedelta(days=45)).strftime("%Y-%m-%d")
            end_str = end_d.strftime("%Y-%m-%d")
            df = collector.fetch_korean_stock(symbol, start_d, end_str)
            if df.empty or len(df) < 15:
                return None
            if "close" not in df.columns or "volume" not in df.columns:
                return None
            close = df["close"].astype(float)
            vol = df["volume"].astype(float)
            trading_value = (close * vol).tail(20)
            if trading_value.isna().all() or trading_value.sum() == 0:
                return None
            return float(trading_value.mean())
        except Exception as e:
            logger.debug("20일 평균 거래대금 계산 실패 {}: {}", symbol, e)
            return None

    def _build_us_sp500_top20(self, settings: dict) -> list[str]:
        """위키백과 S&P500 목록 + yfinance 시가총액 기준 상위 N (표본 최대 120종)."""
        top_n = max(1, int(settings.get("top_n", 20)))
        try:
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                storage_options={"User-Agent": "quant-trader/1.0"},
            )
        except Exception as e:
            logger.warning("S&P500 테이블 로드 실패: {}", e)
            return []
        if not tables:
            return []
        df = tables[0]
        col = "Symbol" if "Symbol" in df.columns else None
        if col is None:
            return []
        raw = (
            df[col]
            .astype(str)
            .str.strip()
            .str.replace(".", "-", regex=False)
            .tolist()
        )
        symbols = [s for s in raw if s and s.lower() != "nan"]
        if not symbols:
            return []
        ranked = self._rank_us_symbols_by_market_cap(symbols, top_n, max_lookups=min(120, len(symbols)))
        logger.info("watchlist 자동 생성: mode=us_sp500_top20 → {}개", len(ranked))
        return ranked

    def _build_us_nasdaq_top20(self, settings: dict) -> list[str]:
        """위키백과 NASDAQ-100 구성 + yfinance 시가총액 기준 상위 N."""
        top_n = max(1, int(settings.get("top_n", 20)))
        try:
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/NASDAQ-100",
                storage_options={"User-Agent": "quant-trader/1.0"},
            )
        except Exception as e:
            logger.warning("NASDAQ-100 테이블 로드 실패: {}", e)
            return []
        symbols = []
        for tbl in tables:
            col = None
            if "Ticker" in tbl.columns:
                col = "Ticker"
            elif "Symbol" in tbl.columns:
                col = "Symbol"
            if col:
                symbols = (
                    tbl[col]
                    .astype(str)
                    .str.strip()
                    .str.replace(".", "-", regex=False)
                    .tolist()
                )
                symbols = [s for s in symbols if s and s.lower() != "nan" and len(s) <= 8]
                break
        if not symbols:
            return []
        ranked = self._rank_us_symbols_by_market_cap(symbols, top_n, max_lookups=len(symbols))
        logger.info("watchlist 자동 생성: mode=us_nasdaq_top20 → {}개", len(ranked))
        return ranked

    def _rank_us_symbols_by_market_cap(
        self, symbols: list[str], top_n: int, max_lookups: int,
    ) -> list[str]:
        """yfinance .info marketCap 기준 정렬. 조회 실패 종목은 뒤로."""
        try:
            import yfinance as yf
        except ImportError:
            return self._normalize_symbols(symbols[:top_n])

        caps: list[tuple[str, int]] = []
        for s in symbols[:max_lookups]:
            sym = str(s).strip().upper().replace(".", "-")
            mc = 0
            try:
                info = yf.Ticker(sym).info
                mc = int(info.get("marketCap") or info.get("enterpriseValue") or 0)
            except Exception:
                pass
            caps.append((sym, mc))
        caps.sort(key=lambda x: -x[1])
        out = [s for s, m in caps[:top_n] if s]
        if len(out) < top_n:
            for s in symbols:
                sym = str(s).strip().upper().replace(".", "-")
                if sym not in out:
                    out.append(sym)
                if len(out) >= top_n:
                    break
        return self._normalize_symbols(out[:top_n])

    def _build_momentum_top_watchlist(self, settings: dict) -> list[str]:
        """모멘텀 팩터: 12개월 수익률 상위 종목. 후보 풀에서 1년 수익률 계산 후 상위 top_n 반환."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n + 20, 60)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_1y = (end_d - timedelta(days=400)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        results = []
        for sym in candidates:
            ret_12m = self._compute_12m_return(collector, sym, start_1y, end_str)
            if ret_12m is not None:
                results.append((sym, ret_12m))

        if not results:
            logger.warning("모멘텀 팩터: 12개월 수익률 계산 가능한 종목 없음.")
            return []

        results.sort(key=lambda x: x[1], reverse=True)
        symbols = self._normalize_symbols([r[0] for r in results[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=momentum_top (12개월 수익률 상위 {}개)",
            len(symbols),
        )
        return symbols

    def _build_low_vol_top_watchlist(self, settings: dict) -> list[str]:
        """저변동성 팩터: 60일 실현변동성(연율화) 하위 = 저변동성 상위 종목."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n + 20, 60)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_d = (end_d - timedelta(days=130)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        results = []
        for sym in candidates:
            vol = self._compute_60d_vol(collector, sym, start_d, end_str)
            if vol is not None:
                results.append((sym, vol))

        if not results:
            logger.warning("저변동성 팩터: 60일 변동성 계산 가능한 종목 없음.")
            return []

        results.sort(key=lambda x: x[1])
        symbols = self._normalize_symbols([r[0] for r in results[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=low_vol_top (60일 저변동성 상위 {}개)",
            len(symbols),
        )
        return symbols

    def _build_momentum_lowvol_watchlist(self, settings: dict) -> list[str]:
        """모멘텀 + 저변동성: 저변동성 필터 통과 종목 중 12개월 수익률 상위."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n * 3, 80)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_1y = (end_d - timedelta(days=400)).strftime("%Y-%m-%d")
        start_60 = (end_d - timedelta(days=130)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        rows = []
        for sym in candidates:
            ret_12m = self._compute_12m_return(collector, sym, start_1y, end_str)
            vol = self._compute_60d_vol(collector, sym, start_60, end_str)
            if ret_12m is not None and vol is not None:
                rows.append({"symbol": sym, "ret_12m": ret_12m, "vol_60d": vol})

        if not rows:
            logger.warning("모멘텀+저변동성: 계산 가능한 종목 없음.")
            return []

        vol_median = float(np.median([r["vol_60d"] for r in rows]))
        filtered = [r for r in rows if r["vol_60d"] <= vol_median]
        if not filtered:
            filtered = rows
        filtered.sort(key=lambda x: x["ret_12m"], reverse=True)
        symbols = self._normalize_symbols([r["symbol"] for r in filtered[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=momentum_lowvol (저변동성 필터 후 모멘텀 상위 {}개)",
            len(symbols),
        )
        return symbols

    @staticmethod
    def _compute_12m_return(collector: DataCollector, symbol: str, start_date: str, end_date: str):
        """12개월 수익률(%). 데이터 부족 시 None."""
        try:
            df = collector.fetch_korean_stock(symbol, start_date, end_date)
            if df.empty or len(df) < 120:
                return None
            close = df["close"].astype(float).dropna()
            if len(close) < 120:
                return None
            return float((close.iloc[-1] / close.iloc[0] - 1) * 100)
        except Exception as e:
            logger.debug("12m return 계산 실패 {}: {}", symbol, e)
            return None

    @staticmethod
    def _compute_60d_vol(collector: DataCollector, symbol: str, start_date: str, end_date: str):
        """60일 실현변동성(연율화). 일일 수익률 표준편차 * sqrt(252). 데이터 부족 시 None."""
        try:
            df = collector.fetch_korean_stock(symbol, start_date, end_date)
            if df.empty or len(df) < 65:
                return None
            close = df["close"].astype(float).dropna()
            if len(close) < 65:
                return None
            ret = close.pct_change().dropna()
            vol_60 = ret.tail(60).std()
            if vol_60 is None or np.isnan(vol_60) or vol_60 <= 0:
                return None
            return float(vol_60 * np.sqrt(252))
        except Exception as e:
            logger.debug("60d vol 계산 실패 {}: {}", symbol, e)
            return None

    @staticmethod
    def _normalize_symbols(symbols) -> list[str]:
        unique = []
        seen = set()
        for symbol in symbols or []:
            value = str(symbol).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique

    @staticmethod
    def _pick_column(df, *candidates):
        for candidate in candidates:
            if candidate in df.columns:
                return candidate
        return None

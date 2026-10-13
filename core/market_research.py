"""Timestamped market observations; deliberately has no portfolio or order dependency.

Every comparison uses the benchmark's exact session dates. Current-day candles are
excluded even after the close: a daily supplier can publish provisional closes and
the project does not yet have an exchange finalization feed. Missing observations
remain missing; they never become neutral scores or synthetic prices.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION = 1


def research_directory() -> Path:
    return Path(os.environ.get("QUANT_MARKET_RESEARCH_DIR", ROOT / "output" / "market_research"))


def _now(value=None) -> datetime:
    stamp = pd.Timestamp(value if value is not None else datetime.now(KST))
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(KST)
    return stamp.tz_convert(KST).to_pydatetime()


def load_research_config(path=None) -> dict:
    from config.config_loader import load_yaml

    config = load_yaml(str(path or ROOT / "config" / "market_research.yaml"))
    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    if not isinstance(config, dict) or not isinstance(config.get("universe", []), list):
        raise ValueError("research configuration must contain an observation list")
    if not 1 <= len(config.get("universe", [])) <= 100:
        raise ValueError("research observation universe must contain 1 to 100 symbols")
    horizons = config.get("horizons", [20, 60, 120])
    if not horizons or any(type(n) is not int or n < 1 or n > 504 for n in horizons):
        raise ValueError("horizons must be positive session counts <= 504")
    if config.get("ranking_horizon", 60) not in horizons:
        raise ValueError("ranking_horizon must occur in horizons")
    rows = [config.get("benchmark", {})] + config.get("universe", [])
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("benchmark and observations must be mappings")
    symbols = [str(row.get("symbol", "")) for row in rows]
    if any(not symbol or not symbol.isalnum() for symbol in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("benchmark and observation symbols must be nonempty and unique")
    if not isinstance(config.get("require_adjusted_prices", True), bool):
        raise ValueError("require_adjusted_prices must be boolean")


def _calendar(holidays=None) -> set[str]:
    if holidays is not None:
        return {str(day)[:10] for day in holidays}
    path = ROOT / "config" / "holidays.yaml"
    # Read-only: the research path must never trigger a calendar update.
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = data.get("holidays", data.get("dates", []))
    if not isinstance(values, list) or not values:
        raise ValueError("trading holiday calendar is missing")
    return {str(day)[:10] for day in values}


def expected_session(now=None, *, holidays=None) -> str:
    current = _now(now).date()
    days = _calendar(holidays)
    if holidays is None and str(current.year) not in {d[:4] for d in days}:
        raise ValueError("trading holiday calendar does not cover this year")
    previous = current - timedelta(days=1)
    while previous.weekday() >= 5 or previous.isoformat() in days:
        previous -= timedelta(days=1)
    return previous.isoformat()


def _sessions(start: str, end: str, holidays: set[str]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([day for day in pd.bdate_range(start, end) if day.date().isoformat() not in holidays])


def _frame(raw, cutoff: str, holidays: set[str]) -> tuple[pd.DataFrame, list[str]]:
    if raw is None or raw.empty:
        return pd.DataFrame(), ["price_missing"]
    frame = raw.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    if "date" in frame:
        frame = frame.set_index("date")
    try:
        dates = pd.to_datetime(frame.index)
        if dates.tz is not None:
            dates = dates.tz_convert(KST).tz_localize(None)
        frame.index = dates.normalize()
    except (ValueError, TypeError, AttributeError):
        return pd.DataFrame(), ["invalid_dates"]
    frame = frame.loc[(frame.index <= pd.Timestamp(cutoff)) & ~frame.index.isna()].sort_index()
    frame = frame.loc[(frame.index.dayofweek < 5) & ~frame.index.strftime("%Y-%m-%d").isin(holidays)]
    if frame.index.has_duplicates:
        return pd.DataFrame(), ["duplicate_session"]
    if frame.empty or "close" not in frame:
        return pd.DataFrame(), ["price_missing"]
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame.loc[~np.isfinite(frame["close"]) | (frame["close"] <= 0), "close"] = np.nan
    if "volume" in frame:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
        frame.loc[~np.isfinite(frame["volume"]) | (frame["volume"] < 0), "volume"] = np.nan
    return frame, []


def _source(value) -> dict:
    if isinstance(value, dict):
        return {"name": value.get("name", value.get("source", "unknown")), "adjusted": value.get("adjusted"), "observed_at": value.get("observed_at")}
    name = str(value or "unknown")
    adjusted = True if name in {"FinanceDataReader", "yfinance", "FinanceDataReader+yfinance"} else (False if name == "KIS" else None)
    return {"name": name, "adjusted": adjusted, "observed_at": None}


def _rounded(value):
    return round(float(value), 6) if value is not None and math.isfinite(float(value)) else None


def _observation(spec: dict, frame: pd.DataFrame, benchmark: pd.DataFrame, expected: str,
                 horizons: list[int], source: dict, holidays: set[str], issues: list[str], require_adjusted: bool) -> dict:
    row = {key: spec.get(key, "") for key in ("symbol", "name", "cohort", "role", "group", "reference")}
    row.update(as_of=None, source=source, issues=list(issues), status="unavailable", horizons={}, volume_ratio_20d=None,
               volatility_20d_pct=None, drawdown_120d_pct=None, above_sma60=None, rank=None)
    if frame.empty:
        row["horizons"] = {str(n): None for n in horizons}
        return row
    row["as_of"] = frame.index[-1].date().isoformat()
    row["input_hash"] = hashlib.sha256(frame[[column for column in ("close", "volume") if column in frame]].to_csv().encode()).hexdigest()
    row["missing_sessions"] = max(0, len(_sessions(row["as_of"], expected, holidays)) - 1)
    if row["as_of"] != expected:
        row["issues"].append("stale_price")
    if require_adjusted and source.get("adjusted") is not True:
        row["issues"].append("adjustment_unverified")
    if "+" in str(source.get("name", "")):
        row["issues"].append("mixed_price_sources")
    if "mixed_price_sources" in row["issues"] or "adjustment_unverified" in row["issues"]:
        # Adjustment labels do not prove that two providers share the same scale.
        # A splice can manufacture momentum, so even displayed returns stay absent.
        row["horizons"] = {str(n): None for n in horizons}
        row["status"] = "partial"
        return row
    latest = pd.Timestamp(expected)
    # All assets have the same end date; a halted/missing asset is never silently
    # compared over a different interval from the benchmark or other observations.
    for count in horizons:
        value = None
        if not benchmark.empty and latest in benchmark.index and len(benchmark.loc[:latest]) > count:
            dates = benchmark.loc[:latest].index[-(count + 1):]
            expected_dates = _sessions(str(dates[0].date()), expected, holidays)
            a = frame["close"].reindex(dates)
            b = benchmark["close"].reindex(dates)
            if dates.equals(expected_dates) and a.notna().all() and b.notna().all():
                ret, bench_ret = (a.iloc[-1] / a.iloc[0] - 1) * 100, (b.iloc[-1] / b.iloc[0] - 1) * 100
                value = {"return_pct": _rounded(ret), "benchmark_return_pct": _rounded(bench_ret),
                         "excess_pct": _rounded(ret - bench_ret), "start_date": str(dates[0].date()), "end_date": expected}
        row["horizons"][str(count)] = value
        if value is None:
            row["issues"].append(f"incomplete_{count}d_window")
    for size in (21, 60, 121):
        tail = frame.tail(size)
        if len(tail) != size or row["as_of"] != expected or not tail.index.equals(_sessions(str(tail.index[0].date()), expected, holidays)) or tail["close"].isna().any():
            continue
        if size == 21:
            row["volatility_20d_pct"] = _rounded(tail["close"].pct_change(fill_method=None).dropna().std(ddof=1) * np.sqrt(252) * 100)
            if "volume" in tail and tail["volume"].notna().all() and tail["volume"].iloc[:-1].mean() > 0:
                row["volume_ratio_20d"] = _rounded(tail["volume"].iloc[-1] / tail["volume"].iloc[:-1].mean())
        elif size == 60:
            row["above_sma60"] = bool(tail["close"].iloc[-1] > tail["close"].mean())
        elif size == 121:
            row["drawdown_120d_pct"] = _rounded((tail["close"] / tail["close"].cummax() - 1).min() * 100)
    if row["volume_ratio_20d"] is None:
        row["issues"].append("volume_unavailable")
    row["status"] = "ready" if not row["issues"] else "partial"
    return row


def build_market_research(config: dict, prices: dict, *, sources=None, generated_at=None, evaluation_at=None,
                          holidays=None, previous=None, replay=True) -> dict:
    """Build a JSON-safe observation report from prices without I/O or trading."""
    _validate_config(config)
    generated = _now(generated_at)
    now = _now(evaluation_at) if evaluation_at is not None else generated
    calendar = _calendar(holidays)
    expected = expected_session(now, holidays=holidays)
    config_hash = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    horizons = config.get("horizons", [20, 60, 120])
    benchmark_spec = config["benchmark"]
    frames, frame_issues = {}, {}
    for spec in [benchmark_spec] + config.get("universe", []):
        symbol = spec["symbol"]
        frames[symbol], frame_issues[symbol] = _frame(prices.get(symbol), expected, calendar)
    benchmark = frames[benchmark_spec["symbol"]]
    args = (benchmark, expected, horizons)
    rows = [_observation(spec, frames[spec["symbol"]], *args, _source((sources or {}).get(spec["symbol"])), calendar,
                         frame_issues[spec["symbol"]], config.get("require_adjusted_prices", True))
            for spec in [benchmark_spec] + config.get("universe", [])]
    benchmark_row, observations = rows[0], rows[1:]
    benchmark_eligible = benchmark_row["as_of"] == expected and not {"adjustment_unverified", "mixed_price_sources"}.intersection(benchmark_row["issues"])
    if not benchmark_eligible:
        for row in observations:
            row["horizons"] = {str(n): None for n in horizons}
            row["issues"].append("benchmark_not_comparable")
            if row["status"] == "ready":
                row["status"] = "partial"
    rank_horizon = str(config.get("ranking_horizon", 60))
    cohorts = sorted({row["cohort"] for row in observations})
    breadth, leaders, laggards = [], [], []
    for cohort in cohorts:
        cohort_rows = [row for row in observations if row["cohort"] == cohort]
        eligible = [row for row in cohort_rows if benchmark_eligible and row["as_of"] == expected and "adjustment_unverified" not in row["issues"] and row["horizons"].get(rank_horizon) is not None]
        eligible.sort(key=lambda row: (-row["horizons"][rank_horizon]["excess_pct"], row["symbol"]))
        for rank, row in enumerate(eligible, start=1):
            row["rank"] = rank
        trend_rows = [row for row in eligible if row["above_sma60"] is not None]
        breadth.append({"cohort": cohort, "scope": "configured_observation_universe", "total": len(cohort_rows), "eligible": len(eligible),
                        "above_sma60_count": sum(row["above_sma60"] for row in trend_rows), "sma60_denominator": len(trend_rows),
                        "above_sma60_pct": _rounded(100 * sum(row["above_sma60"] for row in trend_rows) / len(trend_rows)) if trend_rows else None,
                        "outperforming_count": sum(row["horizons"][rank_horizon]["excess_pct"] > 0 for row in eligible)})
        leaders.extend(eligible[:3])
        laggards.extend(list(reversed(eligible[-3:])))
    lead_changes = []
    if previous and previous.get("replay", False) == replay and previous.get("config_hash") == config_hash and isinstance(previous.get("as_of"), str) and previous["as_of"] < expected:
        prior = {row["symbol"]: row for row in previous.get("observations", []) if row.get("rank") is not None}
        comparable_cohorts = {cohort for cohort in cohorts
                              if {row["symbol"] for row in observations if row["cohort"] == cohort and row["rank"] is not None}
                              == {row["symbol"] for row in prior.values() if row.get("cohort") == cohort}}
        for row in observations:
            old = prior.get(row["symbol"])
            if old and row["cohort"] in comparable_cohorts and row["rank"] is not None and old["rank"] != row["rank"]:
                lead_changes.append({"symbol": row["symbol"], "name": row["name"], "cohort": row["cohort"],
                                     "previous_rank": old["rank"], "rank": row["rank"], "previous_as_of": previous["as_of"]})
    ready = sum(row["status"] == "ready" for row in observations)
    ranked = sum(row["rank"] is not None for row in observations)
    return {"schema_version": SCHEMA_VERSION, "name": config.get("name", "시장 관찰 레이더"), "research_only": True,
            "status": "ready" if ready == len(observations) and benchmark_row["status"] == "ready" else ("partial" if ranked else "unavailable"),
            "generated_at": generated.isoformat(), "evaluation_at": now.isoformat(), "replay": replay,
            "as_of": expected if ranked else None, "expected_session": expected,
            "config_hash": config_hash, "ranking_horizon": int(rank_horizon), "benchmark": benchmark_row,
            "coverage": {"total": len(observations), "ready": ready, "ranked": ranked, "missing": sum(row["status"] == "unavailable" for row in observations)},
            "breadth": breadth, "leaders": leaders, "laggards": laggards, "observations": observations, "lead_changes": lead_changes,
            "limitations": ["연구 관찰값이며 주문·목표 비중·전략 승인을 변경하지 않습니다.", "당일 봉은 장 마감 후에도 제외하고 이전 거래일까지만 사용합니다.",
                            "현재 설정한 표본 안의 비교이며 전체 시장의 폭이나 과거 투자 가능 유니버스를 뜻하지 않습니다.",
                            "상대 수익률은 가격 기준이며 ETF 총수익·배당·세금·체결 비용을 보장하지 않습니다.",
                            "업종 ETF는 산업 관찰용 대리 변수입니다. 정책·소비·기술 변화의 인과관계를 검증한 신호가 아닙니다."]}


def collect_market_research(config=None, collector=None, now=None, *, previous=None) -> dict:
    """Fetch public OHLCV into memory. No database initialization or write occurs."""
    config = config or load_research_config()
    _validate_config(config)
    replay = now is not None
    now = _now(now)
    expected = expected_session(now)
    if collector is None:
        from types import SimpleNamespace
        from core.data_collector import DataCollector
        # Do not enable broker fallback merely because the trading config does.
        collector = DataCollector(SimpleNamespace(settings={"data_source": {"allow_kis_fallback": False}}))
    start = (date.fromisoformat(expected) - timedelta(days=int(max(config.get("horizons", [120])) * 2.2 + 50))).isoformat()
    prices, sources, failures = {}, {}, {}
    for spec in [config["benchmark"]] + config.get("universe", []):
        symbol = spec["symbol"]
        try:
            prices[symbol] = collector.fetch_korean_stock(symbol, start_date=start, end_date=expected)
            info = collector.get_last_source_info() if hasattr(collector, "get_last_source_info") else {}
            name = (info.get("history") or {}).get(symbol) or info.get("source") or "unknown"
            source = _source(name)
            source["observed_at"] = _now().isoformat()
            sources[symbol] = source
        except Exception as exc:
            # Exception text from providers may contain URLs/credentials; retain class only.
            failures[symbol] = type(exc).__name__
    report = build_market_research(config, prices, sources=sources, generated_at=_now(), evaluation_at=now, previous=previous, replay=replay)
    report["collection_failures"] = failures
    return report


def save_market_research(report: dict, output_dir=None) -> dict:
    """Immutable snapshot first, atomically replace latest second; no JSONL tearing."""
    directory = Path(output_dir) if output_dir else research_directory()
    snapshots = directory / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    digest = hashlib.sha256(content.encode()).hexdigest()[:20]
    snapshot = snapshots / f"{report['expected_session']}_{digest}.json"
    if snapshot.exists() and snapshot.read_text(encoding="utf-8") != content:
        raise ValueError("immutable research snapshot content mismatch")
    for target in ([snapshot] if not snapshot.exists() else []) + [directory / "latest.json"]:
        fd, temporary = tempfile.mkstemp(prefix=".research-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"latest": str(directory / "latest.json"), "snapshot": str(snapshot)}


def read_market_research(path=None, now=None) -> dict:
    """Dashboard accessor: no network, writes, or fabricated fresh observations."""
    target = Path(path) if path else research_directory() / "latest.json"
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION or report.get("research_only") is not True:
            raise ValueError("unsupported research snapshot")
        observations = report.get("observations")
        if not isinstance(observations, list) or len(observations) > 100 or not all(isinstance(row, dict) for row in observations):
            raise ValueError("invalid observations")
        if report.get("status") not in {"ready", "partial", "unavailable"} or not isinstance(report.get("benchmark"), dict):
            raise ValueError("invalid research snapshot")
        if not isinstance(report.get("generated_at"), str):
            raise ValueError("generation timestamp is missing")
        generated = _now(report["generated_at"])
        if generated > _now(now):
            raise ValueError("research snapshot is from the future")
        for row in [report["benchmark"]] + observations:
            source = row.get("source") or {}
            if not isinstance(source, dict):
                raise ValueError("invalid source provenance")
            observed_at = source.get("observed_at")
            if observed_at and report.get("replay") is not True and _now(observed_at) > generated:
                raise ValueError("source was observed after the report generation timestamp")
        expected = expected_session(now)
        as_of = report.get("as_of")
        report["read_expected_session"] = expected
        report["is_stale"] = as_of != expected
        if path is None:
            current_hash = hashlib.sha256(json.dumps(load_research_config(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            report["config_changed"] = report.get("config_hash") != current_hash
            report["is_stale"] = report["is_stale"] or report["config_changed"]
        if report["is_stale"] and report.get("status") != "unavailable":
            report["status"] = "stale"
        return report
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        return {"schema_version": SCHEMA_VERSION, "research_only": True, "status": "unavailable", "reason": "research_snapshot_unavailable", "observations": []}

"""Prospective paper-basket observations. Never initializes or writes a ledger.

The stored close TWR uses the application's start-of-period cash-flow convention;
it is an approximation when flows occur inside a valuation interval, not a GIPS
compliance claim. Local hash-chained files detect accidental edits, not a trusted
external timestamp authority. No result grants live-trading permission.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION = 1
DIRECT_CLOSE_METHOD = "observed_close_to_close_start_period_flows"
OBSERVATION_RULE = "confirmed_close_plus_30m_cutoff_ledger_v2"
# Deliberately excludes main/scheduler, dashboards, research and reporting code.
# New execution dependencies must be added explicitly when the trading path changes.
OPERATING_CODE_FILES = (
    "core/basket_rebalancer.py", "core/basket_risk.py", "core/basket_close.py", "core/basket_deploy.py",
    "core/portfolio_manager.py", "core/risk_manager.py", "core/risk_overlays.py",
    "core/order_executor.py", "core/order_guard.py", "core/order_state.py",
    "core/position_lock.py", "core/trading_hours.py", "core/data_collector.py",
    "core/market_regime.py", "core/blackswan_detector.py", "core/earnings_filter.py", "core/dart_loader.py",
    "core/instrument_classes.py", "database/models.py", "database/repositories.py",
    "config/config_loader.py",
)
LIMITATIONS = [
    "연구용 모의운용 관측이며 실계좌 자동 승격 권한이 없습니다.",
    "기록된 종가 TWR은 입출금을 측정 구간 시작에 반영하는 근사치입니다. GIPS 인증 성과가 아닙니다.",
    "모의 체결 비용은 실제 체결 품질을 입증하지 않습니다. 슬리피지는 체결 가격에 포함돼 재차 차감하지 않습니다.",
    "비교지수는 배당 제외 가격지수이며 현금·방어자산 비중이 다른 계좌와의 차이는 위험조정 알파가 아닙니다.",
    "NAVER 공개 비교지수는 공개 웹 API의 가격지수이며 계약된 거래소 데이터 피드가 아닙니다.",
    "상승·하락 표본은 비교지수의 일별 등락으로 구분하며 장기 강세장·약세장 완주를 뜻하지 않습니다.",
    "관측 최소치 충족은 수익성 합격 판정이 아닙니다. 정책 변경 이전 관측은 별도 코호트로 보존됩니다.",
]


def _utc_now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _dt(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (value.replace(tzinfo=KST) if value.tzinfo is None else value).astimezone(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a ledger number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("invalid ledger number")
    return number


def _round(value):
    return round(value, 8) if value is not None and math.isfinite(value) else None


def _yaml(path):
    # Duplicate YAML keys are rejected by the project's safe loader.
    from config.config_loader import load_yaml
    from yaml import YAMLError
    try:
        return load_yaml(str(path))
    except YAMLError:
        # Parser exceptions can include entire input lines, including credentials.
        raise ValueError("invalid_yaml_configuration") from None


def _directory(output_dir=None):
    return Path(output_dir or os.environ.get("QUANT_PAPER_VALIDATION_DIR") or ROOT / "data" / "paper_validation").resolve()


def _protocol(config_path=None):
    value = _yaml(config_path or os.environ.get("QUANT_PAPER_VALIDATION_CONFIG") or ROOT / "config" / "paper_validation.yaml")
    allowed = {"schema_version", "protocol_version", "minimum_observations", "minimum_calendar_days",
               "minimum_regime_observations", "maximum_missing_ratio", "maximum_capture_delay_sessions", "baskets"}
    if not isinstance(value, dict) or set(value) != allowed or value["schema_version"] != 1:
        raise ValueError("invalid paper-validation protocol schema")
    if not isinstance(value["protocol_version"], str) or not value["protocol_version"].strip():
        raise ValueError("protocol_version is required")
    for key in ("minimum_observations", "minimum_calendar_days", "minimum_regime_observations", "maximum_capture_delay_sessions"):
        if type(value[key]) is not int or not 1 <= value[key] <= 10000:
            raise ValueError(f"invalid {key}")
    if not 0 <= _number(value["maximum_missing_ratio"]) < 1:
        raise ValueError("invalid maximum_missing_ratio")
    if not isinstance(value["baskets"], dict) or not value["baskets"]:
        raise ValueError("baskets are required")
    for name, spec in value["baskets"].items():
        benchmark = spec.get("benchmark", {}) if isinstance(spec, dict) else {}
        if (not isinstance(name, str) or set(spec) != {"benchmark"}
                or set(benchmark) != {"symbol", "name", "basis", "provider"}
                or benchmark.get("symbol") not in {"KS11", "KS200", "KQ11"}
                or benchmark.get("provider") not in {"naver_public", "data_collector"}
                or benchmark.get("basis") != "price_index" or not benchmark.get("name")):
            raise ValueError("a named Korean price-index benchmark is required for each basket")
    return value


def _calendar():
    value = _yaml(ROOT / "config" / "holidays.yaml")
    holidays = {str(item) for item in value.get("holidays", [])}
    if not holidays:
        raise ValueError("trading_calendar_missing")
    for day in holidays:
        date.fromisoformat(day)
    return holidays


def _sessions(start, end, holidays):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        return []
    years = {int(day[:4]) for day in holidays}
    if any(year not in years for year in range(first.year, last.year + 1)):
        raise ValueError("trading_calendar_year_missing")
    result = []
    while first <= last:
        if first.weekday() < 5 and first.isoformat() not in holidays:
            result.append(first.isoformat())
        first += timedelta(days=1)
    return result


def _expected(now, holidays, market_close="15:30"):
    # Paper evidence uses an explicitly confirmed close, unlike the T-1 research
    # radar. Availability starts 30 minutes after the configured market close.
    local = now.astimezone(KST)
    available = datetime.combine(local.date(), time.fromisoformat(market_close), KST) + timedelta(minutes=30)
    end = local.date() if local >= available else local.date() - timedelta(days=1)
    return _sessions((end - timedelta(days=31)).isoformat(), end.isoformat(), holidays)[-1]


def _cohort_protocol(protocol, name):
    benchmark = protocol["baskets"][name]["benchmark"]
    value = {**{k: v for k, v in protocol.items() if k != "baskets"},
             "benchmark": benchmark, "observation_rule": OBSERVATION_RULE}
    if benchmark["provider"] == "naver_public":
        value["benchmark_collector_code"] = hashlib.sha256((ROOT / "core" / "observation_benchmark.py").read_bytes()).hexdigest()
    return value


def _load_events(directory):
    events, previous = [], None
    for index, path in enumerate(sorted((directory / "events").glob("*.json")), 1):
        event = json.loads(path.read_text(encoding="utf-8"))
        digest = event.pop("hash", None)
        if (event.get("sequence") != index or event.get("previous_hash") != previous
                or digest != _hash(event) or event.get("schema_version") != SCHEMA_VERSION
                or path.name != f"{index:08d}_{digest}.json"):
            raise ValueError("paper_validation_journal_integrity_error")
        if events and _dt(event["observed_at"]) < _dt(events[-1]["observed_at"]):
            raise ValueError("paper_validation_clock_rollback")
        event["hash"] = digest
        events.append(event)
        previous = digest
    return events


def _append(directory, events, kind, data, now):
    if events and now < _dt(events[-1]["observed_at"]):
        raise ValueError("paper_validation_clock_rollback")
    event = {"schema_version": 1, "sequence": len(events) + 1,
             "previous_hash": events[-1]["hash"] if events else None,
             "observed_at": _stamp(now), "kind": kind, "data": data}
    digest = _hash(event)
    event["hash"] = digest
    folder = directory / "events"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{len(events) + 1:08d}_{digest}.json"
    temporary = folder / f".{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(event, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # Atomic publication, never overwrite a revision.
    finally:
        temporary.unlink(missing_ok=True)
    events.append(event)


def _observations(events, cohort_id):
    """A retry adds a later observation; the original failed attempt stays intact."""
    rows = {}
    for event in events:
        data = event["data"]
        if data.get("cohort_id") != cohort_id:
            continue
        if event["kind"] == "close_observed":
            rows[data["observation"]["date"]] = json.loads(json.dumps(data["observation"]))
        elif event["kind"] == "benchmark_observed" and data["date"] in rows:
            row = rows[data["date"]]
            if row["source_hash"] != data["valuation_source_hash"]:
                raise ValueError("benchmark_close_link_mismatch")
            row["benchmark"] = data["benchmark"]
            row["issues"] = [issue for issue in row["issues"] if not issue.startswith("benchmark_")]
    return [rows[key] for key in sorted(rows)]


@contextmanager
def _exclusive_lock(directory):
    # OS locks release on process exit/timeout, unlike an O_EXCL sentinel file.
    with (directory / ".collect.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("paper_validation_collection_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _operating_material(basket, settings, risk):
    policy = {k: v for k, v in basket.items() if k not in {"name", "purpose", "enabled", "primary", "monitoring", "promotion"}}
    # Only a digest of configuration is persisted. Never serialize credentials or
    # arbitrary data-source settings to the journal/dashboard.
    data_source = {k: settings.get("data_source", {}).get(k) for k in ("preferred", "allow_kis_fallback")}
    material = {"basket": policy, "risk": risk, "trading": settings.get("trading", {}), "data_source": data_source,
                "dart_enabled": settings.get("dart", {}).get("enabled", False)}
    if policy.get("signal_weighted") or settings.get("trading", {}).get("market_regime_filter"):
        strategies = _yaml(ROOT / "config" / "strategies.yaml")
        material["strategies"] = strategies if policy.get("signal_weighted") else strategies.get("regime_adaptive", {})
    config_hash = _hash(material)
    code = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in OPERATING_CODE_FILES}
    if policy.get("signal_weighted"):
        for path in sorted((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / "indicator_engine.py"]:
            code[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    exposure = {k: policy.get(k) for k in ("holdings", "target_stock_weight", "diversification", "overlays", "contribution")}
    return config_hash, _hash(code), code, exposure


def _read_database(path):
    if not path.is_file():
        raise FileNotFoundError("paper_ledger_missing")
    # Do not call init_database/Config database engines, including for an empty DB.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result, issues = {}, []
        for name in ("portfolio_valuations", "trade_history", "cash_flows"):
            if name not in tables:
                result[name] = None
                issues.append(f"{name}_missing")
            else:
                result[name] = [dict(row) for row in connection.execute(f"SELECT * FROM {name} WHERE mode='paper'")]
        result["historical_snapshot_counts"] = ({row[0]: row[1] for row in connection.execute(
            "SELECT account_key, COUNT(*) FROM portfolio_snapshots WHERE mode='paper' GROUP BY account_key")}
            if "portfolio_snapshots" in tables else {})
        return result, issues


def _source_close(row, all_rows, now, market_close="15:30", initial_capital=None):
    day = str(row["date"])[:10]
    measured, recorded = _dt(row["measured_at"]), _dt(row["recorded_at"])
    if (measured.astimezone(KST).date().isoformat() != day or not measured <= recorded <= now
            or measured.astimezone(KST).time() != time.fromisoformat(market_close)):
        raise ValueError("close_timestamp_invalid")
    context = json.loads(row["detail"])
    method = context.get("return_method")
    direct = method == DIRECT_CLOSE_METHOD
    if method not in {"linked_measurements_start_period_flows", DIRECT_CLOSE_METHOD} or context.get("quote_dates") != [day]:
        raise ValueError("close_method_unverified")
    total, cash = _number(row["total_value"], True), _number(row["cash"])
    index = 1 + _number(row["cumulative_return"]) / 100
    if index <= 0 or cash < -1 or cash > total + 1:
        raise ValueError("close_nav_invalid")
    positions = context.get("positions")
    if not isinstance(positions, list):
        raise ValueError("close_positions_missing")
    values, sources, symbols = 0.0, set(), set()
    for position in positions:
        quantity = _number(position["quantity"], True)
        price = _number(position["price"], True)
        source = position.get("source")
        if (quantity != int(quantity) or position.get("quote_date") != day or not source or source == "unknown"
                or position["symbol"] in symbols or not measured <= _dt(position["observed_at"]) <= recorded):
            raise ValueError("close_quote_unverified")
        values += quantity * price
        sources.add(source)
        symbols.add(position["symbol"])
    if abs(cash + values - total) > max(1.0, total * 1e-8):
        raise ValueError("close_holdings_nav_mismatch")
    flow = _number(context.get("flow_since"))
    baseline = all_rows.get(context.get("baseline_valuation_id"))
    extra = {"return_method": method}
    first = False
    if direct:
        capital = _number(context.get("initial_capital"), True)
        trade_ids = context.get("trade_ids")
        if (initial_capital is None or capital != _number(initial_capital, True)
                or _dt(context.get("ledger_cutoff_at")) != measured
                or type(context.get("first_close_in_series")) is not bool
                or not isinstance(trade_ids, list) or any(type(item) is not int for item in trade_ids)
                or len(trade_ids) != len(set(trade_ids))):
            raise ValueError("close_ledger_context_unverified")
        prior = [item for item in all_rows.values() if item.get("kind") == "close"
                 and item["account_key"] == row["account_key"] and _dt(item["measured_at"]) < measured]
        prior.sort(key=lambda item: (_dt(item["measured_at"]), item["id"]))
        first = context["first_close_in_series"]
        if first:
            if (prior or context.get("baseline_valuation_id") is not None
                    or _dt(context.get("baseline_measured_at")) != measured or flow != 0):
                raise ValueError("close_bootstrap_unverified")
        elif not prior or baseline is None or baseline["id"] != prior[-1]["id"]:
            raise ValueError("close_baseline_unverified")
        extra.update(initial_capital=capital, ledger_trade_ids=sorted(trade_ids), first_close_in_series=first)
    if first:
        expected_index = 1.0
    else:
        if (not baseline or baseline.get("kind") != ("close" if direct else "intraday")
                or baseline["account_key"] != row["account_key"]
                or _dt(baseline["measured_at"]) != _dt(context.get("baseline_measured_at"))
                or not _dt(baseline["measured_at"]) <= measured
                or not _dt(baseline["measured_at"]) <= _dt(baseline["recorded_at"]) <= recorded
                or (str(baseline["date"])[:10] >= day if direct else str(baseline["date"])[:10] != day)):
            raise ValueError("close_baseline_unverified")
        denominator = _number(baseline["total_value"], True) + flow
        if denominator <= 0:
            raise ValueError("close_flow_base_invalid")
        expected_index = (1 + _number(baseline["cumulative_return"]) / 100) * total / denominator
    if not math.isclose(index, expected_index, rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("close_twr_mismatch")
    return {"date": day, "valuation_id": row["id"], "source_hash": _hash(row), "measured_at": _stamp(measured),
            "recorded_at": _stamp(recorded), "total_value": total, "cash": cash, "twr_index": index,
            "invested_ratio": (total - cash) / total, "sources": sorted(sources),
            "baseline_valuation_id": baseline["id"] if baseline else None,
            "baseline_source_hash": _hash(baseline) if baseline else None,
            "baseline_measured_at": context["baseline_measured_at"], "close_flow_since": flow,
            "positions": positions, **extra}


def _make_benchmark_collector(benchmark):
    if benchmark["provider"] == "naver_public":
        from core.observation_benchmark import BenchmarkCollector
        return BenchmarkCollector()
    from core.data_collector import DataCollector
    return DataCollector(SimpleNamespace(settings={"data_source": {"preferred": "auto", "allow_kis_fallback": False}}))


def _benchmark(collector, symbol, day, now, provider="data_collector"):
    try:
        frame = collector.fetch_korean_stock(symbol, day, day)
        source_info = collector.get_last_source_info()
        source = source_info.get("source")
        if not source or source == "unknown" or "+" in source or source == "KIS":
            return None, "benchmark_source_unverified"
        if (provider == "naver_public") != (source == "NAVERPublicIndex"):
            return None, "benchmark_provider_mismatch"
        if source_info.get("replay") is True:
            return None, "benchmark_replay_not_forward"
        if frame is None or frame.empty:
            return None, "benchmark_price_missing"
        frame = frame.copy()
        frame.columns = [str(column).lower() for column in frame.columns]
        dates = frame["date"] if "date" in frame else frame.index
        matches = [index for index, value in enumerate(dates) if str(value)[:10] == day]
        if len(matches) != 1 or "close" not in frame:
            return None, "benchmark_exact_date_missing"
        observed = _utc_now()
        if source_info.get("observed_at") is not None and _dt(source_info["observed_at"]) > observed:
            return None, "benchmark_observation_in_future"
        quote = {"symbol": symbol, "date": day, "close": _number(frame["close"].iloc[matches[0]], True),
                 "source": source, "basis": "price_index", "observed_at": _stamp(observed)}
        if provider == "naver_public":
            quote["provenance"] = {key: source_info.get(key) for key in (
                "source_url", "response_hash", "provider", "publication_timestamp", "direct_krx_feed", "observed_at")}
        return quote, None
    except Exception as exc:
        return None, f"benchmark_unavailable:{type(exc).__name__}"


def _ledger_interval(database, key, start, end, close):
    result, issues = {"trades": None, "flows": None}, []
    for table, stamp, target in (("trade_history", "executed_at", "trades"), ("cash_flows", "occurred_at", "flows")):
        rows = database.get(table)
        if rows is None:
            issues.append(f"{table}_missing")
            continue
        selected = []
        try:
            for row in rows:
                if row["account_key"] != key:
                    continue
                at = _dt(row[stamp])
                if start < at <= end:
                    if target == "trades":
                        if row["action"] not in {"BUY", "SELL"}:
                            raise ValueError("trade_action_invalid")
                        item = {"id": row["id"], "executed_at": row[stamp], "action": row["action"], "source_hash": _hash(row)}
                        item.update({field: _number(row[field]) for field in ("total_amount", "commission", "tax", "slippage")})
                        if any(item[field] < 0 for field in ("total_amount", "commission", "tax", "slippage")):
                            raise ValueError("negative_trade_cost")
                    else:
                        item = {"id": row["id"], "occurred_at": row[stamp], "amount": _number(row["amount"]), "source_hash": _hash(row)}
                    selected.append(item)
            result[target] = sorted(selected, key=lambda row: row["id"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"{table}_invalid")
    if database.get("cash_flows") is not None:
        try:
            flow = sum(_number(row["amount"]) for row in database["cash_flows"] if row["account_key"] == key
                       and _dt(close["baseline_measured_at"]) < _dt(row["occurred_at"]) <= end)
            if not math.isclose(flow, close["close_flow_since"], abs_tol=1e-6):
                issues.append("close_cash_flow_mismatch")
        except (TypeError, KeyError, ValueError):
            issues.append("cash_flows_invalid")
    if close.get("return_method") == DIRECT_CLOSE_METHOD:
        # The first close has no return interval, but its cash/holdings must still
        # reconcile to the complete ledger. Preserve membership and hashes so a
        # later INSERT/revision before that first close cannot silently pass.
        result["cutoff_ledger"] = None
        try:
            trades = [row for row in database["trade_history"] if row["account_key"] == key and _dt(row["executed_at"]) <= end]
            flows = [row for row in database["cash_flows"] if row["account_key"] == key and _dt(row["occurred_at"]) <= end]
            if sorted(row["id"] for row in trades) != close["ledger_trade_ids"]:
                issues.append("close_ledger_trade_ids_mismatch")
            cash = _number(close["initial_capital"], True) + sum(_number(row["amount"]) for row in flows)
            quantities = {}
            for row in trades:
                if row["action"] not in {"BUY", "SELL"} or not row["symbol"]:
                    raise ValueError("trade_action_or_symbol_invalid")
                qty = _number(row["quantity"], True)
                amount, commission, tax = (_number(row[field]) for field in ("total_amount", "commission", "tax"))
                if qty != int(qty) or min(amount, commission, tax) < 0:
                    raise ValueError("trade_quantity_or_cost_invalid")
                sign = 1 if row["action"] == "BUY" else -1
                quantities[row["symbol"]] = quantities.get(row["symbol"], 0) + sign * qty
                cash -= sign * amount + commission + tax
            expected_quantities = {symbol: qty for symbol, qty in quantities.items() if qty != 0}
            actual_quantities = {row["symbol"]: row["quantity"] for row in close["positions"]}
            if expected_quantities != actual_quantities:
                issues.append("close_ledger_quantity_mismatch")
            if not math.isclose(cash, close["cash"], rel_tol=1e-8, abs_tol=1e-6):
                issues.append("close_ledger_cash_mismatch")
            result["cutoff_ledger"] = {name: [{"id": row["id"], "source_hash": _hash(row)} for row in sorted(rows, key=lambda row: row["id"])]
                                       for name, rows in (("trades", trades), ("flows", flows))}
        except (TypeError, KeyError, ValueError):
            issues.append("close_ledger_unverified")
    return result, issues


def _summarize(cohort, observations, expected, holidays, issues):
    protocol = cohort["protocol"]
    observations = sorted(observations, key=lambda row: row["date"])
    dates = [row["date"] for row in observations]
    start = _dt(cohort["started_at"]).astimezone(KST)
    # Registration is an observation boundary, never a synthetic funded close.
    first_possible = start.date().isoformat()
    if start.time() >= time.fromisoformat(cohort["market_close"]):
        first_possible = (start.date() + timedelta(days=1)).isoformat()
    expected_dates = _sessions(first_possible, expected, holidays)
    missing = sorted(set(expected_dates) - set(dates))
    ratio = len(missing) / len(expected_dates) if expected_dates else None
    performance = {"start_date": dates[0] if dates else None, "end_date": dates[-1] if dates else None,
                   "twr_pct": None, "benchmark_return_pct": None, "excess_return_pct": None,
                   "drawdown_pct": None, "return_method": "stored_close_twr_start_period_flows"}
    regimes = {name: {"observations": 0, "portfolio_return_pct": None, "benchmark_return_pct": None,
                       "excess_return_pct": None, "classification": "benchmark_adjacent_session_return_sign",
                       "return_scope": "compounded_selected_days_not_a_contiguous_period"} for name in ("up", "down", "flat")}
    pairs = []
    for previous, current in zip(observations, observations[1:]):
        a, b = previous.get("benchmark"), current.get("benchmark")
        if (len(_sessions(previous["date"], current["date"], holidays)) == 2 and a and b
                and a["source"] == b["source"] and not previous["issues"] and not current["issues"]):
            pairs.append((current["twr_index"] / previous["twr_index"] - 1, b["close"] / a["close"] - 1))
    if len(observations) >= 2:
        first, last = observations[0], observations[-1]
        performance["twr_pct"] = _round((last["twr_index"] / first["twr_index"] - 1) * 100)
        benchmarks = [row.get("benchmark") for row in observations]
        valid = [row for row in benchmarks if row]
        if benchmarks[0] and benchmarks[-1] and len({row["source"] for row in valid}) == 1:
            result = (benchmarks[-1]["close"] / benchmarks[0]["close"] - 1) * 100
            performance["benchmark_return_pct"] = _round(result)
            performance["excess_return_pct"] = _round(performance["twr_pct"] - result)
        peak, drawdown = first["twr_index"], 0.0
        for row in observations:
            peak = max(peak, row["twr_index"])
            drawdown = min(drawdown, row["twr_index"] / peak - 1)
        performance["drawdown_pct"] = _round(drawdown * 100)
        performance["drawdown_scope"] = "observed_closes_only"
    for name, values in regimes.items():
        selected = [(a, b) for a, b in pairs if ("up" if b > 0 else "down" if b < 0 else "flat") == name]
        values["observations"] = len(selected)
        if selected:
            a, b = [(math.prod(1 + pair[index] for pair in selected) - 1) * 100 for index in (0, 1)]
            values.update(portfolio_return_pct=_round(a), benchmark_return_pct=_round(b), excess_return_pct=_round(a - b))
    intervals = [row["ledger"] for row in observations[1:]]
    costs = {"commission": None, "tax": None, "explicit_cost": None, "modeled_slippage_embedded": None,
             "gross_turnover_ratio": None, "trade_count": None, "net_external_flow": None,
             "flow_count": None, "cost_basis": "already_in_nav_do_not_subtract_again",
             "turnover_method": "gross_notional_over_mean_observed_close_nav_not_annualized"}
    if intervals and all(row["trades"] is not None for row in intervals):
        trades = [trade for row in intervals for trade in row["trades"]]
        costs.update(commission=_round(sum(t["commission"] for t in trades)), tax=_round(sum(t["tax"] for t in trades)),
                     modeled_slippage_embedded=_round(sum(t["slippage"] for t in trades)), trade_count=len(trades))
        costs["explicit_cost"] = _round(costs["commission"] + costs["tax"])
        mean_nav = sum(row["total_value"] for row in observations) / len(observations)
        costs["gross_turnover_ratio"] = _round(sum(t["total_amount"] for t in trades) / mean_nav)
    if intervals and all(row["flows"] is not None for row in intervals):
        flows = [flow for row in intervals for flow in row["flows"]]
        costs.update(net_external_flow=_round(sum(flow["amount"] for flow in flows)), flow_count=len(flows))
    all_issues = set(issues)
    for row in observations:
        all_issues.update(row["issues"])
        if not row.get("benchmark"):
            all_issues.add("benchmark_missing")
    if len({row["benchmark"]["source"] for row in observations if row.get("benchmark")}) > 1:
        all_issues.add("benchmark_source_changed")
    if missing:
        all_issues.add("close_sessions_missing")
    benchmark_missing = [row["date"] for row in observations if not row.get("benchmark")]
    paired_missing_ratio = (len(set(missing) | set(benchmark_missing)) / len(expected_dates)) if expected_dates else None
    calendar_days = max(0, (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days) if dates else 0
    integrity_issues = {issue for issue in all_issues if issue != "close_sessions_missing"
                        and not issue.startswith(("benchmark_missing", "benchmark_price_missing", "benchmark_exact_date_missing", "benchmark_unavailable:"))}
    if any(issue.startswith(("observed_ledger_", "close_cash_flow_", "close_ledger_", "cash_flows_")) for issue in integrity_issues):
        performance.update(twr_pct=None, excess_return_pct=None, drawdown_pct=None, status="unverified_ledger")
    else:
        performance["status"] = "observed" if len(observations) >= 2 else "insufficient_sample"
    requirements = {"observations": len(pairs) >= protocol["minimum_observations"],
                    "calendar_days": calendar_days >= protocol["minimum_calendar_days"],
                    "up_regime": regimes["up"]["observations"] >= protocol["minimum_regime_observations"],
                    "down_regime": regimes["down"]["observations"] >= protocol["minimum_regime_observations"],
                    "coverage": paired_missing_ratio is not None and paired_missing_ratio <= protocol["maximum_missing_ratio"],
                    "integrity": not integrity_issues}
    if not observations:
        status = "awaiting_forward" if not expected_dates and not all_issues else "data_gaps"
    elif missing and missing[-1] == expected:
        status = "stale"
    elif not requirements["integrity"] or not requirements["coverage"]:
        status = "data_gaps"
    elif all(requirements.values()):
        status = "ready_for_review"
    else:
        status = "insufficient_sample"
    return {"basket": cohort["basket"], "account_key": cohort["account_key"], "cohort_id": cohort["cohort_id"],
            "started_at": cohort["started_at"], "market_close": cohort["market_close"], "active": True, "status": status,
            "observations": len(observations), "return_observations": len(pairs),
            "minimum_observations": protocol["minimum_observations"], "minimum_calendar_days": protocol["minimum_calendar_days"],
            "minimum_regime_observations": protocol["minimum_regime_observations"], "calendar_days": calendar_days,
            "requirements": requirements, "performance": performance, "regimes": regimes, "costs": costs,
            "coverage": {"expected_sessions": len(expected_dates), "observed_sessions": len(observations),
                         "missing_sessions": missing, "missing_ratio": _round(ratio), "expected_through": expected,
                         "benchmark_missing_sessions": benchmark_missing, "paired_missing_ratio": _round(paired_missing_ratio),
                         "maximum_missing_ratio": protocol["maximum_missing_ratio"]},
            "exposure_policy": cohort["exposure_policy"], "benchmark": cohort["benchmark"],
            "latest_invested_ratio": observations[-1]["invested_ratio"] if observations else None,
            "fingerprint": cohort["fingerprint"], "registration_reason": cohort["registration_reason"],
            "historical_closes_excluded": cohort["historical_closes_excluded"],
            "historical_snapshots_excluded": cohort["historical_snapshots_excluded"], "issues": sorted(all_issues),
            "research_only": True, "automatic_live_promotion": False, "profitability_pass": None}


def _collect(directory, events, db_path, protocol, collector, now):
    settings = _yaml(ROOT / "config" / "settings.yaml")
    risk = _yaml(ROOT / "config" / "risk_params.yaml")
    baskets = _yaml(ROOT / "config" / "baskets.yaml").get("baskets", {})
    path = Path(db_path or os.environ.get("QUANT_DB_PATH") or settings.get("database", {}).get("sqlite_path", "data/quant_trader.db"))
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    database, database_issues = _read_database(path)
    holidays = _calendar()
    expected = _expected(now, holidays, settings.get("trading", {}).get("market_close", "15:30"))
    registrations = [event["data"] for event in events if event["kind"] == "cohort_registered"]
    closed = {event["data"]["cohort_id"] for event in events if event["kind"] == "cohort_ended"}
    active = {row["basket"]: row for row in registrations if row["cohort_id"] not in closed}
    selected = {name for name in protocol["baskets"] if baskets.get(name, {}).get("enabled") is True}
    for name, cohort in active.copy().items():
        if name not in selected:
            _append(directory, events, "cohort_ended", {"cohort_id": cohort["cohort_id"], "reason": "basket_disabled_or_protocol_removed"}, _utc_now())
            del active[name]
    results = []
    supplied_collector = collector
    for name in sorted(selected):
        collector = supplied_collector  # A basket's provider never leaks into another policy.
        key = f"basket_rebalance:{name}"
        config_hash, code_hash, files, exposure = _operating_material(baskets[name], settings, risk)
        benchmark = protocol["baskets"][name]["benchmark"]
        own_protocol = _cohort_protocol(protocol, name)
        initial_capital = baskets[name].get("initial_capital")
        if initial_capital is None:
            initial_capital = risk.get("position_sizing", {}).get("initial_capital", 10000000)
        initial_capital = _number(initial_capital, True)
        fingerprint = {"config": config_hash, "code": code_hash, "protocol": _hash(own_protocol),
                       "source_database": _hash(str(path))}
        rows = [row for row in database["portfolio_valuations"] or [] if row["account_key"] == key]
        close_rows = [row for row in rows if row["kind"] == "close"]
        by_id = {row["id"]: row for row in rows}
        cohort = active.get(name)
        if cohort is None or cohort["fingerprint"] != fingerprint:
            reasons = [k + "_changed" for k in fingerprint if cohort and cohort["fingerprint"].get(k) != fingerprint[k]]
            if cohort:
                _append(directory, events, "cohort_ended", {"cohort_id": cohort["cohort_id"], "reason": reasons}, _utc_now())
            registered = _utc_now()
            cohort = {"cohort_id": uuid.uuid4().hex, "basket": name, "account_key": key, "started_at": _stamp(registered),
                      "protocol": own_protocol, "fingerprint": fingerprint, "operating_code_files": files,
                      "exposure_policy": exposure, "benchmark": benchmark, "registration_reason": reasons or ["first_registration"],
                      "market_close": settings.get("trading", {}).get("market_close", "15:30"), "initial_capital": initial_capital,
                      "excluded_valuation_ids": sorted(row["id"] for row in close_rows),
                      "historical_closes_excluded": len(close_rows),
                      "historical_snapshots_excluded": database["historical_snapshot_counts"].get(key, 0)}
            _append(directory, events, "cohort_registered", cohort, registered)
        observations = _observations(events, cohort["cohort_id"])
        issues = list(database_issues)
        for index, observation in enumerate(observations):
            original = by_id.get(observation["valuation_id"])
            if original is None or _hash(original) != observation["source_hash"]:
                issues.append("observed_ledger_revised_or_missing")
            if observation["baseline_valuation_id"] is not None:
                baseline = by_id.get(observation["baseline_valuation_id"])
                if baseline is None or _hash(baseline) != observation["baseline_source_hash"]:
                    issues.append("observed_ledger_revised_or_missing")
            if original is not None and observation.get("return_method") == DIRECT_CLOSE_METHOD:
                try:
                    _source_close(original, by_id, _utc_now(), cohort["market_close"], cohort.get("initial_capital"))
                except (TypeError, KeyError, ValueError, OverflowError):
                    issues.append("observed_ledger_revised_or_missing")
            previous = _dt(observations[index - 1]["measured_at"]) if index else _dt(observation["measured_at"])
            ledger, audit_issues = _ledger_interval(database, key, previous, _dt(observation["measured_at"]), observation)
            if ledger != observation["ledger"] or audit_issues:
                issues.append("observed_ledger_revised_or_missing")
            if (observation.get("benchmark") is None
                    and len(_sessions(observation["date"], expected, holidays)) - 1 <= protocol["maximum_capture_delay_sessions"]):
                if collector is None:
                    collector = _make_benchmark_collector(benchmark)
                quote, error = _benchmark(collector, benchmark["symbol"], observation["date"], now, benchmark["provider"])
                if quote:
                    _append(directory, events, "benchmark_observed", {"cohort_id": cohort["cohort_id"], "date": observation["date"],
                            "valuation_source_hash": observation["source_hash"], "benchmark": quote}, _utc_now())
        observations = _observations(events, cohort["cohort_id"])
        seen = {row["date"] for row in observations}
        excluded = set(cohort["excluded_valuation_ids"])
        for row in sorted(close_rows, key=lambda row: (str(row["date"]), row["id"])):
            day = str(row["date"])[:10]
            if row["id"] in excluded or day > expected or _dt(row["measured_at"]) <= _dt(cohort["started_at"]):
                continue
            if day in seen:
                if any(ob["date"] == day and ob["valuation_id"] != row["id"] for ob in observations):
                    issues.append("duplicate_close_date")
                continue
            if observations and day <= observations[-1]["date"]:
                issues.append("late_close_not_appended")
                continue
            if not _sessions(day, day, holidays):
                issues.append("non_session_close")
                continue
            if len(_sessions(day, expected, holidays)) - 1 > protocol["maximum_capture_delay_sessions"]:
                continue  # Historical recovery may exist in the source, but is not forward evidence.
            try:
                close = _source_close(row, by_id, _utc_now(), cohort["market_close"], cohort.get("initial_capital"))
                previous = _dt(observations[-1]["measured_at"]) if observations else _dt(close["measured_at"])
                close["ledger"], close["issues"] = _ledger_interval(database, key, previous, _dt(close["measured_at"]), close)
                if collector is None:
                    collector = _make_benchmark_collector(benchmark)
                close["benchmark"], error = _benchmark(collector, benchmark["symbol"], day, now, benchmark["provider"])
                if error:
                    close["issues"].append(error)
                observed = _utc_now()
                close["observed_at"] = _stamp(observed)
                close["calendar_hash"] = _hash(sorted(holidays))
                _append(directory, events, "close_observed", {"cohort_id": cohort["cohort_id"], "observation": close}, observed)
                observations.append(close)
                seen.add(day)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                issues.append(f"invalid_close:{day}:{type(exc).__name__}")
        summary = _summarize(cohort, observations, expected, holidays, issues)
        prior = next((event for event in reversed(events) if event["kind"] == "assessment" and event["data"]["cohort_id"] == cohort["cohort_id"]), None)
        daily = {**summary, "assessment_date": now.astimezone(KST).date().isoformat()}
        if prior is None or prior["data"] != daily:
            _append(directory, events, "assessment", daily, _utc_now())
        results.append(cohort["cohort_id"])
    # Also records successful no-change runs, separately from market observations.
    _append(directory, events, "collection_completed", {"active_cohort_ids": results, "expected_through": expected}, _utc_now())


def collect_paper_validation(db_path=None, *, config_path=None, output_dir=None, collector=None):
    """Stamp the real server clock; read ledger via SQLite mode=ro; append evidence.

    There is intentionally no --now/as_of/backdate input on the collection path.
    Missing database files/tables are reported without initializing any schema.
    """
    directory = _directory(output_dir)
    if directory in {ROOT, ROOT / "data", ROOT / "config"}:
        raise ValueError("use a dedicated paper-validation output directory")
    directory.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(directory):
        events = _load_events(directory)
        now = _utc_now()
        if events and now < _dt(events[-1]["observed_at"]):
            raise ValueError("paper_validation_clock_rollback")
        try:
            _collect(directory, events, db_path, _protocol(config_path), collector, now)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            # Only error type: source paths, configuration values and secrets stay private.
            _append(directory, events, "collection_failed", {"error": type(exc).__name__}, _utc_now())
    return read_paper_validation(directory)


def read_paper_validation(output_dir=None):
    """Read saved files and default-path freshness. No DB/network writes or enrollment.

    The default operational journal is checked against current configuration and
    code. An explicit artifact directory retains its own historical policy context.
    """
    result = {"schema_version": 1, "research_only": True, "automatic_live_promotion": False,
              "status": "not_started", "updated_at": None, "cohorts": [], "issues": [], "limitations": LIMITATIONS}
    try:
        events = _load_events(_directory(output_dir))
        if not events:
            return result
        if _dt(events[-1]["observed_at"]) > _utc_now():
            raise ValueError("paper_validation_clock_rollback")
        result["updated_at"] = events[-1]["observed_at"]
        ended = {event["data"]["cohort_id"]: event for event in events if event["kind"] == "cohort_ended"}
        summaries = {}
        for event in events:
            if event["kind"] == "assessment":
                summaries[event["data"]["cohort_id"]] = {**event["data"], "assessed_at": event["observed_at"]}
        for key, row in summaries.items():
            row["active"] = key not in ended
            if not row["active"]:
                row["ended_at"] = ended[key]["observed_at"]
                row["end_reason"] = ended[key]["data"]["reason"]
        active = [row for row in summaries.values() if row["active"]]
        if output_dir is None and active:
            try:
                protocol = _protocol()
                settings = _yaml(ROOT / "config" / "settings.yaml")
                risk = _yaml(ROOT / "config" / "risk_params.yaml")
                baskets = _yaml(ROOT / "config" / "baskets.yaml").get("baskets", {})
                for row in active:
                    name = row["basket"]
                    changed = name not in protocol["baskets"] or baskets.get(name, {}).get("enabled") is not True
                    if not changed:
                        config_hash, code_hash, _, _ = _operating_material(baskets[name], settings, risk)
                        own_protocol = _cohort_protocol(protocol, name)
                        current = {"config": config_hash, "code": code_hash, "protocol": _hash(own_protocol)}
                        changed = any(row["fingerprint"].get(key) != value for key, value in current.items())
                    if changed:
                        row["status"] = "stale"
                        row["issues"] = sorted(set(row["issues"]) | {"policy_changed_pending_collection"})
                        result["issues"].append("policy_changed_pending_collection:" + name)
            except (OSError, ValueError, TypeError, KeyError):
                result["issues"].append("current_policy_unavailable")
                for row in active:
                    row["status"] = "stale"
                    row["issues"] = sorted(set(row["issues"]) | {"current_policy_unavailable"})
        last_run = next((event for event in reversed(events) if event["kind"] in {"collection_completed", "collection_failed"}), None)
        if last_run and last_run["kind"] == "collection_failed":
            result["issues"].append("latest_collection_failed:" + last_run["data"]["error"])
        try:
            holidays = _calendar()
            for row in active:
                expected = _expected(_utc_now(), holidays, row.get("market_close", "15:30"))
                if row["coverage"]["expected_through"] < expected:
                    row["status"] = "stale"
                    row["issues"] = sorted(set(row["issues"]) | {"assessment_stale"})
                    result["issues"].append("assessment_stale:" + row["basket"])
        except (OSError, ValueError, TypeError, KeyError):
            result["issues"].append("trading_calendar_unavailable")
            for row in active:
                row["status"] = "stale"
                row["issues"] = sorted(set(row["issues"]) | {"trading_calendar_unavailable"})
        if last_run is None:
            result["issues"].append("collection_stale")
            for row in active:
                row["status"] = "stale"
                row["issues"] = sorted(set(row["issues"]) | {"collection_stale"})
        result["cohorts"] = sorted(summaries.values(), key=lambda row: (not row["active"], row["basket"], row["started_at"]), reverse=False)
        result["status"] = ("unavailable" if result["issues"] else "data_gaps" if any(row["status"] in {"data_gaps", "stale"} for row in active)
                            else "observing" if active else "inactive")
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(status="corrupt", issues=["journal_read_error:" + type(exc).__name__])
        return result

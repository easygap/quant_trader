"""Public disclosures and investor-flow observations with immutable evidence.

DART receipt dates do not contain publication times. NAVER exposes recent daily
net quantities, not monetary flow or a historical point-in-time database. Every
record therefore retains its actual first observation separately from source dates.
No database, portfolio, execution, broker, or notification module is imported.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

import requests
import yaml

from core.market_research import ROOT, _calendar, _now, _sessions, expected_session

SCHEMA_VERSION = 1
DART_GUIDE = "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001"
NAVER_URL = "https://m.stock.naver.com/api/stock/{symbol}/integration"


def intelligence_directory() -> Path:
    return Path(os.environ.get("QUANT_MARKET_INTELLIGENCE_DIR", ROOT / "output" / "market_intelligence"))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


_RECORD_METADATA = {"kind", "record_id", "revision_id", "observed_at", "first_observed_at", "available_to_system_at", "previous_revision_id", "version_path"}


def _record_digest(record):
    return _digest({key: value for key, value in record.items() if key not in _RECORD_METADATA})


def _timestamp(value):
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError("timezone-aware timestamp required")
    return _now(value)


def load_intelligence_config(path=None) -> dict:
    from config.config_loader import load_yaml
    config = load_yaml(str(path or ROOT / "config" / "market_intelligence.yaml"))
    _validate(config)
    return config


def _validate(config):
    universe = config.get("universe")
    if not isinstance(universe, list) or not 1 <= len(universe) <= 30:
        raise ValueError("observation universe must contain 1 to 30 equities")
    codes = [row.get("symbol") for row in universe if isinstance(row, dict)]
    if len(codes) != len(universe) or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes) or len(set(codes)) != len(codes):
        raise ValueError("unique six-digit equity codes are required")
    for key, low, high in (("request_timeout_seconds", 1, 30), ("collection_budget_seconds", 5, 150), ("workers", 1, 4)):
        value = config.get(key, {"request_timeout_seconds": 12, "collection_budget_seconds": 120, "workers": 3}[key])
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"invalid {key}")
    for section, key, low, high, default in (("disclosures", "lookback_calendar_days", 1, 90, 14), ("disclosures", "max_pages_per_company", 1, 10, 4), ("investor_flows", "lookback_sessions", 1, 5, 4)):
        settings = config.get(section) or {}
        if not isinstance(settings.get("enabled", True), bool):
            raise ValueError(f"invalid {section}.enabled")
        value = settings.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"invalid {section}.{key}")
    if (config.get("investor_flows") or {}).get("provider", "naver_securities") != "naver_securities":
        raise ValueError("unsupported investor-flow provider")
    if any(word in json.dumps(config).lower() for word in ('"api_key"', '"crtfc_key"', '"password"', '"secret"')):
        raise ValueError("credentials must be environment settings, not observation config")


class SourceError(RuntimeError):
    """Safe provider reason; never includes request URLs or credential values."""


class PublicTransport:
    def get_bytes(self, url, *, params=None, timeout=12):
        try:
            response = requests.get(url, params=params, timeout=timeout, allow_redirects=False,
                                    headers={"User-Agent": "quant-trader-research/1.0", "Accept": "application/json,application/zip"})
            if response.status_code != 200:
                raise SourceError(f"http_{response.status_code}")
            if len(response.content) > 15_000_000:
                raise SourceError("response_too_large")
            return response.content
        except SourceError:
            raise
        except requests.RequestException as exc:
            raise SourceError(type(exc).__name__) from None

    def get_json(self, url, *, params=None, timeout=12):
        try:
            return json.loads(self.get_bytes(url, params=params, timeout=timeout))
        except (ValueError, UnicodeError):
            raise SourceError("invalid_json") from None


def _guard(deadline):
    if time.monotonic() >= deadline:
        raise SourceError("collection_budget_exhausted")


def _corp_codes(transport, key, timeout, deadline):
    _guard(deadline)
    content = transport.get_bytes("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=timeout)
    try:
        if not zipfile.is_zipfile(BytesIO(content)):
            # Authentication/rate failures are XML even though successful output
            # is a ZIP. Retain only the official status code, never free text.
            response = ET.fromstring(content)
            code = response.findtext("status", "")
            if re.fullmatch(r"\d{3}", code) and code != "000":
                raise SourceError(f"dart_status_{code}")
            raise SourceError("invalid_corporation_archive")
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".xml")]
            if len(names) != 1 or names[0].file_size > 50_000_000:
                raise SourceError("invalid_corporation_archive")
            root = ET.fromstring(archive.read(names[0]))
        mapping = {}
        for row in root.findall("list"):
            symbol, corp = row.findtext("stock_code", "").strip(), row.findtext("corp_code", "").strip()
            if re.fullmatch(r"\d{6}", symbol) and re.fullmatch(r"\d{8}", corp):
                mapping[symbol] = corp
        if not mapping:
            raise SourceError("corporation_mapping_empty")
        return mapping, hashlib.sha256(content).hexdigest()
    except (zipfile.BadZipFile, ET.ParseError, OSError, KeyError):
        raise SourceError("invalid_corporation_archive") from None


def _dated(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _record(kind, key, payload, observed_at):
    return {"kind": kind, "record_id": key, "revision_id": _digest(payload), "observed_at": observed_at,
            "first_observed_at": observed_at, "available_to_system_at": observed_at, **payload}


def collect_disclosures(spec, *, corp_code, key, transport, start, end, timeout, deadline, max_pages):
    symbol = spec["symbol"]
    records, seen, issues = [], set(), []
    status, total_pages, requested_pages, reported_count = "ready", 1, 0, None
    try:
        if not corp_code:
            raise SourceError("corp_code_missing")
        for page in range(1, max_pages + 1):
            _guard(deadline)
            data = transport.get_json("https://opendart.fss.or.kr/api/list.json", timeout=timeout,
                                      params={"crtfc_key": key, "corp_code": corp_code, "bgn_de": start.replace("-", ""),
                                              "end_de": end.replace("-", ""), "last_reprt_at": "N", "page_no": str(page), "page_count": "100"})
            observed_at = _now().isoformat()
            requested_pages += 1
            if not isinstance(data, dict):
                raise SourceError("invalid_disclosure_schema")
            code = str(data.get("status", ""))
            if code == "013":
                if page != 1:
                    raise SourceError("unexpected_empty_page")
                break  # Official no-data response is distinct from missing/failed response.
            if code != "000":
                raise SourceError(f"dart_status_{code}" if re.fullmatch(r"\d{3}", code) else "dart_status_unknown")
            rows = data.get("list")
            if not isinstance(rows, list):
                raise SourceError("invalid_disclosure_schema")
            try:
                new_total_pages = int(data["total_page"])
                new_count = int(data["total_count"])
            except (ValueError, TypeError, KeyError):
                raise SourceError("invalid_pagination") from None
            if new_total_pages < 1 or new_count < 1 or not rows:
                raise SourceError("invalid_pagination")
            if reported_count is not None and (new_count != reported_count or new_total_pages != total_pages):
                issues.append("pagination_changed_during_collection")
            total_pages, reported_count = new_total_pages, new_count
            for raw in rows:
                if not isinstance(raw, dict):
                    issues.append("invalid_disclosure_row")
                    continue
                receipt, published = str(raw.get("rcept_no", "")), _dated(str(raw.get("rcept_dt", "")))
                if not re.fullmatch(r"\d{14}", receipt) or not published or not start <= published <= end or raw.get("corp_code") != corp_code or raw.get("stock_code") != symbol or not isinstance(raw.get("report_nm"), str) or not raw["report_nm"].strip():
                    issues.append("invalid_disclosure_row")
                    continue
                if receipt in seen:
                    continue
                seen.add(receipt)
                title = str(raw.get("report_nm", ""))
                raw_fields = {field: raw.get(field) for field in ("corp_code", "corp_name", "stock_code", "report_nm", "rcept_no", "rcept_dt", "flr_nm", "rm", "corp_cls")}
                payload = {"symbol": symbol, "name": spec.get("name", symbol), "provider": "OpenDART", "title": title,
                           "published_date": published, "published_at": None, "published_precision": "date_only",
                           "is_correction": "정정" in title, "has_later_correction": "정" in str(raw.get("rm", "")),
                           "is_withdrawal": "철" in str(raw.get("rm", "")), "corrects_record_id": None,
                           "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}", "raw_fields": raw_fields}
                records.append(_record("disclosure", f"dart:{receipt}", payload, observed_at))
            if page >= total_pages:
                break
        if requested_pages < total_pages:
            issues.append("pagination_limit_reached")
        elif reported_count is not None and len(records) != reported_count:
            issues.append("disclosure_count_mismatch")
    except SourceError as exc:
        issues.append(str(exc))
    except Exception as exc:
        issues.append(type(exc).__name__)
    if issues:
        status = "partial" if records else "unavailable"
    return {"symbol": symbol, "name": spec.get("name", symbol), "status": status,
            "confirmed_empty": status == "ready" and not records, "issues": sorted(set(issues)),
            "pages_fetched": requested_pages, "pages_reported": total_pages, "records": records}


def _quantity(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)", value.strip()):
        return int(value.strip().replace(",", ""))
    return None


def collect_investor_flows(spec, *, transport, sessions, timeout, deadline):
    symbol, records, issues = spec["symbol"], [], []
    source_url = NAVER_URL.format(symbol=symbol)
    try:
        _guard(deadline)
        data = transport.get_json(source_url, timeout=timeout)
        observed_at = _now().isoformat()
        if not isinstance(data, dict) or data.get("itemCode") != symbol or not isinstance(data.get("dealTrendInfos"), list):
            raise SourceError("invalid_flow_schema")
        seen = set()
        for raw in data["dealTrendInfos"]:
            if not isinstance(raw, dict) or raw.get("itemCode") != symbol:
                issues.append("invalid_flow_row")
                continue
            trade_date = _dated(raw.get("bizdate"))
            if not trade_date:
                issues.append("invalid_flow_date")
                continue
            if trade_date not in sessions:
                continue  # Current/provisional and older out-of-window rows are excluded.
            if trade_date in seen:
                issues.append("duplicate_flow_session")
                records = [record for record in records if record["trade_date"] != trade_date]
                continue
            seen.add(trade_date)
            quantities = {"individual_net_buy_shares": _quantity(raw.get("individualPureBuyQuant")),
                          "institution_net_buy_shares": _quantity(raw.get("organPureBuyQuant")),
                          "foreign_net_buy_shares": _quantity(raw.get("foreignerPureBuyQuant"))}
            missing = [field for field, value in quantities.items() if value is None]
            if missing:
                issues.append("missing_investor_quantities")
            payload = {"symbol": symbol, "name": spec.get("name", symbol), "provider": "NAVER Securities public web",
                       "trade_date": trade_date, "published_at": None, "published_precision": "not_provided",
                       "unit": "shares", "source_url": source_url, "source_page": f"https://stock.naver.com/domestic/stock/{symbol}/investmentinfo",
                       "market_scope": "provider_unspecified_venue_coverage", "status": "partial" if missing else "ready",
                       "missing_fields": missing, **quantities,
                       "raw_fields": {field: raw.get(field) for field in ("itemCode", "bizdate", "individualPureBuyQuant", "organPureBuyQuant", "foreignerPureBuyQuant")}}
            records.append(_record("investor_flow", f"naver:{symbol}:{trade_date}", payload, observed_at))
        missing_sessions = sorted(set(sessions) - {row["trade_date"] for row in records})
        if missing_sessions:
            issues.append("missing_flow_sessions")
    except SourceError as exc:
        issues.append(str(exc))
    except Exception as exc:
        issues.append(type(exc).__name__)
    status = "ready" if records and not issues else ("partial" if records else "unavailable")
    return {"symbol": symbol, "name": spec.get("name", symbol), "status": status, "confirmed_empty": False,
            "issues": sorted(set(issues)), "missing_sessions": sorted(set(sessions) - {row["trade_date"] for row in records}), "records": records}


def _section(provider, results):
    records = [record for result in results for record in result["records"]]
    ready = sum(result["status"] == "ready" for result in results)
    status = "ready" if ready == len(results) and results else ("partial" if records or ready else "unavailable")
    return {"provider": provider, "status": status, "records": records,
            "coverage": {"requested": len(results), "ready": ready, "failed": sum(result["status"] == "unavailable" for result in results),
                         "partial": sum(result["status"] == "partial" for result in results), "confirmed_empty": sum(result.get("confirmed_empty", False) for result in results)},
            "symbols": [{key: value for key, value in result.items() if key != "records"} for result in results]}


def collect_market_intelligence(config=None, *, transport=None, dart_api_key=None, now=None) -> dict:
    """Collect public research data within a bounded budget; never writes a DB."""
    config = config or load_intelligence_config()
    _validate(config)
    started = _now()
    evaluation = _now(now) if now is not None else started
    end = expected_session(evaluation)
    calendar = _calendar()
    count = (config.get("investor_flows") or {}).get("lookback_sessions", 4)
    sessions = [str(day.date()) for day in _sessions((evaluation.date() - timedelta(days=35)).isoformat(), end, calendar)][-count:]
    # Disclosures may include today's actually observed receipts; exact published
    # time remains unknown. Daily flow excludes current-day provisional quantities.
    disclosure_end = evaluation.date().isoformat()
    start = (evaluation.date() - timedelta(days=(config.get("disclosures") or {}).get("lookback_calendar_days", 14))).isoformat()
    transport = transport or PublicTransport()
    timeout = config.get("request_timeout_seconds", 12)
    deadline = time.monotonic() + config.get("collection_budget_seconds", 120)
    if dart_api_key is None:
        from config.config_loader import Config
        dart_api_key = (Config.get().dart or {}).get("api_key", "")
    mapping, mapping_hash, dart_issue = {}, None, None
    if not (config.get("disclosures") or {}).get("enabled", True):
        dart_issue = "collector_disabled"
    elif not dart_api_key:
        dart_issue = "dart_api_key_missing"
    else:
        try:
            mapping, mapping_hash = _corp_codes(transport, dart_api_key, timeout, deadline)
        except SourceError as exc:
            dart_issue = str(exc)
        except Exception as exc:
            dart_issue = type(exc).__name__
    results = {"disclosures": [], "investor_flows": []}
    with ThreadPoolExecutor(max_workers=config.get("workers", 3)) as pool:
        futures = {}
        for spec in config["universe"]:
            if dart_issue:
                results["disclosures"].append({"symbol": spec["symbol"], "name": spec.get("name"), "status": "unavailable", "confirmed_empty": False, "issues": [dart_issue], "records": []})
            else:
                future = pool.submit(collect_disclosures, spec, corp_code=mapping.get(spec["symbol"]), key=dart_api_key,
                                     transport=transport, start=start, end=disclosure_end, timeout=timeout, deadline=deadline,
                                     max_pages=(config.get("disclosures") or {}).get("max_pages_per_company", 4))
                futures[future] = "disclosures"
            if (config.get("investor_flows") or {}).get("enabled", True):
                future = pool.submit(collect_investor_flows, spec, transport=transport, sessions=sessions, timeout=timeout, deadline=deadline)
                futures[future] = "investor_flows"
            else:
                results["investor_flows"].append({"symbol": spec["symbol"], "name": spec.get("name"), "status": "unavailable", "issues": ["collector_disabled"], "records": []})
        for future in as_completed(futures):
            results[futures[future]].append(future.result())
    for values in results.values():
        values.sort(key=lambda row: row["symbol"])
    sections = {"disclosures": _section("OpenDART", results["disclosures"]), "investor_flows": _section("NAVER Securities public web", results["investor_flows"])}
    statuses = [section["status"] for section in sections.values()]
    return {"schema_version": SCHEMA_VERSION, "research_only": True, "replay": now is not None,
            "status": "ready" if all(status == "ready" for status in statuses) else ("unavailable" if all(status == "unavailable" for status in statuses) else "partial"),
            "collection_started_at": started.isoformat(), "generated_at": _now().isoformat(), "evaluation_at": evaluation.isoformat(),
            "expected_session": end, "config_hash": _digest(config), "corporation_mapping_hash": mapping_hash,
            "disclosure_window": {"start": start, "end": disclosure_end}, "flow_sessions": sessions, **sections,
            "sources": [{"provider": "OpenDART", "documentation_url": DART_GUIDE,
                         "endpoint": "https://opendart.fss.or.kr/api/list.json", "authentication": "configured_api_key"},
                        {"provider": "NAVER Securities public web", "endpoint_template": NAVER_URL,
                         "documentation_url": None, "authentication": "public", "contracted_api": False}],
            "limitations": ["공시 접수일은 날짜 정밀도이며 실제 공개 시각을 제공하지 않습니다. 관측 전 시점에 알았던 정보로 취급하지 않습니다.",
                            "수급은 NAVER 증권 공개 웹 자료의 순매수 수량(주)이며 거래대금·시장 전체 합계·계약된 KRX 원본 피드가 아닙니다.",
                            "거래소별 집계 범위와 공급자 공개 시각은 제공되지 않습니다. 현재일 수급은 제외합니다.",
                            "공시 정정은 별도 접수번호 또는 공급자 내용 변경으로 보존하며 원 공시와의 연결을 추정하지 않습니다.",
                            "연구 자료 수집이며 주문·전략 승격·목표 비중을 변경하지 않습니다."]}


def _atomic_json(path, value, *, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".intelligence-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # Hard-link creation is atomic and refuses an existing name. Unlike
            # replace(), this cannot overwrite evidence if writers overlap.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_market_intelligence(report, output_dir=None) -> dict:
    """Persist immutable evidence; caller should serialize latest-report writers."""
    directory = Path(output_dir) if output_dir else intelligence_directory()
    # Work on a copy so a caller cannot mistake saved first-observation metadata for
    # an in-memory collection time. The returned paths locate the canonical report.
    report = json.loads(json.dumps(report, allow_nan=False))
    previous = read_market_intelligence(directory / "latest.json")
    prior = {row["record_id"]: row for section in ("disclosures", "investor_flows") for row in (previous.get(section) or {}).get("records", [])}
    wanted = {row["record_id"] for section in ("disclosures", "investor_flows") for row in report[section]["records"]} - set(prior)
    if wanted:
        priorities = {}
        # Versions retain their FIRST observation. To recover the most recent
        # state after a failed attempt, use collection history: A->B->A->failure
        # must link the next revision to A, not to B's newer first observation.
        for path in (directory / "snapshots").glob("*.json"):
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if path.name != f"{snapshot['expected_session']}_{_digest(snapshot)[:24]}.json":
                raise ValueError("immutable snapshot identity mismatch")
            for section in ("disclosures", "investor_flows"):
                for candidate in snapshot[section]["records"]:
                    identity = candidate["record_id"]
                    if identity not in wanted:
                        continue
                    if candidate["revision_id"] != _record_digest(candidate):
                        raise ValueError("immutable evidence identity mismatch")
                    priority = (_timestamp(candidate["observed_at"]), _timestamp(snapshot["generated_at"]))
                    if priority == priorities.get(identity) and candidate["revision_id"] != prior[identity]["revision_id"]:
                        raise ValueError("ambiguous source revision chronology")
                    if identity not in priorities or priority > priorities[identity]:
                        prior[identity], priorities[identity] = candidate, priority
    for section in ("disclosures", "investor_flows"):
        for row in report[section]["records"]:
            identity = hashlib.sha256(row["record_id"].encode()).hexdigest()[:24]
            version = directory / "versions" / f"{identity}_{row['revision_id']}.json"
            if version.exists():
                stored = json.loads(version.read_text(encoding="utf-8"))
                if stored.get("record_id") != row["record_id"] or stored.get("revision_id") != row["revision_id"] or _record_digest(stored) != row["revision_id"]:
                    raise ValueError("immutable evidence identity mismatch")
                row["first_observed_at"] = stored["first_observed_at"]
            row["available_to_system_at"] = row["first_observed_at"]
            old = prior.get(row["record_id"])
            row["previous_revision_id"] = old.get("revision_id") if old and old.get("revision_id") != row["revision_id"] else None
            row["version_path"] = f"versions/{version.name}"
            if not version.exists():
                try:
                    _atomic_json(version, row, immutable=True)
                except FileExistsError:
                    # A concurrent writer won creation. Refuse promotion; the
                    # next serialized collection will reconcile first observation.
                    raise ValueError("concurrent evidence writer; retry collection") from None
    snapshot_id = _digest(report)
    snapshot = directory / "snapshots" / f"{report['expected_session']}_{snapshot_id[:24]}.json"
    if not snapshot.exists():
        try:
            _atomic_json(snapshot, report, immutable=True)
        except FileExistsError:
            pass  # Identical content-addressed snapshot already exists.
    _atomic_json(directory / "latest.json", report)
    return {"latest": str(directory / "latest.json"), "snapshot": str(snapshot), "snapshot_id": snapshot_id}


def read_market_intelligence(path=None, now=None) -> dict:
    """Read-only dashboard accessor with deadline/config freshness checks."""
    target = Path(path) if path else intelligence_directory() / "latest.json"
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION or report.get("research_only") is not True:
            raise ValueError("invalid intelligence schema")
        statuses = {"ready", "partial", "unavailable"}
        if report.get("status") not in statuses or type(report.get("replay")) is not bool:
            raise ValueError("invalid report status")
        if not re.fullmatch(r"[0-9a-f]{64}", report.get("config_hash", "")):
            raise ValueError("invalid config provenance")
        for section in ("disclosures", "investor_flows"):
            if not isinstance(report.get(section), dict) or not isinstance(report[section].get("records"), list) or report[section].get("status") not in statuses or len(report[section]["records"]) > 30000:
                raise ValueError("invalid section schema")
        if report["status"] == "ready" and any(report[section]["status"] != "ready" for section in ("disclosures", "investor_flows")):
            raise ValueError("inconsistent readiness")
        generated = _timestamp(report["generated_at"])
        started = _timestamp(report["collection_started_at"])
        evaluation = _timestamp(report["evaluation_at"])
        current = _now(now)
        if generated > current or started > generated or (not report["replay"] and evaluation != started):
            raise ValueError("future generation timestamp")
        for section in ("disclosures", "investor_flows"):
            seen = set()
            for row in report[section]["records"]:
                if not isinstance(row, dict) or not started <= _timestamp(row["observed_at"]) <= generated or _timestamp(row["first_observed_at"]) > _timestamp(row["observed_at"]) or row["available_to_system_at"] != row["first_observed_at"]:
                    raise ValueError("invalid observation provenance")
                if row["record_id"] in seen or row["revision_id"] != _record_digest(row):
                    raise ValueError("invalid record version")
                seen.add(row["record_id"])
        report["read_expected_session"] = expected_session(current)
        report["config_changed"] = path is None and report.get("config_hash") != _digest(load_intelligence_config())
        # A verified completed session does not expire merely because a weekend
        # or exchange holiday lasts longer than 36 hours. Source failures retain
        # their own partial/unavailable status; collection heartbeat is separate.
        report["is_stale"] = report["expected_session"] != report["read_expected_session"] or report["config_changed"]
        if report["is_stale"] and report.get("status") != "unavailable":
            report["status"] = "stale"
        return report
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        return {"schema_version": SCHEMA_VERSION, "research_only": True, "status": "unavailable", "reason": "intelligence_snapshot_unavailable",
                "disclosures": {"status": "unavailable", "records": []}, "investor_flows": {"status": "unavailable", "records": []}}

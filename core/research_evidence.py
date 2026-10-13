"""Curator-authored research hypotheses, separate from trading decisions.

Imports append immutable revisions and stamp observation time on this machine.
Published time is a curator-supplied claim, not a verified news-feed timestamp.
Readers never fetch URLs, create directories, or modify a trading database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parent.parent
_MAX_BYTES = 2_000_000
_TYPES = {"industry_thesis", "macro_thesis", "company_thesis", "scheduled_event", "methodology"}
_ENTRY_FIELDS = {
    "id", "title", "theme", "thesis", "invalidation_condition", "horizon",
    "review_at", "expires_at", "event_at", "evidence_type", "sources", "symbols",
    "counterevidence",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any, field: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone is required")
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: use an ISO timestamp with a timezone") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field}: non-empty text up to {maximum} characters is required")
    return value.strip()


def _url(value: Any) -> str:
    result = _text(value, "source URL", 2000)
    try:
        parsed = urlsplit(result)
        valid = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
        valid = valid and not any(character.isspace() or ord(character) < 32 for character in result)
        if not valid:
            raise ValueError
    except ValueError as exc:
        raise ValueError("source URL: an http(s) URL without credentials is required") from exc
    return result


def _unknown_fields(value: dict, allowed: set[str], field: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"{field}: unsupported fields {sorted(extra)}")


def _entry(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("entry must be an object")
    _unknown_fields(value, _ENTRY_FIELDS, "entry")
    result = {key: _text(value.get(key), key) for key in (
        "id", "title", "theme", "thesis", "invalidation_condition", "horizon", "evidence_type",
    )}
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", result["id"]):
        raise ValueError("id: use 1-80 lowercase letters, digits, hyphens or underscores")
    if result["evidence_type"] not in _TYPES:
        raise ValueError(f"evidence_type must be one of {sorted(_TYPES)}")
    for key in ("review_at", "expires_at", "event_at"):
        result[key] = _iso(_time(value[key], key)) if value.get(key) else None
    if result["review_at"] is None or result["expires_at"] is None:
        raise ValueError("review_at and expires_at are required")
    if _time(result["review_at"], "review_at") > _time(result["expires_at"], "expires_at"):
        raise ValueError("review_at must not be after expires_at")
    if result["evidence_type"] == "scheduled_event" and not result["event_at"]:
        raise ValueError("scheduled_event requires event_at")
    sources = value.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
        raise ValueError("sources must contain 1-20 source objects")
    result["sources"] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        _unknown_fields(source, {"url", "title", "published_at", "published_date", "publication_note"}, "source")
        published_date = source.get("published_date")
        if published_date is not None:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_date):
                    raise ValueError
                datetime.strptime(published_date, "%Y-%m-%d")
            except (TypeError, ValueError) as exc:
                raise ValueError("published_date must be a valid YYYY-MM-DD date") from exc
        result["sources"].append({
            "url": _url(source.get("url")), "title": _text(source.get("title"), "source title", 500),
            "published_at": _iso(_time(source["published_at"], "published_at")) if source.get("published_at") else None,
            "published_date": published_date,
            "publication_note": _text(source["publication_note"], "publication_note", 500) if source.get("publication_note") else None,
        })
    symbols = value.get("symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 100:
        raise ValueError("symbols must be a list of at most 100 associations")
    result["symbols"] = []
    seen_symbols = set()
    urls = {source["url"] for source in result["sources"]}
    for association in symbols:
        if not isinstance(association, dict):
            raise ValueError("symbol association must be an object")
        _unknown_fields(association, {"symbol", "rationale", "source_url"}, "association")
        symbol = _text(association.get("symbol"), "symbol", 20)
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.:-]{0,19}", symbol) or symbol in seen_symbols:
            raise ValueError("symbol must be unique and contain only uppercase letters, digits, . : -")
        source_url = _url(association.get("source_url"))
        if source_url not in urls:
            raise ValueError("each symbol association must cite one of this entry's source URLs")
        seen_symbols.add(symbol)
        result["symbols"].append({"symbol": symbol, "rationale": _text(association.get("rationale"), "rationale"), "source_url": source_url})
    counterevidence = value.get("counterevidence", [])
    if not isinstance(counterevidence, list) or len(counterevidence) > 30:
        raise ValueError("counterevidence must be a list of at most 30 statements")
    result["counterevidence"] = [_text(item, "counterevidence") for item in counterevidence]
    return result


def validate_research_input(payload: Any) -> dict:
    """Validate curator input; observation timestamps/revisions cannot be supplied."""
    if not isinstance(payload, dict):
        raise ValueError("research input must be an object")
    _unknown_fields(payload, {"schema_version", "curator", "entries"}, "research input")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    curator = _text(payload.get("curator"), "curator", 200)
    entries = payload.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("entries must contain 1-100 hypotheses")
    normalized = [_entry(entry) for entry in entries]
    if len({entry["id"] for entry in normalized}) != len(normalized):
        raise ValueError("entry IDs must be unique within one import")
    return {"schema_version": 1, "curator": curator, "entries": normalized}


def evidence_journal_dir(journal_dir: str | os.PathLike | None = None) -> Path:
    return Path(journal_dir or os.environ.get("QUANT_RESEARCH_EVIDENCE_DIR") or _ROOT / "data" / "research_evidence").resolve()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_json(path: Path) -> Any:
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("JSON file exceeds 2 MB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _history(directory: Path) -> tuple[list[dict], list[str]]:
    records, issues = [], []
    if not directory.exists():
        return records, issues
    if not directory.is_dir():
        return records, ["journal path is not a directory"]
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink() or path.resolve().parent != directory:
                raise ValueError("journal symlinks are not supported")
            record = _read_json(path)
            digest = record.pop("sha256")
            if hashlib.sha256(_canonical(record)).hexdigest() != digest:
                raise ValueError("snapshot checksum mismatch")
            _unknown_fields(record, {"schema_version", "observed_at", "curator", "entries"}, "snapshot")
            observed_at = _iso(_time(record["observed_at"], "observed_at"))
            raw_entries = record["entries"]
            versions = [entry["version"] for entry in raw_entries]
            if any(type(version) is not int or version < 1 for version in versions):
                raise ValueError("invalid revision number")
            validated = validate_research_input({
                "schema_version": record["schema_version"], "curator": record["curator"],
                "entries": [{key: value for key, value in entry.items() if key != "version"} for entry in raw_entries],
            })
            for entry, version in zip(validated["entries"], versions):
                records.append({**entry, "version": version, "observed_at": observed_at, "curator": validated["curator"]})
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            issues.append(f"{path.name}: {exc}")
    records.sort(key=lambda record: (record["observed_at"], record["version"], record["id"]))
    latest_versions: dict[str, int] = {}
    valid_records = []
    for record in records:
        expected = latest_versions.get(record["id"], 0) + 1
        if record["version"] != expected:
            issues.append(f"{record['id']}: revision sequence is incomplete or duplicated")
            continue
        latest_versions[record["id"]] = record["version"]
        valid_records.append(record)
    return valid_records, issues


def import_research_evidence(source_path: str | os.PathLike, journal_dir: str | os.PathLike | None = None) -> dict:
    """Append one versioned snapshot. Never overwrite input or existing revisions.

    The exclusive import lock prevents duplicate revisions; a leftover lock is an
    explicit error, not silently stolen. JSON is read from disk, never fetched.
    """
    source = Path(source_path).resolve(strict=True)
    directory = evidence_journal_dir(journal_dir)
    if source.is_relative_to(directory):
        raise ValueError("curated source must be outside the journal directory")
    payload = validate_research_input(_read_json(source))
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".import.lock"
    try:
        lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("another import is active or .import.lock needs inspection") from exc
    temporary = None
    try:
        os.close(lock)
        history, issues = _history(directory)
        if issues:
            raise ValueError("journal integrity error: " + "; ".join(issues))
        observed = _utc_now()
        if history and observed <= max(_time(entry["observed_at"], "observed_at") for entry in history):
            raise ValueError("server clock has not advanced beyond the last observation")
        for entry in payload["entries"]:
            for source_item in entry["sources"]:
                if source_item["published_at"] and _time(source_item["published_at"], "published_at") > observed:
                    raise ValueError("published_at cannot be after the observation; use event_at for a future scheduled event")
        versions = {entry["id"]: entry["version"] for entry in history}
        entries = [{**entry, "version": versions.get(entry["id"], 0) + 1} for entry in payload["entries"]]
        record = {"schema_version": 1, "observed_at": _iso(observed), "curator": payload["curator"], "entries": entries}
        digest = hashlib.sha256(_canonical(record)).hexdigest()
        encoded = json.dumps({**record, "sha256": digest}, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("normalized snapshot exceeds 2 MB")
        token = uuid.uuid4().hex
        target = directory / f"{observed.strftime('%Y%m%dT%H%M%S%fZ')}_{token}.json"
        temporary = directory / f".pending-{token}"
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and fails if the target already exists.
        os.link(temporary, target)
        return {"path": str(target), "observed_at": record["observed_at"], "imported": len(entries), "versions": {entry["id"]: entry["version"] for entry in entries}}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def read_evidence_journal(
    journal_dir: str | os.PathLike | None = None, *, as_of: str | datetime | None = None,
    symbols: list[str] | None = None, limit: int = 50,
) -> dict:
    """Latest observed revision per hypothesis as of an aware timestamp.

    Future-dated publications never become usable evidence early. Unknown source
    dates are explicitly unverified, including examples supplied with the app.
    CURRENT means review is not overdue, not that a thesis is proven or tradable.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    point = _time(as_of, "as_of") if as_of is not None else _utc_now()
    history, issues = _history(evidence_journal_dir(journal_dir))
    latest = {}
    for record in history:
        if _time(record["observed_at"], "observed_at") <= point:
            latest[record["id"]] = record
    entries = []
    for record in latest.values():
        if symbols is not None and not set(symbols).intersection(item["symbol"] for item in record["symbols"]):
            continue
        notes = []
        sources = []
        for source in record["sources"]:
            published = _time(source["published_at"], "published_at") if source["published_at"] else None
            # A future publication claimed in an old/tampered record cannot turn
            # into observed evidence merely because the wall clock has advanced.
            status = "UNKNOWN_PUBLICATION" if published is None else "NOT_YET_PUBLISHED" if published > point else "PUBLICATION_AFTER_OBSERVATION" if published > _time(record["observed_at"], "observed_at") else "AVAILABLE"
            if status != "AVAILABLE":
                notes.append(status)
            sources.append({**source, "availability": status})
        if not record["counterevidence"]:
            notes.append("COUNTEREVIDENCE_NOT_RECORDED")
        if _time(record["expires_at"], "expires_at") <= point:
            notes.append("EXPIRED")
        elif _time(record["review_at"], "review_at") <= point:
            notes.append("REVIEW_DUE")
        if "EXPIRED" in notes or "REVIEW_DUE" in notes:
            status = "STALE"
        elif any(source["availability"] != "AVAILABLE" for source in sources):
            status = "UNVERIFIED"
        elif record["event_at"] and _time(record["event_at"], "event_at") > point:
            status = "UPCOMING"
        else:
            status = "CURRENT"
        event_status = None
        if record["event_at"]:
            event_status = "UPCOMING" if _time(record["event_at"], "event_at") > point else "PASSED"
        entries.append({**record, "sources": sources, "status": status, "event_status": event_status, "issues": list(dict.fromkeys(notes)), "trade_eligible": False})
    entries.sort(key=lambda entry: (entry["observed_at"], entry["id"]), reverse=True)
    counts = dict(Counter(entry["status"] for entry in entries))
    attention = bool(issues or counts.get("STALE") or counts.get("UNVERIFIED"))
    return {
        "schema_version": 1, "source": "curated_research", "automatic_collection": False,
        "publication_verification": "curator_supplied", "as_of": _iso(point),
        "status": "ATTENTION" if attention else "OK" if entries else "EMPTY",
        "entries": entries[:limit], "total": len(entries), "counts": counts,
        "truncated": len(entries) > limit, "issues": issues,
    }


get_research_evidence = read_evidence_journal

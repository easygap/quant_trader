"""Research history must describe what was known, not backfilled hindsight."""

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core import research_evidence as evidence

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def setup_journal(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 13, 2, tzinfo=timezone.utc)]
    monkeypatch.setattr(evidence, "_utc_now", lambda: clock[0])
    payload = {
        "schema_version": 1, "curator": "test curator",
        "entries": [{
            "id": "sample_thesis", "title": "산업 가설", "theme": "산업 변화",
            "thesis": "검증할 가설", "invalidation_condition": "실적에서 매출 연결이 확인되지 않음",
            "horizon": "6개월", "review_at": "2026-11-13T09:00:00+09:00",
            "expires_at": "2026-12-13T09:00:00+09:00", "evidence_type": "industry_thesis",
            "sources": [{"url": "https://example.org/report", "title": "공식 보고서", "published_at": "2026-10-01T12:00:00+09:00"}],
            "symbols": [{"symbol": "005930", "rationale": "보고서에서 자사 사업을 설명", "source_url": "https://example.org/report"}],
            "counterevidence": ["증설로 현금흐름이 악화할 수 있음"],
        }],
    }
    source = tmp_path / "input.json"
    directory = tmp_path / "journal"

    def save():
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    save()
    return payload, source, directory, clock, save


def test_read_missing_journal_has_no_side_effect(tmp_path):
    directory = tmp_path / "missing"
    result = evidence.read_evidence_journal(directory)
    assert result["status"] == "EMPTY"
    assert result["automatic_collection"] is False
    assert not directory.exists()


def test_import_preserves_source_and_prior_revision_and_filters_hindsight(setup_journal):
    payload, source, directory, clock, save = setup_journal
    original_source = source.read_bytes()
    first = evidence.import_research_evidence(source, directory)
    first_path = Path(first["path"])
    original_record = first_path.read_bytes()
    assert source.read_bytes() == original_source
    assert evidence.read_evidence_journal(directory, as_of="2026-10-13T01:59:59Z")["entries"] == []
    clock[0] = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
    payload["entries"][0]["thesis"] = "수정한 가설"
    save()
    second = evidence.import_research_evidence(source, directory)
    assert second["versions"] == {"sample_thesis": 2}
    assert first_path.read_bytes() == original_record
    historic = evidence.read_evidence_journal(directory, as_of="2026-10-13T03:00:00Z")
    assert historic["entries"][0]["thesis"] == "검증할 가설"
    current = evidence.read_evidence_journal(directory)
    assert current["entries"][0]["thesis"] == "수정한 가설"
    assert current["entries"][0]["trade_eligible"] is False
    assert current["publication_verification"] == "curator_supplied"


@pytest.mark.parametrize("place,key,value", [
    ("root", "observed_at", "2020-01-01T00:00:00Z"),
    ("entry", "observed_at", "2020-01-01T00:00:00Z"),
    ("entry", "version", 7),
    ("entry", "id", "../../unsafe"),
    ("entry", "review_at", "2026-10-30"),
    ("entry", "expires_at", "2026-10-02T00:00:00Z"),
])
def test_invalid_batch_cannot_write_or_backdate(setup_journal, place, key, value):
    payload, source, directory, _, save = setup_journal
    (payload if place == "root" else payload["entries"][0])[key] = value
    save()
    with pytest.raises(ValueError):
        evidence.import_research_evidence(source, directory)
    assert not directory.exists()


def test_clock_regression_is_rejected(setup_journal):
    _, source, directory, clock, _ = setup_journal
    evidence.import_research_evidence(source, directory)
    clock[0] = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="clock"):
        evidence.import_research_evidence(source, directory)
    assert len(list(directory.glob("*.json"))) == 1
    assert not (directory / ".import.lock").exists()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/a", "https://user:secret@example.org", "https://example.org/a\nb"])
def test_source_urls_cannot_be_code_or_credentials(setup_journal, url):
    payload, _, _, _, _ = setup_journal
    payload["entries"][0]["sources"][0]["url"] = url
    with pytest.raises(ValueError, match="URL"):
        evidence.validate_research_input(payload)


def test_symbol_requires_specific_cited_relationship(setup_journal):
    payload, _, _, _, _ = setup_journal
    payload["entries"][0]["symbols"][0]["source_url"] = "https://other.example/report"
    with pytest.raises(ValueError, match="cite"):
        evidence.validate_research_input(payload)
    payload["entries"][0]["symbols"][0]["source_url"] = "https://example.org/report"
    payload["entries"][0]["symbols"][0]["rationale"] = ""
    with pytest.raises(ValueError, match="rationale"):
        evidence.validate_research_input(payload)


def test_unknown_publication_is_not_usable_evidence(setup_journal):
    payload, source, directory, _, save = setup_journal
    entry = payload["entries"][0]
    entry["sources"][0]["published_at"] = None
    entry["sources"][0]["published_date"] = "2026-10-01"
    save()
    evidence.import_research_evidence(source, directory)
    result = evidence.read_evidence_journal(directory)
    assert result["status"] == "ATTENTION"
    assert result["entries"][0]["status"] == "UNVERIFIED"
    assert result["entries"][0]["sources"][0]["availability"] == "UNKNOWN_PUBLICATION"


def test_future_publication_cannot_become_evidence_without_new_observation(setup_journal):
    payload, source, directory, _, save = setup_journal
    payload["entries"][0]["sources"][0]["published_at"] = "2026-10-20T00:00:00Z"
    save()
    with pytest.raises(ValueError, match="event_at"):
        evidence.import_research_evidence(source, directory)
    assert not list(directory.glob("*.json"))


def test_review_expiry_and_event_are_independent(setup_journal):
    payload, source, directory, clock, save = setup_journal
    payload["entries"][0]["event_at"] = "2026-10-29T10:00:00+09:00"
    payload["entries"][0]["evidence_type"] = "scheduled_event"
    save()
    evidence.import_research_evidence(source, directory)
    current = evidence.read_evidence_journal(directory)["entries"][0]
    assert current["status"] == current["event_status"] == "UPCOMING"
    clock[0] = datetime(2026, 11, 13, 1, tzinfo=timezone.utc)
    stale = evidence.read_evidence_journal(directory)["entries"][0]
    assert stale["status"] == "STALE"
    assert stale["event_status"] == "PASSED"
    assert "REVIEW_DUE" in stale["issues"]
    clock[0] = datetime(2026, 12, 13, 1, tzinfo=timezone.utc)
    assert "EXPIRED" in evidence.read_evidence_journal(directory)["entries"][0]["issues"]


def test_corrupt_history_is_reported_and_blocks_new_import(setup_journal):
    _, source, directory, clock, _ = setup_journal
    imported = evidence.import_research_evidence(source, directory)
    path = Path(imported["path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["observed_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = evidence.read_evidence_journal(directory)
    assert result["status"] == "ATTENTION" and result["issues"]
    assert result["entries"] == []
    clock[0] = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="integrity"):
        evidence.import_research_evidence(source, directory)


def test_filter_and_count_before_limit(setup_journal):
    payload, source, directory, _, save = setup_journal
    second = copy.deepcopy(payload["entries"][0])
    second["id"] = "second"
    second["symbols"] = []
    payload["entries"].append(second)
    save()
    evidence.import_research_evidence(source, directory)
    result = evidence.read_evidence_journal(directory, limit=1)
    assert result["total"] == 2 and result["truncated"] is True
    assert result["counts"] == {"CURRENT": 2}
    filtered = evidence.read_evidence_journal(directory, symbols=["005930"])
    assert filtered["total"] == 1
    assert filtered["entries"][0]["id"] == "sample_thesis"


def test_examples_have_no_fabricated_precise_publication_timestamps():
    payload = json.loads((_ROOT / "config/research_theses.json").read_text(encoding="utf-8"))
    normalized = evidence.validate_research_input(payload)
    assert len(normalized["entries"]) >= 2
    for entry in normalized["entries"]:
        assert entry["counterevidence"]
        assert all(source["published_at"] is None for source in entry["sources"])
    industry = next(entry for entry in normalized["entries"] if entry["evidence_type"] == "industry_thesis")
    assert industry["symbols"] == []


def test_cli_real_import_and_read_without_database(tmp_path):
    directory = tmp_path / "journal"
    command = [sys.executable, str(_ROOT / "tools/research_evidence.py"), "--journal-dir", str(directory)]
    original = (_ROOT / "config/research_theses.json").read_bytes()
    result = subprocess.run(command + ["import", str(_ROOT / "config/research_theses.json")], capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout)["imported"] == 2
    result = subprocess.run(command + ["list", "--json"], capture_output=True, text=True, encoding="utf-8", check=True)
    viewed = json.loads(result.stdout)
    assert viewed["total"] == 2
    assert viewed["automatic_collection"] is False
    assert (_ROOT / "config/research_theses.json").read_bytes() == original
    assert not list(tmp_path.rglob("*.db"))

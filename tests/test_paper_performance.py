"""Account history is derived from saved evidence, never a new forward sample."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from core import paper_performance as history
from core import paper_validation as pv


HOLIDAYS = {"2026-01-01"}


class Evidence:
    """Small journal-shaped fixture without database or network access."""

    def __init__(self):
        self.events = []
        self.summaries = {}
        self.cohorts = []
        self.rows = []

    def version(self, day, *, source="ledger-a", symbol="KS11"):
        key = f"version-{len(self.cohorts) + 1}"
        cohort = {
            "cohort_id": key, "basket": "test", "account_key": "basket_rebalance:test",
            "started_at": f"{day}T00:00:00Z", "initial_capital": 100,
            "registration_reason": ["code_changed"] if self.cohorts else ["first_registration"],
            "fingerprint": {"source_database": source, "code": key},
            "benchmark": {"symbol": symbol, "basis": "price_index", "provider": "data_collector"},
        }
        for summary in self.summaries.values():
            summary["active"] = False
        self.summaries[key] = {
            "active": True, "issues": [], "return_observations": 0,
            "coverage": {"missing_sessions": []},
        }
        self.cohorts.append(cohort)
        self.events.append({"kind": "cohort_registered", "data": cohort})
        return cohort

    def close(self, day, index, *, flow=0, benchmark=100):
        cohort = self.cohorts[-1]
        prior = self.rows[-1] if self.rows else None
        count = len(self.rows) + 1
        cutoff = deepcopy(prior["ledger"]["cutoff_ledger"]) if prior else {"trades": [], "flows": []}
        if flow:
            cutoff["flows"].append({"id": count, "source_hash": f"flow-{count}"})
        row = {
            "date": day, "valuation_id": count, "source_hash": f"close-{count}",
            "measured_at": f"{day}T15:30:00+09:00", "twr_index": index,
            "total_value": (prior["total_value"] + flow) * index / prior["twr_index"] if prior else 100,
            "return_method": pv.DIRECT_CLOSE_METHOD, "initial_capital": 100,
            "first_close_in_series": prior is None,
            "baseline_valuation_id": prior["valuation_id"] if prior else None,
            "baseline_source_hash": prior["source_hash"] if prior else None,
            "baseline_measured_at": prior["measured_at"] if prior else f"{day}T15:30:00+09:00",
            "close_flow_since": flow, "calendar_hash": "calendar-v1", "issues": [],
            "benchmark": {"date": day, "close": benchmark, "source": "FinanceDataReader",
                          "symbol": cohort["benchmark"]["symbol"], "basis": "price_index"},
            "ledger": {"trades": [], "flows": [], "cutoff_ledger": cutoff},
        }
        self.rows.append(row)
        self.events.append({"kind": "close_observed", "data": {"cohort_id": cohort["cohort_id"], "observation": row}})
        own_count = sum(e["kind"] == "close_observed" and e["data"]["cohort_id"] == cohort["cohort_id"] for e in self.events)
        self.summaries[cohort["cohort_id"]]["return_observations"] = own_count - 1
        return row

    def report(self):
        reports = history.build_performance_history(self.events, self.summaries, HOLIDAYS)
        assert len(reports) == 1
        return reports[0]


def two_versions():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1, benchmark=100)
    evidence.close("2026-10-14", 1.1, benchmark=101)
    evidence.version("2026-10-15")
    evidence.close("2026-10-15", 1.21, benchmark=102)
    evidence.close("2026-10-16", 1.331, benchmark=103)
    return evidence


def test_code_boundary_keeps_account_return_but_not_version_samples(monkeypatch):
    evidence = two_versions()
    before = json.dumps([evidence.events, evidence.summaries], sort_keys=True)
    monkeypatch.setattr(pv, "_read_database", lambda *_: pytest.fail("report must not read a database"))

    report = evidence.report()

    assert report["observations"] == 4 and report["return_observations"] == 3
    assert report["performance"]["twr_pct"] == pytest.approx(33.1)
    assert report["performance"]["benchmark_return_pct"] == pytest.approx(3)
    assert [v["performance"]["twr_pct"] for v in report["versions"]] == pytest.approx([10, 10])
    assert [v["return_observations"] for v in report["versions"]] == [1, 1]
    comparison = report["comparisons"][0]
    assert comparison["transition"]["twr_pct"] == pytest.approx(10)
    assert comparison["matched_window"]["intervals"] == 1
    assert comparison["matched_window"]["twr_change_pp"] == pytest.approx(0)
    assert report["profitability_pass"] is None
    assert json.dumps([evidence.events, evidence.summaries], sort_keys=True) == before


def test_deposit_at_version_boundary_is_not_an_investment_gain():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-14", 1.1)
    evidence.version("2026-10-15")
    deposited = evidence.close("2026-10-15", 1.122, flow=100)
    evidence.close("2026-10-16", 1.15566)

    report = evidence.report()

    assert deposited["total_value"] == pytest.approx(214.2)
    assert report["performance"]["twr_pct"] == pytest.approx(15.566)
    assert report["comparisons"][0]["transition"]["twr_pct"] == pytest.approx(2)
    assert report["comparisons"][0]["matched_window"]["after"]["twr_pct"] == pytest.approx(3)
    assert "costs" not in report  # Empty first-observation trade lists do not establish zero boundary costs.


@pytest.mark.parametrize("field,value,issue", [
    ("baseline_valuation_id", 999, "close_baseline_link_unverified"),
    ("baseline_source_hash", "revised-close", "close_baseline_link_unverified"),
    ("baseline_measured_at", "2026-10-13T15:30:00+09:00", "close_baseline_link_unverified"),
    ("close_flow_since", 999, "close_twr_link_mismatch"),
    ("calendar_hash", "different-calendar", "calendar_changed"),
    ("first_close_in_series", True, "close_baseline_link_unverified"),
])
def test_broken_boundary_never_produces_one_total(field, value, issue):
    evidence = two_versions()
    evidence.rows[2][field] = value

    report = evidence.report()

    assert report["performance"]["twr_pct"] is None
    assert issue in report["issues"]
    assert report["comparisons"][0]["transition"]["status"] == "unlinked"
    assert report["comparisons"][0]["matched_window"]["twr_change_pp"] is None


@pytest.mark.parametrize("table", ["trades", "flows"])
@pytest.mark.parametrize("change", ["revised", "removed"])
def test_old_cutoff_membership_and_hashes_must_survive_the_boundary(table, change):
    evidence = two_versions()
    for row in evidence.rows:
        row["ledger"]["cutoff_ledger"][table] = [{"id": 10, "source_hash": "original"}]
    for row in evidence.rows[2:]:
        row["ledger"]["cutoff_ledger"][table] = ([{"id": 10, "source_hash": "changed"}] if change == "revised" else [])

    report = evidence.report()

    assert report["performance"]["twr_pct"] is None
    assert "cutoff_ledger_revised_or_missing" in report["issues"]
    assert len(report["segments"]) == 2


def test_closed_version_integrity_error_blocks_clean_looking_observations():
    evidence = two_versions()
    old = evidence.cohorts[0]["cohort_id"]
    evidence.summaries[old]["issues"] = ["observed_ledger_revised_or_missing"]

    report = evidence.report()

    assert all(row["issues"] == [] for row in evidence.rows)
    assert report["performance"]["twr_pct"] is None
    assert "observed_ledger_revised_or_missing" in report["issues"]
    assert report["versions"][1]["performance"]["twr_pct"] == pytest.approx(10)


def test_missing_session_is_a_break_not_an_invented_daily_return():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-15", 1.1)

    report = evidence.report()

    assert report["coverage"]["missing_sessions"] == ["2026-10-14"]
    assert report["return_observations"] == 0
    assert report["performance"]["twr_pct"] is None
    assert "nonadjacent_close_sessions" in report["issues"]


def test_a_weekend_is_not_a_missing_session():
    evidence = Evidence()
    evidence.version("2026-10-16")
    evidence.close("2026-10-16", 1)
    evidence.close("2026-10-19", 1.1)

    report = evidence.report()

    assert report["coverage"]["missing_sessions"] == []
    assert report["return_observations"] == 1
    assert report["performance"]["twr_pct"] == pytest.approx(10)


def test_different_source_database_does_not_merge_same_named_account():
    evidence = two_versions()
    evidence.cohorts[1]["fingerprint"]["source_database"] = "ledger-b"

    report = evidence.report()

    assert report["performance"]["twr_pct"] is None
    assert "source_database_changed" in report["issues"]
    assert len(report["segments"]) == 2
    assert report["comparisons"][0]["matched_window"]["status"] == "unavailable"


def test_benchmark_change_preserves_account_return_without_splicing_indices():
    evidence = two_versions()
    evidence.cohorts[1]["benchmark"]["symbol"] = "KS200"
    for row in evidence.rows[2:]:
        row["benchmark"]["symbol"] = "KS200"

    report = evidence.report()

    assert report["performance"]["twr_pct"] == pytest.approx(33.1)
    assert report["performance"]["benchmark_return_pct"] is None
    assert report["performance"]["excess_return_pct"] is None
    assert "benchmark_history_not_comparable" in report["issues"]
    matched = report["comparisons"][0]["matched_window"]
    assert matched["status"] == "observed" and matched["twr_change_pp"] == pytest.approx(0)
    assert matched["excess_change_pp"] is None


def test_equal_length_comparison_is_trailing_before_and_leading_after():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-14", 1.5)
    evidence.close("2026-10-15", 1.8)
    evidence.version("2026-10-16")
    evidence.close("2026-10-16", 1.98)
    evidence.close("2026-10-19", 1.9998)

    matched = evidence.report()["comparisons"][0]["matched_window"]

    assert matched["intervals"] == 1
    assert (matched["before"]["start_date"], matched["before"]["end_date"]) == ("2026-10-14", "2026-10-15")
    assert (matched["after"]["start_date"], matched["after"]["end_date"]) == ("2026-10-16", "2026-10-19")
    assert matched["before"]["twr_pct"] == pytest.approx(20)
    assert matched["after"]["twr_pct"] == pytest.approx(1)
    assert matched["twr_change_pp"] == pytest.approx(-19)


def test_empty_version_stays_in_history_without_creating_an_observation():
    evidence = Evidence()
    evidence.version("2026-10-12")
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-14", 1.1)
    evidence.version("2026-10-15")
    evidence.close("2026-10-15", 1.21)

    report = evidence.report()

    assert report["version_count"] == 3 and report["observations"] == 3
    assert report["return_observations"] == 2
    assert report["versions"][0]["performance"]["status"] == "insufficient_sample"
    assert report["performance"]["twr_pct"] == pytest.approx(21)
    assert all(c["matched_window"]["status"] == "insufficient_sample" for c in report["comparisons"])
    assert report["comparisons"][1]["transition"]["twr_pct"] == pytest.approx(10)


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_close_does_not_inflate_count_and_conflicts_block_total(conflict):
    evidence = two_versions()
    duplicate = deepcopy(evidence.rows[1])
    if conflict:
        duplicate["source_hash"] = "different-source-row"
    evidence.events.append({"kind": "close_observed", "data": {
        "cohort_id": evidence.cohorts[1]["cohort_id"], "observation": duplicate,
    }})
    original = json.dumps([evidence.events, evidence.summaries], sort_keys=True)

    report = evidence.report()

    assert report["observations"] == 4
    if conflict:
        assert report["performance"]["twr_pct"] is None
        assert "duplicate_close_conflict" in report["issues"]
        assert report["return_observations"] == 1
        assert sum(segment["return_observations"] for segment in report["segments"]) == 1
        disputed = next(segment for segment in report["segments"]
                        if segment["performance"]["start_date"] == "2026-10-14")
        assert disputed["performance"]["status"] == "continuity_unverified"
    else:
        assert report["performance"]["twr_pct"] == pytest.approx(33.1)
    assert json.dumps([evidence.events, evidence.summaries], sort_keys=True) == original


def test_drawdown_includes_loss_across_a_version_boundary():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-14", 1.2)
    evidence.version("2026-10-15")
    evidence.close("2026-10-15", 0.9)
    evidence.close("2026-10-16", 1)

    report = evidence.report()

    assert report["performance"]["twr_pct"] == pytest.approx(0)
    assert report["performance"]["drawdown_pct"] == pytest.approx(-25)
    assert report["comparisons"][0]["transition"]["twr_pct"] == pytest.approx(-25)
    assert [version["performance"]["drawdown_pct"] for version in report["versions"]] == [0, 0]


def test_registration_without_a_new_close_keeps_prior_history_visible():
    evidence = Evidence()
    evidence.version("2026-10-13")
    evidence.close("2026-10-13", 1)
    evidence.close("2026-10-14", 1.1)
    evidence.version("2026-10-15")

    report = evidence.report()

    assert report["observations"] == 2
    assert report["performance"]["twr_pct"] == pytest.approx(10)
    assert report["versions"][-1]["active"] is True
    assert report["versions"][-1]["observations"] == 0
    assert report["versions"][-1]["performance"]["twr_pct"] is None
    assert report["comparisons"][0]["transition"]["status"] == "awaiting_observations"


@pytest.mark.parametrize("bad_cutoff", [None, {"trades": None, "flows": []}])
def test_missing_cutoff_evidence_fails_closed(bad_cutoff):
    evidence = two_versions()
    evidence.rows[2]["ledger"]["cutoff_ledger"] = bad_cutoff

    report = evidence.report()

    assert report["performance"]["twr_pct"] is None
    assert "close_link_unverified" in report["issues"]


def test_missing_benchmark_does_not_erase_verified_account_return():
    evidence = two_versions()
    evidence.rows[2]["benchmark"] = None
    evidence.rows[2]["issues"] = ["benchmark_missing"]

    report = evidence.report()

    assert report["performance"]["twr_pct"] == pytest.approx(33.1)
    assert report["performance"]["benchmark_return_pct"] is None
    assert report["coverage"]["benchmark_missing_sessions"] == ["2026-10-15"]


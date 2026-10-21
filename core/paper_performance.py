"""Read-only account history across frozen validation cohorts.

This is a derived view of the hash-checked journal, not another enrollment or a
current database audit. Version boundaries never contribute to a version's
forward-test sample. No historical event, timestamp or promotion rule is changed.
"""
from __future__ import annotations

from collections import defaultdict
import math

from core import paper_validation as pv


REPORT_VERSION = 1
LIMITATIONS = [
    "전체 누적 성과는 저장 당시 검증된 동일 계좌의 관측을 연결한 값이며 현재 DB를 다시 감사한 결과가 아닙니다.",
    "버전 변경 경계의 수익률은 계좌 누적 성과에만 포함하며 어느 버전의 개선 효과로도 귀속하지 않습니다.",
    "변경 전후 비교는 같은 수의 수익 구간을 사용해도 시장 상황이 다르므로 코드 변경의 인과 효과나 우열을 입증하지 않습니다.",
    "전체 누적 관측을 현재 버전의 검증 표본으로 합산하지 않습니다. 비용은 기존 TWR에 반영된 범위이며 경계 비용을 0으로 추정하지 않습니다.",
]


def _blocking_issues(summary, observation):
    # Freshness and benchmark failures do not invalidate an already saved NAV.
    allowed = {"close_sessions_missing", "assessment_stale", "collection_stale",
               "policy_changed_pending_collection", "current_policy_unavailable"}
    return sorted({issue for issue in summary.get("issues", []) + observation.get("issues", [])
                   if issue not in allowed and not issue.startswith("benchmark_")})


def _point_issues(point):
    cohort, row, summary = point
    issues = _blocking_issues(summary, row)
    if not summary:
        issues.append("assessment_missing")
    try:
        pv._number(row["twr_index"], True)
        pv._number(row["total_value"], True)
        if pv._dt(row["measured_at"]).astimezone(pv.KST).date().isoformat() != row["date"]:
            issues.append("close_date_mismatch")
        if row.get("return_method") == pv.DIRECT_CLOSE_METHOD:
            if pv._number(row["initial_capital"], True) != pv._number(cohort["initial_capital"], True):
                issues.append("initial_capital_changed")
    except (ValueError, TypeError, KeyError):
        issues.append("close_value_invalid")
    return sorted(set(issues))


def _cutoff_rows(row, name):
    values = row.get("ledger", {}).get("cutoff_ledger", {}).get(name)
    if not isinstance(values, list):
        raise ValueError("cutoff_ledger_missing")
    result = {item["id"]: item["source_hash"] for item in values}
    if len(result) != len(values) or not all(result.values()):
        raise ValueError("cutoff_ledger_invalid")
    return result


def _close_identity(point):
    cohort, row, _ = point
    return (cohort["fingerprint"].get("source_database"), row.get("valuation_id"), row.get("source_hash"),
            row.get("measured_at"), row.get("twr_index"), row.get("return_method"))


def _link_issues(left, right, holidays):
    a, previous, _ = left
    b, current, _ = right
    issues = _point_issues(left) + _point_issues(right)
    for key in ("basket", "account_key"):
        if not a.get(key) or a.get(key) != b.get(key):
            issues.append("account_changed")
    source = a.get("fingerprint", {}).get("source_database")
    if not source or source != b.get("fingerprint", {}).get("source_database"):
        issues.append("source_database_changed")
    if not previous.get("calendar_hash") or previous.get("calendar_hash") != current.get("calendar_hash"):
        issues.append("calendar_changed")
    try:
        if len(pv._sessions(previous["date"], current["date"], holidays)) != 2:
            issues.append("nonadjacent_close_sessions")
    except (ValueError, TypeError):
        issues.append("trading_calendar_unavailable")
    method = previous.get("return_method")
    if not method or method != current.get("return_method"):
        issues.append("return_method_changed")
    elif method == pv.DIRECT_CLOSE_METHOD:
        try:
            if (current.get("first_close_in_series") is not False
                    or current.get("baseline_valuation_id") != previous["valuation_id"]
                    or current.get("baseline_source_hash") != previous["source_hash"]
                    or pv._dt(current["baseline_measured_at"]) != pv._dt(previous["measured_at"])):
                issues.append("close_baseline_link_unverified")
            if previous["initial_capital"] != current["initial_capital"]:
                issues.append("initial_capital_changed")
            denominator = pv._number(previous["total_value"], True) + pv._number(current["close_flow_since"])
            expected = pv._number(current["total_value"], True) / pv._number(denominator, True)
            actual = pv._number(current["twr_index"], True) / pv._number(previous["twr_index"], True)
            if not math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-8):
                issues.append("close_twr_link_mismatch")
            for name in ("trades", "flows"):
                old, new = _cutoff_rows(previous, name), _cutoff_rows(current, name)
                if any(new.get(key) != value for key, value in old.items()):
                    issues.append("cutoff_ledger_revised_or_missing")
        except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError):
            issues.append("close_link_unverified")
    elif method != "linked_measurements_start_period_flows" or a["cohort_id"] != b["cohort_id"]:
        # Legacy intraday baselines cannot prove a close link across versions.
        issues.append("close_baseline_link_unverified")
    return sorted(set(issues))


def _benchmark_key(point):
    cohort, row, _ = point
    quote = row.get("benchmark")
    spec = cohort.get("benchmark", {})
    if not quote or any(issue.startswith("benchmark_") for issue in row.get("issues", [])):
        return None
    try:
        pv._number(quote["close"], True)
        if (quote["date"] != row["date"] or quote["symbol"] != spec["symbol"]
                or quote["basis"] != spec["basis"] or not quote["source"]):
            return None
        return quote["symbol"], quote["basis"], quote["source"], spec.get("provider")
    except (ValueError, TypeError, KeyError):
        return None


def _metrics(points, holidays):
    rows = [point[1] for point in points]
    result = {"start_date": rows[0]["date"] if rows else None, "end_date": rows[-1]["date"] if rows else None,
              "twr_pct": None, "benchmark_return_pct": None, "excess_return_pct": None,
              "drawdown_pct": None, "drawdown_scope": "observed_closes_only",
              "return_method": "linked_stored_close_twr_start_period_flows", "issues": []}
    blocking = {issue for point in points for issue in _point_issues(point)}
    for left, right in zip(points, points[1:]):
        blocking.update(_link_issues(left, right, holidays))
    if blocking:
        return {**result, "status": "continuity_unverified", "issues": sorted(blocking)}
    if len(rows) < 2:
        return {**result, "status": "insufficient_sample"}
    result["twr_pct"] = pv._round((rows[-1]["twr_index"] / rows[0]["twr_index"] - 1) * 100)
    peak, drawdown = rows[0]["twr_index"], 0.0
    for row in rows:
        peak = max(peak, row["twr_index"])
        drawdown = min(drawdown, row["twr_index"] / peak - 1)
    result["drawdown_pct"] = pv._round(drawdown * 100)
    keys = [_benchmark_key(point) for point in points]
    if all(keys) and len(set(keys)) == 1:
        benchmark = (rows[-1]["benchmark"]["close"] / rows[0]["benchmark"]["close"] - 1) * 100
        result.update(benchmark_return_pct=pv._round(benchmark),
                      excess_return_pct=pv._round(result["twr_pct"] - benchmark))
    else:
        result["issues"].append("benchmark_history_not_comparable")
    return {**result, "status": "observed"}


def _segments(points, holidays):
    segments = []
    for point in points:
        if not segments or _link_issues(segments[-1][-1], point, holidays):
            segments.append([])
        segments[-1].append(point)
    return segments


def _comparison(before, after, before_points, after_points, holidays):
    transition = {"status": "awaiting_observations", "start_date": None, "end_date": None,
                  "twr_pct": None, "benchmark_return_pct": None, "excess_return_pct": None, "issues": []}
    if before_points and after_points:
        transition = _metrics([before_points[-1], after_points[0]], holidays)
        transition["status"] = "linked" if transition["status"] == "observed" else "unlinked"
    matched = {"status": "insufficient_sample", "intervals": 0, "before": None, "after": None,
               "twr_change_pp": None, "excess_change_pp": None, "issues": []}
    if before_points and after_points:
        # Fixed anchoring: trailing old-version vs leading new-version windows.
        # Never search for the best-performing interval after seeing returns.
        old = _segments(before_points, holidays)[-1]
        new = _segments(after_points, holidays)[0]
        count = min(len(old), len(new)) - 1
        if count > 0:
            old_window, new_window = old[-count - 1:], new[:count + 1]
            a, b = _metrics(old_window, holidays), _metrics(new_window, holidays)
            comparable = (a["status"] == b["status"] == "observed"
                          and before["account_key"] == after["account_key"]
                          and before["fingerprint"]["source_database"] == after["fingerprint"]["source_database"]
                          and old_window[-1][1].get("return_method") == new_window[0][1].get("return_method")
                          and before.get("initial_capital") == after.get("initial_capital")
                          and transition["status"] == "linked")
            matched.update(status="observed" if comparable else "unavailable", intervals=count, before=a, after=b)
            if comparable:
                matched["twr_change_pp"] = pv._round(b["twr_pct"] - a["twr_pct"])
                if (a["excess_return_pct"] is not None and b["excess_return_pct"] is not None
                        and _benchmark_key(old_window[0]) == _benchmark_key(new_window[0])):
                    matched["excess_change_pp"] = pv._round(b["excess_return_pct"] - a["excess_return_pct"])
                else:
                    matched["issues"].append("benchmark_history_not_comparable")
            else:
                matched["issues"].append("comparison_basis_unverified")
    return {"before_cohort_id": before["cohort_id"], "after_cohort_id": after["cohort_id"],
            "changed_at": after["started_at"], "change_reasons": after["registration_reason"],
            "transition": transition, "matched_window": matched}


def build_performance_history(events, summaries, holidays):
    """Derive history from existing evidence; never write or fetch missing data."""
    groups = defaultdict(list)
    for event in events:
        if event["kind"] == "cohort_registered":
            cohort = event["data"]
            groups[cohort["basket"], cohort["account_key"]].append(cohort)
    reports = []
    for (basket, account), cohorts in sorted(groups.items()):
        versions, all_points, by_cohort = [], [], {}
        conflicts, conflict_dates = set(), set()
        for cohort in cohorts:
            key = cohort["cohort_id"]
            summary = summaries.get(key, {})
            original_dates = {}
            for event in events:
                if event["kind"] == "close_observed" and event["data"].get("cohort_id") == key:
                    original = event["data"]["observation"]
                    identity = _close_identity((cohort, original, summary))
                    if original["date"] in original_dates and original_dates[original["date"]] != identity:
                        conflicts.add("duplicate_close_conflict")
                        conflict_dates.add(original["date"])
                    original_dates[original["date"]] = identity
            points = [(cohort, row, summary) for row in pv._observations(events, key)]
            by_cohort[key] = points
            all_points.extend(points)
        all_points.sort(key=lambda point: (point[1]["date"], point[0]["started_at"]))
        # Identical repeats do not inflate account samples; conflicts block totals.
        unique = {}
        for point in all_points:
            day = point[1]["date"]
            if day in unique:
                prior = unique[day]
                if _close_identity(prior) != _close_identity(point):
                    conflicts.add("duplicate_close_conflict")
                    conflict_dates.add(day)
                if _point_issues(point):
                    conflicts.update(_point_issues(point))
                    conflict_dates.add(day)
            else:
                unique[day] = point
        points = list(unique.values())
        # A conflicting day must not look like a valid segment or daily return.
        points = [(cohort, row, {**summary, "issues": sorted(set(summary.get("issues", [])) | conflicts)})
                  if row["date"] in conflict_dates else (cohort, row, summary)
                  for cohort, row, summary in points]
        for cohort in cohorts:
            key = cohort["cohort_id"]
            summary = summaries.get(key, {})
            own_points = [(c, row, {**s, "issues": sorted(set(s.get("issues", [])) | conflicts)})
                          if row["date"] in conflict_dates else (c, row, s)
                          for c, row, s in by_cohort[key]]
            by_cohort[key] = own_points
            metric = _metrics(own_points, holidays)
            versions.append({**{k: summary.get(k) for k in ("active", "ended_at")},
                             **{k: cohort[k] for k in ("cohort_id", "started_at", "registration_reason", "fingerprint")},
                             "observations": len(own_points),
                             "return_observations": sum(not _link_issues(a, b, holidays) for a, b in zip(own_points, own_points[1:])),
                             "performance": metric, "issues": sorted(set(summary.get("issues", [])) | set(metric["issues"]))})
        parts = _segments(points, holidays)
        performance = _metrics(points, holidays)
        if conflicts:
            performance.update(status="continuity_unverified", twr_pct=None, benchmark_return_pct=None,
                               excess_return_pct=None, drawdown_pct=None)
            performance["issues"] = sorted(set(performance["issues"]) | conflicts)
        dates = set(unique)
        expected = set()
        if dates:
            expected.update(pv._sessions(min(dates), max(dates), holidays))
        for cohort in cohorts:
            expected.update(summaries.get(cohort["cohort_id"], {}).get("coverage", {}).get("missing_sessions", []))
        missing = sorted(expected - dates)
        reports.append({"basket": basket, "account_key": account, "report_version": REPORT_VERSION,
                        "status": performance["status"], "observations": len(points),
                        "return_observations": sum(not _link_issues(a, b, holidays) for a, b in zip(points, points[1:])),
                        "version_count": len(cohorts), "performance": performance,
                        "coverage": {"missing_sessions": missing,
                                     "benchmark_missing_sessions": [p[1]["date"] for p in points if not _benchmark_key(p)]},
                        "issues": sorted(set(performance["issues"]) | ({"close_sessions_missing"} if missing else set())),
                        "segments": [{"observations": len(part), "return_observations": max(0, len(part) - 1),
                                      "performance": _metrics(part, holidays)} for part in parts],
                        "versions": versions,
                        "comparisons": [_comparison(a, b, by_cohort[a["cohort_id"]], by_cohort[b["cohort_id"]], holidays)
                                        for a, b in zip(cohorts, cohorts[1:])],
                        "research_only": True, "profitability_pass": None, "limitations": LIMITATIONS})
    return reports

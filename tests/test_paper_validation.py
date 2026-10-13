"""No operating DB, public price network or real-clock dependencies."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from core import paper_validation as pv


class Prices:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.source = "FinanceDataReader"

    def fetch_korean_stock(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        value = self.values.get(start)
        return pd.DataFrame({"close": [value]}, index=pd.to_datetime([start])) if value else pd.DataFrame()

    def get_last_source_info(self):
        return {"source": self.source}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = root / "config"
    config.mkdir(parents=True)
    protocol = {"schema_version": 1, "protocol_version": "test-v1", "minimum_observations": 2,
                "minimum_calendar_days": 2, "minimum_regime_observations": 1, "maximum_missing_ratio": 0.1,
                "maximum_capture_delay_sessions": 2,
                "baskets": {"test": {"benchmark": {"symbol": "KS11", "name": "KOSPI", "basis": "price_index", "provider": "data_collector"}}}}
    policies = {"baskets": {"test": {"enabled": True, "name": "test", "target_stock_weight": 1.0,
                                     "holdings": {"005930": 1.0}, "rebalance": {"min_trade_amount": 50000}}}}
    values = {"paper_validation.yaml": protocol, "baskets.yaml": policies,
              "settings.yaml": {"trading": {"market_close": "15:30"}, "data_source": {"api_secret": "must-not-leak"}},
              "risk_params.yaml": {"position_sizing": {"initial_capital": 100}},
              "holidays.yaml": {"holidays": ["2026-01-01", "2027-01-01"]}}
    for name, value in values.items():
        (config / name).write_text(yaml.safe_dump(value), encoding="utf-8")
    for name in pv.OPERATING_CODE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# operating version 1\n", encoding="utf-8")
    monkeypatch.setattr(pv, "ROOT", root)
    clock = [datetime.fromisoformat("2026-10-13T07:00:00+09:00").astimezone(timezone.utc)]
    monkeypatch.setattr(pv, "_utc_now", lambda: clock[0])
    db = tmp_path / "ledger.db"
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE portfolio_valuations(id INTEGER PRIMARY KEY, mode TEXT, account_key TEXT,
              date TEXT, measured_at TEXT, recorded_at TEXT, kind TEXT, total_value REAL, cash REAL,
              cumulative_return REAL, daily_return REAL, mdd REAL, detail TEXT, fingerprint TEXT);
            CREATE TABLE trade_history(id INTEGER PRIMARY KEY, mode TEXT, account_key TEXT,
              executed_at TEXT, action TEXT, total_amount REAL, commission REAL, tax REAL, slippage REAL);
            CREATE TABLE cash_flows(id INTEGER PRIMARY KEY, mode TEXT, account_key TEXT,
              occurred_at TEXT, amount REAL);
        """)
    folder = tmp_path / "evidence"
    prices = Prices()

    def at(value):
        clock[0] = datetime.fromisoformat(value).astimezone(timezone.utc)

    def close(day, nav=100, cumulative=0, *, flow=0, baseline_nav=None, baseline_cumulative=None,
              source="FinanceDataReader", measured=None, recorded=None):
        # The intra-day baseline itself may already include returns since prior close.
        baseline_nav = nav - flow if baseline_nav is None else baseline_nav
        baseline_cumulative = cumulative if baseline_cumulative is None else baseline_cumulative
        measured = measured or f"{day}T15:30:00"
        with sqlite3.connect(db) as connection:
            base = connection.execute("INSERT INTO portfolio_valuations(mode,account_key,date,measured_at,recorded_at,kind,total_value,cash,cumulative_return,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                     ("paper", "basket_rebalance:test", day, f"{day}T10:00:00", f"{day}T10:01:00", "intraday", baseline_nav, baseline_nav, baseline_cumulative, "{}")).lastrowid
            context = {"return_method": "linked_measurements_start_period_flows", "quote_dates": [day],
                       "positions": [{"symbol": "005930", "quantity": 1, "price": nav,
                                      "quote_date": day, "source": source, "observed_at": f"{day}T16:00:00"}],
                       "baseline_valuation_id": base, "baseline_measured_at": f"{day}T10:00:00", "flow_since": flow}
            row = connection.execute("INSERT INTO portfolio_valuations(mode,account_key,date,measured_at,recorded_at,kind,total_value,cash,cumulative_return,detail,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     ("paper", "basket_rebalance:test", day, measured, recorded or f"{day}T16:00:00", "close", nav, 0, cumulative, json.dumps(context), f"close-{base}")).lastrowid
        return row

    def collect():
        return pv.collect_paper_validation(db, config_path=config / "paper_validation.yaml", output_dir=folder, collector=prices)

    def write(name, value):
        (config / name).write_text(yaml.safe_dump(value), encoding="utf-8")

    return {"root": root, "db": db, "folder": folder, "prices": prices, "at": at, "close": close,
            "collect": collect, "write": write, "protocol": protocol, "policies": policies}


def active(result):
    return next(row for row in result["cohorts"] if row["active"])


def test_existing_closes_are_context_only_and_registration_never_backdates(setup):
    s = setup
    s["close"]("2026-10-12")
    before = s["db"].read_bytes()
    report = s["collect"]()
    row = active(report)
    assert row["observations"] == row["return_observations"] == 0
    assert row["historical_closes_excluded"] == 1
    assert row["status"] == "awaiting_forward"
    assert row["performance"]["twr_pct"] is None
    assert row["costs"]["trade_count"] is None
    assert not report["automatic_live_promotion"]
    assert s["prices"].calls == []
    assert s["db"].read_bytes() == before
    assert "must-not-leak" not in "".join(path.read_text(encoding="utf-8") for path in (s["folder"] / "events").glob("*.json"))


def test_future_closes_twr_neutralizes_deposit_and_records_costs_regimes(setup):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13", nav=100, cumulative=0)
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-14T07:00:00+09:00")
    assert active(s["collect"]())["observations"] == 1
    # A 100 deposit and 2% return produce 204 NAV, not a 104% investment return.
    s["close"]("2026-10-14", nav=204, cumulative=2, flow=100, baseline_nav=100, baseline_cumulative=0)
    with sqlite3.connect(s["db"]) as connection:
        connection.execute("INSERT INTO cash_flows VALUES(1,'paper','basket_rebalance:test','2026-10-14T12:00:00',100)")
        connection.execute("INSERT INTO trade_history VALUES(1,'paper','basket_rebalance:test','2026-10-14T11:00:00','BUY',50,1,2,3)")
        connection.execute("INSERT INTO trade_history VALUES(2,'live','basket_rebalance:test','2026-10-14T11:00:00','BUY',999,9,9,9)")
    s["prices"].values["2026-10-14"] = 101
    s["at"]("2026-10-15T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["performance"]["twr_pct"] == 2
    assert row["performance"]["benchmark_return_pct"] == 1
    assert row["performance"]["excess_return_pct"] == 1
    assert row["costs"]["explicit_cost"] == 3
    assert row["costs"]["modeled_slippage_embedded"] == 3
    assert row["costs"]["gross_turnover_ratio"] == pytest.approx(50 / 152)
    assert row["costs"]["net_external_flow"] == 100
    assert row["regimes"]["up"]["observations"] == 1
    assert row["regimes"]["down"]["portfolio_return_pct"] is None
    s["close"]("2026-10-15", nav=202, cumulative=1)
    s["prices"].values["2026-10-15"] = 100
    s["at"]("2026-10-16T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["status"] == "ready_for_review"
    assert row["return_observations"] == 2
    assert row["regimes"]["down"]["observations"] == 1
    assert row["profitability_pass"] is None and row["automatic_live_promotion"] is False


def test_benchmark_retry_appends_later_evidence_without_rewriting_failed_attempt(setup):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13")
    s["at"]("2026-10-14T07:00:00+09:00")
    assert "benchmark_missing" in active(s["collect"]())["issues"]
    original = {path: path.read_bytes() for path in (s["folder"] / "events").glob("*.json")}
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-14T08:00:00+09:00")
    row = active(s["collect"]())
    assert "benchmark_missing" not in row["issues"]
    assert all(path.read_bytes() == value for path, value in original.items())
    retries = [event for event in pv._load_events(s["folder"]) if event["kind"] == "benchmark_observed"]
    assert len(retries) == 1
    assert retries[0]["data"]["benchmark"]["observed_at"] == "2026-10-13T23:00:00.000000Z"


def test_missing_days_are_not_one_day_returns_or_recovered_as_forward(setup):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13")
    s["prices"].values.update({"2026-10-13": 100, "2026-10-15": 103})
    s["at"]("2026-10-14T07:00:00+09:00")
    s["collect"]()
    s["close"]("2026-10-15", nav=103, cumulative=3)
    s["at"]("2026-10-16T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["performance"]["twr_pct"] == 3
    assert row["return_observations"] == 0
    assert row["coverage"]["missing_sessions"] == ["2026-10-14"]
    s["close"]("2026-10-14", nav=102, cumulative=2)
    s["at"]("2026-10-20T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["observations"] == 2
    assert "2026-10-14" in row["coverage"]["missing_sessions"]
    assert row["status"] == "stale"


def test_long_offline_closes_are_excluded_even_when_measured_after_registration(setup):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13")
    s["at"]("2026-10-20T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["observations"] == 0
    assert "2026-10-13" in row["coverage"]["missing_sessions"]
    assert not s["prices"].calls


@pytest.mark.parametrize("change", ["policy", "code", "protocol"])
def test_policy_code_protocol_change_starts_separate_empty_cohort(setup, change):
    s = setup
    first = active(s["collect"]())
    s["close"]("2026-10-13")
    s["at"]("2026-10-14T07:00:00+09:00")
    s["prices"].values["2026-10-13"] = 100
    s["collect"]()
    if change == "policy":
        s["policies"]["baskets"]["test"]["target_stock_weight"] = 0.8
        s["write"]("baskets.yaml", s["policies"])
    elif change == "code":
        (s["root"] / pv.OPERATING_CODE_FILES[0]).write_text("# version 2", encoding="utf-8")
    else:
        s["protocol"]["minimum_observations"] = 20
        s["write"]("paper_validation.yaml", s["protocol"])
    s["at"]("2026-10-14T08:00:00+09:00")
    report = s["collect"]()
    row = active(report)
    assert row["cohort_id"] != first["cohort_id"]
    assert row["observations"] == 0
    assert row["historical_closes_excluded"] == 1
    old = next(row for row in report["cohorts"] if not row["active"])
    assert old["observations"] == 1
    assert ("config_changed" if change == "policy" else change + "_changed") in row["registration_reason"]


def test_ui_commentary_changes_do_not_restart_cohort(setup):
    s = setup
    first = active(s["collect"]())
    (s["root"] / "main.py").write_text("# scheduling change", encoding="utf-8")
    s["policies"]["baskets"]["test"]["name"] = "new label"
    s["policies"]["baskets"]["test"]["monitoring"] = {"deployment_tolerance": 0.03}
    s["write"]("baskets.yaml", s["policies"])
    assert active(s["collect"]())["cohort_id"] == first["cohort_id"]


def test_reader_does_not_create_files_or_query_database_and_detects_staleness(setup, monkeypatch):
    s = setup
    absent = s["folder"] / "absent"
    assert pv.read_paper_validation(absent)["status"] == "not_started"
    assert not absent.exists()
    s["collect"]()
    original = sorted(s["folder"].rglob("*"))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("reader opened database"))
    s["at"]("2026-10-14T07:00:00+09:00")
    row = active(pv.read_paper_validation(s["folder"]))
    assert row["status"] == "stale"
    assert "assessment_stale" in row["issues"]
    assert sorted(s["folder"].rglob("*")) == original


def test_default_reader_flags_policy_change_before_next_collection(setup, monkeypatch):
    s = setup
    s["collect"]()
    monkeypatch.setenv("QUANT_PAPER_VALIDATION_DIR", str(s["folder"]))
    assert pv.read_paper_validation()["status"] == "observing"
    s["policies"]["baskets"]["test"]["target_stock_weight"] = 0.5
    s["write"]("baskets.yaml", s["policies"])
    result = pv.read_paper_validation()
    assert active(result)["status"] == "stale"
    assert "policy_changed_pending_collection" in active(result)["issues"]


def test_unknown_calendar_year_cannot_leave_an_active_cohort_healthy(setup):
    s = setup
    s["collect"]()
    s["at"]("2028-06-01T07:00:00+09:00")
    result = pv.read_paper_validation(s["folder"])
    assert result["status"] == "unavailable"
    assert active(result)["status"] == "stale"
    assert "trading_calendar_unavailable" in active(result)["issues"]


def test_database_missing_does_not_create_it_and_missing_tables_stay_unknown(setup):
    s = setup
    missing = s["db"].parent / "absent.db"
    report = pv.collect_paper_validation(missing, output_dir=s["folder"])
    assert report["status"] == "unavailable" and not missing.exists()
    with sqlite3.connect(s["db"]) as connection:
        connection.execute("DROP TABLE trade_history")
    s["collect"]()
    s["close"]("2026-10-13")
    s["close"]("2026-10-14")
    s["prices"].values.update({"2026-10-13": 100, "2026-10-14": 101})
    s["at"]("2026-10-15T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["costs"]["trade_count"] is None
    assert row["costs"]["explicit_cost"] is None
    assert "trade_history_missing" in row["issues"]


def test_invalid_yaml_fails_without_echoing_configuration_or_credentials(setup):
    s = setup
    (s["root"] / "config" / "settings.yaml").write_text("private_secret: [must-not-leak\n", encoding="utf-8")
    result = s["collect"]()
    assert result["status"] == "unavailable"
    assert "must-not-leak" not in json.dumps(result)
    assert "must-not-leak" not in "".join(path.read_text(encoding="utf-8") for path in (s["folder"] / "events").glob("*.json"))


def test_mutated_ledger_is_not_silently_accepted(setup):
    s = setup
    s["collect"]()
    first_id = s["close"]("2026-10-13")
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-14T07:00:00+09:00")
    s["collect"]()
    with sqlite3.connect(s["db"]) as connection:
        connection.execute("UPDATE portfolio_valuations SET total_value=999 WHERE id=?", (first_id,))
    row = active(s["collect"]())
    assert "observed_ledger_revised_or_missing" in row["issues"]
    assert not row["requirements"]["integrity"]
    assert row["performance"]["twr_pct"] is None


@pytest.mark.parametrize("mutation", ["late_trade", "late_flow", "baseline"])
def test_late_interval_inserts_and_revised_baselines_invalidate_evidence(setup, mutation):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13")
    s["close"]("2026-10-14", nav=101, cumulative=1)
    s["prices"].values.update({"2026-10-13": 100, "2026-10-14": 101})
    s["at"]("2026-10-15T07:00:00+09:00")
    assert active(s["collect"]())["requirements"]["integrity"]
    with sqlite3.connect(s["db"]) as connection:
        if mutation == "late_trade":
            connection.execute("INSERT INTO trade_history VALUES(99,'paper','basket_rebalance:test','2026-10-14T11:00:00','BUY',50,1,2,3)")
        elif mutation == "late_flow":
            connection.execute("INSERT INTO cash_flows VALUES(99,'paper','basket_rebalance:test','2026-10-14T09:00:00',100)")
        else:
            connection.execute("UPDATE portfolio_valuations SET cumulative_return=999 WHERE kind='intraday' AND date='2026-10-14'")
    row = active(s["collect"]())
    assert row["status"] == "data_gaps"
    assert "observed_ledger_revised_or_missing" in row["issues"]
    assert not row["requirements"]["integrity"]
    assert row["performance"]["twr_pct"] is None


@pytest.mark.parametrize("bad", ["quote", "time", "cash_flow", "twr"])
def test_unverified_closes_never_count_as_matched_return_evidence(setup, bad):
    s = setup
    s["collect"]()
    arguments = {"source": "unknown"} if bad == "quote" else {"measured": "2026-10-13T11:00:00"} if bad == "time" else {"flow": 100} if bad == "cash_flow" else {"baseline_cumulative": 55}
    s["close"]("2026-10-13", **arguments)
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-14T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["return_observations"] == 0
    assert row["status"] == "data_gaps"
    assert not row["requirements"]["integrity"]


@pytest.mark.parametrize("mutation", ["baseline_recorded_in_future", "quote_after_close_recording"])
def test_source_close_cannot_claim_inputs_observed_after_its_recording(setup, mutation):
    s = setup
    s["collect"]()
    close_id = s["close"]("2026-10-13")
    with sqlite3.connect(s["db"]) as connection:
        if mutation == "baseline_recorded_in_future":
            connection.execute("UPDATE portfolio_valuations SET recorded_at='2026-10-15T10:00:00' WHERE kind='intraday'")
        else:
            connection.execute("UPDATE portfolio_valuations SET recorded_at='2026-10-13T15:45:00' WHERE id=?", (close_id,))
    s["at"]("2026-10-14T07:00:00+09:00")
    row = active(s["collect"]())
    assert row["observations"] == 0 and not row["requirements"]["integrity"]


def test_current_session_future_record_and_clock_rollback_not_forward(setup):
    s = setup
    s["collect"]()
    s["close"]("2026-10-13", recorded="2026-10-15T16:00:00")
    s["at"]("2026-10-13T17:00:00+09:00")
    assert active(s["collect"]())["observations"] == 0
    s["at"]("2026-10-14T07:00:00+09:00")
    assert active(s["collect"]())["observations"] == 0
    s["at"]("2026-10-12T07:00:00+09:00")
    with pytest.raises(ValueError, match="clock_rollback"):
        s["collect"]()


def test_tampered_journal_fails_closed_and_os_lock_releases(setup):
    s = setup
    s["collect"]()
    with pv._exclusive_lock(s["folder"]):
        with pytest.raises(ValueError, match="already_running"):
            s["collect"]()
    assert s["collect"]()["status"] == "observing"
    path = sorted((s["folder"] / "events").glob("*.json"))[0]
    value = json.loads(path.read_text(encoding="utf-8"))
    value["data"]["started_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert pv.read_paper_validation(s["folder"])["status"] == "corrupt"
    with pytest.raises(ValueError, match="integrity_error"):
        s["collect"]()


def direct_close(s, day, *, cash=100, positions=None, prior=None, flow=0, index=1, changes=None):
    """Source fixture only: no inferred historical return or fabricated baseline."""
    positions = positions or []
    measured = f"{day}T15:30:00"
    with sqlite3.connect(s["db"]) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(trade_history)")}
        if "symbol" not in columns:
            connection.execute("ALTER TABLE trade_history ADD COLUMN symbol TEXT")
            connection.execute("ALTER TABLE trade_history ADD COLUMN quantity INTEGER")
        baseline = connection.execute("SELECT measured_at FROM portfolio_valuations WHERE id=?", (prior,)).fetchone() if prior else None
        trade_ids = [row[0] for row in connection.execute("SELECT id FROM trade_history WHERE mode='paper' AND account_key='basket_rebalance:test' AND executed_at<=?", (measured,))]
        context = {"return_method": pv.DIRECT_CLOSE_METHOD, "first_close_in_series": prior is None,
                   "baseline_valuation_id": prior, "baseline_measured_at": baseline[0] if baseline else measured,
                   "ledger_cutoff_at": measured, "flow_since": flow, "initial_capital": 100,
                   "trade_ids": trade_ids, "quote_dates": [day],
                   "positions": [{"symbol": symbol, "quantity": quantity, "price": price,
                                  "quote_date": day, "source": "FinanceDataReader", "observed_at": f"{day}T16:00:00"}
                                 for symbol, quantity, price in positions]}
        context.update(changes or {})
        total = cash + sum(quantity * price for _, quantity, price in positions)
        return connection.execute("INSERT INTO portfolio_valuations(mode,account_key,date,measured_at,recorded_at,kind,total_value,cash,cumulative_return,detail,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  ("paper", "basket_rebalance:test", day, measured, f"{day}T16:00:00", "close", total, cash,
                                   (index - 1) * 100, json.dumps(context), "direct-" + day)).lastrowid


def test_empty_ledger_registration_has_no_collector_or_price_access(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(pv, "_make_benchmark_collector", lambda *_: pytest.fail("empty enrollment must not initialize a price client"))
    before = s["db"].read_bytes()
    report = pv.collect_paper_validation(s["db"], config_path=s["root"] / "config/paper_validation.yaml", output_dir=s["folder"])
    registered = active(report)
    assert registered["observations"] == 0 and registered["status"] == "awaiting_forward"
    assert s["db"].read_bytes() == before and not s["prices"].calls
    direct_close(s, "2026-10-13")
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-13T16:30:00+09:00")
    row = active(s["collect"]())
    assert row["cohort_id"] == registered["cohort_id"] and row["observations"] == 1
    assert row["return_observations"] == 0 and row["performance"]["twr_pct"] is None
    assert row["issues"] == []
    # Null first-close baseline is valid when re-audited the next morning.
    s["at"]("2026-10-14T08:30:00+09:00")
    assert active(s["collect"]())["issues"] == []


def test_same_day_direct_closes_use_actual_cutoff_and_neutralize_after_hours_flow(setup):
    s = setup
    s["collect"]()
    # Add columns without modifying the legacy tests' nine-column fixtures.
    with sqlite3.connect(s["db"]) as connection:
        connection.execute("ALTER TABLE trade_history ADD COLUMN symbol TEXT")
        connection.execute("ALTER TABLE trade_history ADD COLUMN quantity INTEGER")
        connection.execute("INSERT INTO trade_history VALUES(1,'paper','basket_rebalance:test','2026-10-13T10:00:00','BUY',100,0,0,0,'005930',1)")
        connection.execute("INSERT INTO cash_flows VALUES(1,'paper','basket_rebalance:test','2026-10-13T17:00:00',100)")
        connection.execute("INSERT INTO trade_history VALUES(2,'paper','basket_rebalance:test','2026-10-13T17:01:00','BUY',100,0,0,0,'005930',1)")
    first = direct_close(s, "2026-10-13", cash=0, positions=[("005930", 1, 100)])
    s["prices"].values.update({"2026-10-13": 100, "2026-10-14": 101})
    s["at"]("2026-10-13T15:59:59+09:00")
    assert active(s["collect"]())["observations"] == 0
    s["at"]("2026-10-13T16:00:00+09:00")
    assert active(s["collect"]())["observations"] == 1
    direct_close(s, "2026-10-14", cash=0, positions=[("005930", 2, 102)], prior=first, flow=100, index=1.02)
    s["at"]("2026-10-14T16:30:00+09:00")
    row = active(s["collect"]())
    assert row["issues"] == [] and row["return_observations"] == 1
    assert row["performance"]["twr_pct"] == 2 and row["performance"]["benchmark_return_pct"] == 1
    assert row["costs"]["net_external_flow"] == 100 and row["costs"]["trade_count"] == 1


@pytest.mark.parametrize("now,expected", [
    ("2026-10-16T15:59:59+09:00", "2026-10-15"),
    ("2026-10-16T16:00:00+09:00", "2026-10-16"),
    ("2026-10-17T08:30:00+09:00", "2026-10-16"),
    ("2026-10-18T16:30:00+09:00", "2026-10-16"),
    ("2026-10-19T08:30:00+09:00", "2026-10-16"),
])
def test_expected_session_uses_confirmed_close_and_preserves_weekend(now, expected):
    assert pv._expected(datetime.fromisoformat(now), {"2026-01-01"}) == expected
    assert pv._expected(datetime.fromisoformat("2026-10-19T16:30:00+09:00"), {"2026-01-01", "2026-10-19"}) == "2026-10-16"
    assert pv._expected(datetime.fromisoformat("2026-10-19T16:30:00+09:00"), {"2026-01-01"}, "16:30") == "2026-10-16"


def test_weekend_reader_remains_fresh_until_next_confirmed_session(setup):
    s = setup
    s["at"]("2026-10-16T08:30:00+09:00")
    s["collect"]()
    direct_close(s, "2026-10-16")
    s["prices"].values["2026-10-16"] = 100
    s["at"]("2026-10-16T16:30:00+09:00")
    assert active(s["collect"]())["issues"] == []
    s["at"]("2026-10-19T08:30:00+09:00")
    assert active(pv.read_paper_validation(s["folder"]))["status"] == "insufficient_sample"
    s["at"]("2026-10-19T16:00:00+09:00")
    assert "assessment_stale" in active(pv.read_paper_validation(s["folder"]))["issues"]


@pytest.mark.parametrize("changes,index", [
    ({"initial_capital": 200}, 1), ({"ledger_cutoff_at": "2026-10-13T16:00:00"}, 1),
    ({"baseline_valuation_id": 99}, 1), ({"flow_since": 100}, 1),
    ({"first_close_in_series": False}, 1), ({"trade_ids": [1, 1]}, 1), ({}, 1.8),
])
def test_direct_close_bootstrap_cannot_invent_a_baseline_or_prior_profit(setup, changes, index):
    s = setup
    s["collect"]()
    direct_close(s, "2026-10-13", changes=changes, index=index)
    s["at"]("2026-10-13T16:30:00+09:00")
    row = active(s["collect"]())
    assert row["observations"] == 0 and not row["requirements"]["integrity"]


@pytest.mark.parametrize("mutation,issue", [("cash", "close_ledger_cash_mismatch"), ("quantity", "close_ledger_quantity_mismatch"), ("ids", "close_ledger_trade_ids_mismatch")])
def test_direct_close_full_ledger_must_explain_cash_positions_and_ids(setup, mutation, issue):
    s = setup
    s["collect"]()
    direct_close(s, "2026-10-13", cash=99 if mutation == "cash" else 100,
                 positions=[("005930", 1, 1)] if mutation == "quantity" else [],
                 changes={"trade_ids": [99]} if mutation == "ids" else None)
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-13T16:30:00+09:00")
    row = active(s["collect"]())
    assert issue in row["issues"] and row["performance"]["status"] == "unverified_ledger"


def test_late_insert_before_first_close_invalidates_saved_direct_evidence(setup):
    s = setup
    s["collect"]()
    direct_close(s, "2026-10-13")
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-13T16:30:00+09:00")
    assert active(s["collect"]())["issues"] == []
    # These offsetting old entries leave NAV unchanged; membership still changed.
    with sqlite3.connect(s["db"]) as connection:
        connection.execute("INSERT INTO cash_flows VALUES(1,'paper','basket_rebalance:test','2026-10-13T09:00:00',100)")
        connection.execute("INSERT INTO cash_flows VALUES(2,'paper','basket_rebalance:test','2026-10-13T10:00:00',-100)")
    row = active(s["collect"]())
    assert "observed_ledger_revised_or_missing" in row["issues"]
    assert row["performance"]["status"] == "unverified_ledger"


def test_registration_after_close_does_not_relabel_that_day_as_forward(setup):
    s = setup
    s["at"]("2026-10-13T15:31:00+09:00")
    s["collect"]()
    direct_close(s, "2026-10-13")
    s["at"]("2026-10-13T16:30:00+09:00")
    row = active(s["collect"]())
    assert row["observations"] == 0 and not s["prices"].calls


def test_benchmark_provider_and_implementation_are_pinned_in_protocol(setup):
    s = setup
    s["protocol"]["baskets"]["test"]["benchmark"]["provider"] = "naver_public"
    s["write"]("paper_validation.yaml", s["protocol"])
    implementation = s["root"] / "core/observation_benchmark.py"
    implementation.write_text("# public provider version 1", encoding="utf-8")
    first = active(s["collect"]())
    direct_close(s, "2026-10-13")
    s["prices"].values["2026-10-13"] = 100
    s["at"]("2026-10-13T16:30:00+09:00")
    assert "benchmark_provider_mismatch" in active(s["collect"]())["issues"]
    implementation.write_text("# public provider version 2", encoding="utf-8")
    second = active(s["collect"]())
    assert second["cohort_id"] != first["cohort_id"] and second["registration_reason"] == ["protocol_changed"]
    assert second["observations"] == 0 and second["historical_closes_excluded"] == 1


def test_public_benchmark_preserves_provenance_and_rejects_replay(setup, monkeypatch):
    s = setup
    s["prices"].values["2026-10-12"] = 100
    metadata = {"source": "NAVERPublicIndex", "source_url": "https://m.stock.naver.com/api/index/KOSPI/price",
                "response_hash": "a" * 64, "publication_timestamp": None, "direct_krx_feed": False,
                "provider": "NAVER Securities public web", "observed_at": pv._stamp(pv._utc_now()), "replay": False}
    monkeypatch.setattr(s["prices"], "get_last_source_info", lambda: metadata)
    quote, error = pv._benchmark(s["prices"], "KS11", "2026-10-12", pv._utc_now(), "naver_public")
    assert error is None and quote["provenance"]["response_hash"] == "a" * 64
    assert quote["provenance"]["publication_timestamp"] is None
    metadata["replay"] = True
    assert pv._benchmark(s["prices"], "KS11", "2026-10-12", pv._utc_now(), "naver_public") == (None, "benchmark_replay_not_forward")
    metadata.update(replay=False, observed_at="2027-01-01T00:00:00Z")
    assert pv._benchmark(s["prices"], "KS11", "2026-10-12", pv._utc_now(), "naver_public") == (None, "benchmark_observation_in_future")

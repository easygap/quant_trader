from copy import deepcopy
from io import BytesIO
import json
import time
import zipfile

import pytest
import requests

import core.market_intelligence as intelligence
from core.market_research import _now as real_now


SPEC = {"symbol": "005930", "name": "삼성전자"}
DATES = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12"]


def config():
    return {"version": 1, "universe": [SPEC], "workers": 1,
            "disclosures": {"enabled": True, "lookback_calendar_days": 14, "max_pages_per_company": 2},
            "investor_flows": {"enabled": True, "provider": "naver_securities", "lookback_sessions": 4}}


def disclosure(receipt="20261012000001", title="[기재정정]사업보고서", rm="정"):
    return {"rcept_no": receipt, "rcept_dt": "20261012", "report_nm": title,
            "corp_code": "00126380", "stock_code": "005930", "corp_name": "삼성전자", "rm": rm}


def flows(dates=DATES):
    return {"itemCode": "005930", "dealTrendInfos": [
        {"itemCode": "005930", "bizdate": day.replace("-", ""), "individualPureBuyQuant": "+1,000",
         "organPureBuyQuant": "0", "foreignerPureBuyQuant": "-950"} for day in dates]}


class Transport:
    def __init__(self, *, pages=None, flow=None):
        self.pages = pages if pages is not None else [{"status": "000", "total_page": 1, "total_count": 1, "list": [disclosure()]}]
        self.flow = flow if flow is not None else flows()
        self.calls = []

    def get_bytes(self, url, **kwargs):
        self.calls.append("corp_code")
        data = BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("CORPCODE.xml", "<result><list><stock_code>005930</stock_code><corp_code>00126380</corp_code></list></result>")
        return data.getvalue()

    def get_json(self, url, *, params=None, **kwargs):
        if "list.json" in url:
            self.calls.append(("disclosures", params["last_reprt_at"], params["page_no"]))
            response = self.pages[int(params["page_no"]) - 1]
        else:
            self.calls.append("flow")
            response = self.flow
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


@pytest.fixture
def clock(monkeypatch):
    value = ["2026-10-13T12:00:00+09:00"]
    monkeypatch.setattr(intelligence, "_now", lambda timestamp=None: real_now(value[0] if timestamp is None else timestamp))
    return value


def report(transport=None, **kwargs):
    return intelligence.collect_market_intelligence(config(), transport=transport or Transport(), dart_api_key="fixture-key", **kwargs)


def test_real_collection_contract_tracks_dates_provider_corrections_and_units(clock):
    transport = Transport()
    result = report(transport)
    assert result["status"] == "ready"
    assert result["flow_sessions"] == DATES
    assert result["expected_session"] == "2026-10-12"
    assert result["replay"] is False
    assert ("disclosures", "N", "1") in transport.calls
    row = result["disclosures"]["records"][0]
    assert row["provider"] == "OpenDART" and row["published_date"] == "2026-10-12"
    assert row["published_at"] is None and row["published_precision"] == "date_only"
    assert row["is_correction"] and row["has_later_correction"]
    assert row["corrects_record_id"] is None
    assert row["available_to_system_at"] == result["generated_at"]
    flow = result["investor_flows"]["records"][0]
    assert flow["individual_net_buy_shares"] == 1000
    assert flow["institution_net_buy_shares"] == 0
    assert flow["foreign_net_buy_shares"] == -950
    assert flow["unit"] == "shares" and flow["published_at"] is None
    assert flow["market_scope"] == "provider_unspecified_venue_coverage"


def test_missing_api_key_is_unavailable_not_empty_success(clock):
    transport = Transport()
    result = intelligence.collect_market_intelligence(config(), transport=transport, dart_api_key="")
    assert result["status"] == "partial"
    assert result["disclosures"]["status"] == "unavailable"
    assert result["disclosures"]["coverage"]["confirmed_empty"] == 0
    assert result["disclosures"]["symbols"][0]["issues"] == ["dart_api_key_missing"]
    assert transport.calls == ["flow"]


def test_documented_no_data_is_confirmed_but_failed_response_is_not(clock):
    empty = report(Transport(pages=[{"status": "013", "message": "조회된 데이터가 없습니다."}]))
    failed = report(Transport(pages=[{"status": "020", "message": "fixture-key MUST NOT persist"}]))
    assert empty["disclosures"]["status"] == "ready"
    assert empty["disclosures"]["coverage"]["confirmed_empty"] == 1
    assert failed["disclosures"]["status"] == "unavailable"
    assert "dart_status_020" in failed["disclosures"]["symbols"][0]["issues"]
    assert "fixture-key" not in json.dumps(failed)


def test_corporation_archive_authentication_xml_retains_safe_status(clock):
    transport = Transport()
    transport.get_bytes = lambda *args, **kwargs: b"<result><status>010</status><message>fixture-key private</message></result>"
    result = report(transport)
    assert result["disclosures"]["status"] == "unavailable"
    assert result["disclosures"]["symbols"][0]["issues"] == ["dart_status_010"]
    assert "fixture-key" not in json.dumps(result)
    assert result["investor_flows"]["status"] == "ready"


def test_pagination_preserves_records_but_cannot_claim_complete_when_capped(clock):
    pages = [{"status": "000", "total_page": 3, "total_count": 3, "list": [disclosure()]},
             {"status": "000", "total_page": 3, "total_count": 3, "list": [disclosure("20261012000002", "사업보고서", "철")]}]
    result = report(Transport(pages=pages))["disclosures"]
    assert len(result["records"]) == 2
    assert result["status"] == "partial"
    assert "pagination_limit_reached" in result["symbols"][0]["issues"]
    assert result["records"][1]["is_withdrawal"]


@pytest.mark.parametrize("bad", [None, {}, {"status": "000"}, {"status": "000", "list": [], "total_count": 2}, {"status": "000", "list": [], "total_count": 0, "total_page": 1}])
def test_bad_disclosure_response_never_looks_healthy(clock, bad):
    result = report(Transport(pages=[bad]))
    assert result["disclosures"]["status"] == "unavailable"
    assert result["disclosures"]["coverage"]["confirmed_empty"] == 0


def test_current_day_and_unexpected_older_flows_are_excluded(clock):
    data = flows(DATES + ["2026-10-13", "2026-09-30"])
    result = report(Transport(flow=data))["investor_flows"]
    assert result["status"] == "ready"
    assert {row["trade_date"] for row in result["records"]} == set(DATES)


def test_short_disclosure_page_cannot_claim_full_coverage(clock):
    source = Transport(pages=[{"status": "000", "total_count": 2, "total_page": 1, "list": [disclosure()]}])
    section = report(source)["disclosures"]
    assert section["status"] == "partial"
    assert "disclosure_count_mismatch" in section["symbols"][0]["issues"]


def test_missing_quantity_is_null_and_marks_partial_never_zero(clock):
    data = flows()
    data["dealTrendInfos"][0]["organPureBuyQuant"] = "-"
    result = report(Transport(flow=data))["investor_flows"]
    assert result["status"] == "partial"
    assert result["records"][0]["institution_net_buy_shares"] is None
    assert result["records"][0]["missing_fields"] == ["institution_net_buy_shares"]


def test_duplicate_session_is_removed_instead_of_arbitrarily_selected(clock):
    data = flows()
    data["dealTrendInfos"].append(deepcopy(data["dealTrendInfos"][0]))
    result = report(Transport(flow=data))["investor_flows"]
    assert result["status"] == "partial"
    assert DATES[0] not in {row["trade_date"] for row in result["records"]}
    assert result["symbols"][0]["missing_sessions"] == [DATES[0]]


@pytest.mark.parametrize("data", [{"itemCode": "035420", "dealTrendInfos": []}, {"itemCode": "005930", "dealTrendInfos": []}, {}])
def test_wrong_ticker_and_empty_flow_never_looks_healthy(clock, data):
    result = report(Transport(flow=data))["investor_flows"]
    assert result["status"] == "unavailable"
    assert result["coverage"]["confirmed_empty"] == 0


def test_transport_errors_do_not_leak_api_key_or_request_url(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://provider.example?crtfc_key=super-secret")
    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(intelligence.SourceError) as error:
        intelligence.PublicTransport().get_json("https://provider.example", params={"crtfc_key": "super-secret"})
    assert str(error.value) == "ConnectionError"


def test_elapsed_budget_skips_request_and_records_unavailable(clock):
    transport = Transport()
    result = intelligence.collect_investor_flows(SPEC, transport=transport, sessions=DATES, timeout=1, deadline=time.monotonic() - 1)
    assert result["status"] == "unavailable"
    assert result["issues"] == ["collection_budget_exhausted"]
    assert transport.calls == []


def test_append_only_versions_retain_first_observation_and_revision_chain(clock, tmp_path):
    first = report()
    intelligence.save_market_intelligence(first, tmp_path)
    initial = {path.name: path.read_bytes() for path in (tmp_path / "versions").glob("*.json")}
    clock[0] = "2026-10-13T13:00:00+09:00"
    second = report()
    intelligence.save_market_intelligence(second, tmp_path)
    latest = intelligence.read_market_intelligence(tmp_path / "latest.json")
    assert latest["status"] == "ready"
    assert latest["disclosures"]["records"][0]["first_observed_at"] == first["generated_at"]
    assert latest["disclosures"]["records"][0]["observed_at"] == second["generated_at"]
    assert {path.name: path.read_bytes() for path in (tmp_path / "versions").glob("*.json")} == initial
    clock[0] = "2026-10-13T14:00:00+09:00"
    altered = Transport()
    altered.pages[0]["list"][0]["rm"] = "정철"
    intelligence.save_market_intelligence(report(altered), tmp_path)
    changed = intelligence.read_market_intelligence(tmp_path / "latest.json")["disclosures"]["records"][0]
    assert changed["previous_revision_id"] == first["disclosures"]["records"][0]["revision_id"]
    assert changed["first_observed_at"] == clock[0]
    assert len(list((tmp_path / "versions").glob("*.json"))) == len(initial) + 1
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 3
    for filename, contents in initial.items():
        assert (tmp_path / "versions" / filename).read_bytes() == contents


def test_failure_snapshot_preserves_successful_evidence_history(clock, tmp_path):
    intelligence.save_market_intelligence(report(), tmp_path)
    first_versions = list((tmp_path / "versions").glob("*.json"))
    clock[0] = "2026-10-13T13:00:00+09:00"
    failed = report(Transport(pages=[{"status": "020"}], flow={}))
    intelligence.save_market_intelligence(failed, tmp_path)
    assert intelligence.read_market_intelligence(tmp_path / "latest.json")["status"] == "unavailable"
    assert all(path.exists() for path in first_versions)
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2
    clock[0] = "2026-10-13T14:00:00+09:00"
    changed = Transport()
    changed.pages[0]["list"][0]["rm"] = "철"
    intelligence.save_market_intelligence(report(changed), tmp_path)
    row = intelligence.read_market_intelligence(tmp_path / "latest.json")["disclosures"]["records"][0]
    assert row["previous_revision_id"] is not None


def test_immutable_atomic_write_never_replaces_existing_evidence(tmp_path):
    path = tmp_path / "version.json"
    intelligence._atomic_json(path, {"version": 1}, immutable=True)
    with pytest.raises(FileExistsError):
        intelligence._atomic_json(path, {"version": 2}, immutable=True)
    assert json.loads(path.read_text()) == {"version": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_revision_reversion_then_failure_links_next_to_latest_observation(clock, tmp_path):
    first = report()
    intelligence.save_market_intelligence(first, tmp_path)
    clock[0] = "2026-10-13T13:00:00+09:00"
    second_source = Transport()
    second_source.pages[0]["list"][0]["rm"] = "정철"
    second = report(second_source)
    intelligence.save_market_intelligence(second, tmp_path)
    clock[0] = "2026-10-13T14:00:00+09:00"
    intelligence.save_market_intelligence(report(), tmp_path)  # Provider reverts to A.
    clock[0] = "2026-10-13T15:00:00+09:00"
    intelligence.save_market_intelligence(report(Transport(pages=[{"status": "020"}], flow={})), tmp_path)
    clock[0] = "2026-10-13T16:00:00+09:00"
    third_source = Transport()
    third_source.pages[0]["list"][0]["rm"] = "철"
    intelligence.save_market_intelligence(report(third_source), tmp_path)
    latest = intelligence.read_market_intelligence(tmp_path / "latest.json")["disclosures"]["records"][0]
    assert latest["previous_revision_id"] == first["disclosures"]["records"][0]["revision_id"]
    assert latest["previous_revision_id"] != second["disclosures"]["records"][0]["revision_id"]


@pytest.mark.parametrize("mutation", ["future", "missing_time", "naive_time", "tampered_quantity", "inconsistent_status", "duplicate"])
def test_reader_rejects_invalid_schema_or_provenance(clock, tmp_path, mutation):
    result = report()
    if mutation == "future":
        result["generated_at"] = "2026-10-14T13:00:00+09:00"
    elif mutation == "missing_time":
        result["generated_at"] = None
    elif mutation == "naive_time":
        result["generated_at"] = "2026-10-13T12:00:00"
    elif mutation == "tampered_quantity":
        result["investor_flows"]["records"][0]["individual_net_buy_shares"] = 88888
    elif mutation == "inconsistent_status":
        result["disclosures"]["status"] = "unavailable"
    else:
        result["disclosures"]["records"].append(deepcopy(result["disclosures"]["records"][0]))
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    assert intelligence.read_market_intelligence(path)["status"] == "unavailable"


def test_reader_rechecks_session_and_default_configuration(clock, tmp_path, monkeypatch):
    intelligence.save_market_intelligence(report(), tmp_path)
    monkeypatch.setattr(intelligence, "intelligence_directory", lambda: tmp_path)
    monkeypatch.setattr(intelligence, "load_intelligence_config", config)
    assert intelligence.read_market_intelligence()["status"] == "ready"
    changed = config()
    changed["universe"] = [SPEC, {"symbol": "035420", "name": "NAVER"}]
    monkeypatch.setattr(intelligence, "load_intelligence_config", lambda: changed)
    assert intelligence.read_market_intelligence()["config_changed"] is True
    assert intelligence.read_market_intelligence()["status"] == "stale"
    clock[0] = "2026-10-14T09:00:00+09:00"
    assert intelligence.read_market_intelligence(tmp_path / "latest.json")["status"] == "stale"


def test_replay_keeps_actual_observation_not_backdated_to_evaluation(clock):
    result = report(now="2026-10-08T12:00:00+09:00")
    assert result["replay"] is True
    assert result["evaluation_at"] == "2026-10-08T12:00:00+09:00"
    assert result["generated_at"] == clock[0]
    assert all(row["available_to_system_at"] == clock[0] for row in result["investor_flows"]["records"])


def test_modified_immutable_version_is_not_silently_reused(clock, tmp_path):
    intelligence.save_market_intelligence(report(), tmp_path)
    path = next((tmp_path / "versions").glob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = "tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable evidence"):
        intelligence.save_market_intelligence(report(), tmp_path)


def test_weekend_does_not_expire_same_completed_session_after_36_hours(clock, tmp_path):
    clock[0] = "2026-10-17T08:30:00+09:00"
    dates = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    intelligence.save_market_intelligence(report(Transport(flow=flows(dates))), tmp_path)
    clock[0] = "2026-10-19T08:00:00+09:00"
    result = intelligence.read_market_intelligence(tmp_path / "latest.json")
    assert result["expected_session"] == result["read_expected_session"] == "2026-10-16"
    assert result["status"] == "ready" and result["is_stale"] is False
    clock[0] = "2026-10-20T08:00:00+09:00"
    assert intelligence.read_market_intelligence(tmp_path / "latest.json")["status"] == "stale"

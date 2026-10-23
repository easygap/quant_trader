from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import daily_observation as daily


def setup(monkeypatch):
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024 ** 3))


def test_missing_local_settings_uses_default_market_close(monkeypatch, tmp_path):
    from config import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    assert not (tmp_path / "settings.yaml").exists()
    assert daily._expected_session(datetime(2026, 10, 13, 16, 30, tzinfo=daily.KST)) == "2026-10-13"


def test_close_failure_does_not_block_research_and_status_is_not_green(monkeypatch, tmp_path):
    setup(monkeypatch)
    run = Mock(side_effect=[(2, {"status": "partial", "issues": ["quote_missing"]}), (0, {"status": "completed"})])
    monkeypatch.setattr(daily, "_run_child", run)
    result = daily.run_daily_observation(tmp_path)
    assert result["status"] == "attention"
    assert result["jobs"]["research"]["status"] == "completed"
    assert "collect_basket_closes.py" in run.call_args_list[0].args[0][0]
    assert "research_operations.py" in run.call_args_list[1].args[0][0]
    assert "--force" in run.call_args_list[1].args[0]
    assert result["orders_enabled"] is False
    assert len(list((tmp_path / "runs").glob("*.json"))) == 1


def test_before_close_is_waiting_not_a_saved_close(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(daily, "_run_child", Mock(side_effect=[(0, {"status": "awaiting_close"}), (0, {"status": "completed"})]))
    result = daily.run_daily_observation(tmp_path)
    assert result["status"] == result["jobs"]["close"]["status"] == "awaiting_close"


def test_low_disk_never_starts_children(monkeypatch, tmp_path):
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    run = Mock()
    monkeypatch.setattr(daily, "_run_child", run)
    assert daily.run_daily_observation(tmp_path)["status"] == "attention"
    run.assert_not_called()


def test_timeout_and_error_do_not_store_provider_secret(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(daily, "_run_child", Mock(side_effect=[subprocess.TimeoutExpired("provider?secret=HIDDEN", 1), RuntimeError("token=HIDDEN")]))
    result = daily.run_daily_observation(tmp_path)
    assert result["jobs"]["close"]["status"] == "timeout"
    assert "HIDDEN" not in json.dumps(result)


def test_read_only_missing_and_interrupted_state(tmp_path):
    assert daily.read_daily_observation(tmp_path / "missing")["status"] == "not_started"
    assert not (tmp_path / "missing").exists()
    now = datetime.now(timezone.utc)
    daily._save({"schema_version": 1, "status": "running", "updated_at": (now - timedelta(minutes=21)).isoformat(), "jobs": {}}, tmp_path)
    assert daily.read_daily_observation(tmp_path, now=now)["status"] == "interrupted"


def test_weekend_keeps_friday_expected_close_and_monday_is_due_only_after_schedule():
    def kst(value):
        return datetime.fromisoformat(value).replace(tzinfo=daily.KST)
    assert daily._expected_session(kst("2026-10-17T17:00")) == "2026-10-16"
    assert daily._expected_session(kst("2026-10-19T08:30")) == "2026-10-16"
    assert daily._expected_session(kst("2026-10-19T16:29")) == "2026-10-16"
    assert daily._expected_session(kst("2026-10-19T16:30")) == "2026-10-19"


def early_completed_report():
    # Mirrors the October 22 run: a confirmed close at 16:27, before the 16:30 due time.
    return {
        "schema_version": 1, "status": "attention", "updated_at": "2026-10-22T16:27:44+09:00",
        "expected_close_session": "2026-10-21", "issues": [],
        "jobs": {"close": {
            "status": "completed", "data_status": "completed", "returncode": 0,
            "result": {"expected_session": "2026-10-22", "last_completed_session": "2026-10-22",
                       "coverage": {"session": "2026-10-22", "status": "complete", "expected": 2,
                                    "present": 2, "missing": 0, "baskets": [
                                        {"basket": name, "status": "present", "close_count": 1,
                                         "latest_close_session": "2026-10-22"}
                                        for name in ("hold", "pocket")]}}},
                 "research": {"status": "attention"}},
    }


def test_early_close_remains_current_after_schedule_without_rewriting_saved_report(tmp_path, monkeypatch):
    report = early_completed_report()
    daily._save(report, tmp_path)
    original = (tmp_path / "latest.json").read_bytes()
    monkeypatch.setattr(daily, "_run_child", Mock(side_effect=AssertionError("read must not collect")))

    for stamp in ("2026-10-22T16:29:00+09:00", "2026-10-22T16:31:00+09:00", "2026-10-23T08:30:00+09:00"):
        result = daily.read_daily_observation(tmp_path, now=datetime.fromisoformat(stamp))
        assert result["status"] == "attention"  # A separate research failure remains visible.
        assert result["expected_close_session"] == "2026-10-22"
        assert result["jobs"] == report["jobs"]
    assert (tmp_path / "latest.json").read_bytes() == original

    next_close_due = datetime.fromisoformat("2026-10-23T16:31:00+09:00")
    stale = daily.read_daily_observation(tmp_path, now=next_close_due)
    assert stale["status"] == "stale"
    assert stale["expected_close_session"] == "2026-10-22"


@pytest.mark.parametrize("path,value", [
    (("status",), "attention"),
    (("data_status",), "partial"),
    (("returncode",), 2),
    (("result", "expected_session"), "2026-10-21"),
    (("result", "last_completed_session"), "2026-10-21"),
    (("result", "coverage", "status"), "partial"),
    (("result", "coverage", "session"), "2026-10-21"),
    (("result", "coverage", "expected"), 0),
    (("result", "coverage", "present"), 1),
    (("result", "coverage", "missing"), 1),
    (("result", "coverage", "baskets"), []),
    (("result", "coverage", "baskets", 1, "latest_close_session"), "2026-10-21"),
    (("result", "coverage", "baskets", 1, "close_count"), 0),
])
def test_partial_or_inconsistent_close_cannot_hide_a_missing_session(tmp_path, path, value):
    report = early_completed_report()
    target = report["jobs"]["close"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    daily._save(report, tmp_path)
    now = datetime.fromisoformat("2026-10-22T16:31:00+09:00")
    assert daily.read_daily_observation(tmp_path, now=now)["status"] == "stale"


def test_future_close_date_cannot_advance_observed_session(tmp_path):
    report = json.loads(json.dumps(early_completed_report()).replace("2026-10-22", "2026-10-23"))
    report["updated_at"] = "2026-10-22T16:27:44+09:00"
    daily._save(report, tmp_path)
    now = datetime.fromisoformat("2026-10-22T16:31:00+09:00")
    assert daily.read_daily_observation(tmp_path, now=now)["status"] == "stale"


def test_runner_saves_completed_session_when_it_starts_before_schedule(monkeypatch, tmp_path):
    setup(monkeypatch)
    now = datetime.fromisoformat("2026-10-22T16:27:44+09:00")
    monkeypatch.setattr(daily, "_now", lambda: now)
    close = early_completed_report()["jobs"]["close"]
    monkeypatch.setattr(daily, "_run_child", Mock(side_effect=[
        (0, {"status": "completed", **close["result"]}), (1, {"status": "attention"}),
    ]))
    result = daily.run_daily_observation(tmp_path)
    assert result["expected_close_session"] == "2026-10-22"
    assert result["status"] == "attention"
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8")) == result
    assert json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8")) == result


def test_overlapping_observation_does_not_spawn(monkeypatch, tmp_path):
    from core.runtime_lock import process_runtime_lock
    run = Mock()
    monkeypatch.setattr(daily, "_run_child", run)
    with process_runtime_lock(tmp_path / ".runner.lock") as acquired:
        assert acquired
        assert daily.run_daily_observation(tmp_path)["status"] == "busy"
    run.assert_not_called()


def test_unknown_child_result_is_attention_even_with_zero_exit(monkeypatch, tmp_path):
    setup(monkeypatch)
    monkeypatch.setattr(daily, "_run_child", Mock(return_value=(0, {"status": "unknown"})))
    assert daily.run_daily_observation(tmp_path)["status"] == "attention"


def test_reader_does_not_initialize_calendar_or_call_network(monkeypatch, tmp_path):
    from core import trading_hours
    now = datetime.now(timezone.utc)
    daily._save({"schema_version": 1, "status": "completed", "updated_at": now.isoformat(),
                 "expected_close_session": daily._expected_session(now), "jobs": {}}, tmp_path)
    monkeypatch.setattr(trading_hours, "TradingHours", Mock(side_effect=AssertionError("calendar constructor may fetch")))
    assert daily.read_daily_observation(tmp_path, now=now)["status"] == "completed"


def test_timeout_stops_owned_grandchild(tmp_path):
    import pytest
    pid_file = tmp_path / "child_pid.txt"
    script = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        daily._run_child(["-c", script, str(pid_file)], 2)
    pid = int(pid_file.read_text())
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                assert ctypes.windll.kernel32.WaitForSingleObject(handle, 2000) == 0
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    else:
        # A killed child can remain a zombie until the OS reaps it.
        state = Path(f"/proc/{pid}/stat")
        assert not state.exists() or state.read_text().split()[2] == "Z"

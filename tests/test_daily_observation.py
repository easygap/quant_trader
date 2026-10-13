from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from core import daily_observation as daily


def setup(monkeypatch):
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024 ** 3))


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

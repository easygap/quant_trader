from datetime import datetime, timedelta, timezone
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from core import research_operations as ops


def configure(monkeypatch):
    monkeypatch.delenv("QUANT_RESEARCH_AUTOMATION_DISABLED", raising=False)
    monkeypatch.setattr(ops, "_config", lambda: {"enabled": True, "retry_minutes": 60, "stale_hours": 36, "jobs": {}})
    monkeypatch.setattr(ops, "_data_status", lambda name: "collecting" if name == "paper_validation" else "ready")
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024 ** 3))


def test_missing_local_settings_uses_default_market_close(monkeypatch, tmp_path):
    from config import config_loader
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    assert not (tmp_path / "settings.yaml").exists()
    period = ops._collection_period(datetime(2026, 10, 13, 16, tzinfo=ops.KST))
    assert period["phase"] == "post_market" and period["is_trading_day"]


def test_daily_run_persists_and_duplicate_does_not_refetch(monkeypatch, tmp_path):
    configure(monkeypatch)
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    assert ops.run_research_operations(output_dir=tmp_path)["status"] == "completed"
    assert ops.run_research_operations(output_dir=tmp_path)["skipped"] == "already_collected_period"
    assert run.call_count == 3
    assert len(list((tmp_path / "runs").glob("*.json"))) == 1
    assert all(c.kwargs["timeout"] <= 300 and c.kwargs["stdout"] == subprocess.DEVNULL for c in run.call_args_list)


def test_failure_continues_remaining_jobs_without_false_health(monkeypatch, tmp_path):
    configure(monkeypatch)
    run = Mock(side_effect=[subprocess.TimeoutExpired("provider", 1), SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)])
    monkeypatch.setattr(ops.subprocess, "run", run)
    monkeypatch.setattr(ops, "_data_status", lambda name: "unavailable" if name == "intelligence" else "collecting")
    report = ops.run_research_operations(output_dir=tmp_path)
    assert report["status"] == "attention"
    assert report["jobs"]["market"]["status"] == "timeout"
    assert report["jobs"]["intelligence"]["status"] == "attention"
    assert report["jobs"]["paper_validation"]["status"] == "completed"
    assert ops.run_research_operations(output_dir=tmp_path)["skipped"] == "retry_backoff"
    assert run.call_count == 3


def test_killed_child_is_visible_and_read_does_not_create_files(tmp_path):
    assert ops.read_research_operations(tmp_path / "absent")["status"] == "not_started"
    assert not (tmp_path / "absent").exists()
    now = datetime.now(timezone.utc)
    ops._save({"schema_version": 1, "status": "running", "jobs": {}, "started_at": (now - timedelta(minutes=17)).isoformat(), "updated_at": (now - timedelta(minutes=16)).isoformat()}, tmp_path)
    assert ops.read_research_operations(tmp_path, now=now)["status"] == "interrupted"


def test_concurrent_process_cannot_run_duplicate_jobs(monkeypatch, tmp_path):
    from core.runtime_lock import process_runtime_lock
    configure(monkeypatch)
    run = Mock()
    monkeypatch.setattr(ops.subprocess, "run", run)
    with process_runtime_lock(tmp_path / ".collection.lock") as acquired:
        assert acquired
        assert ops.run_research_operations(output_dir=tmp_path)["status"] == "busy"
    run.assert_not_called()


def test_launch_returns_immediately_and_disable_prevents_spawn(monkeypatch):
    configure(monkeypatch)
    spawn = Mock(return_value=SimpleNamespace(pid=12, wait=Mock()))
    monkeypatch.setattr(ops.subprocess, "Popen", spawn)
    assert ops.launch_research_operations("rebalance") == {"status": "launched", "pid": 12}
    monkeypatch.setenv("QUANT_RESEARCH_AUTOMATION_DISABLED", "true")
    assert ops.launch_research_operations()["status"] == "disabled"
    assert spawn.call_count == 1


def test_authenticated_error_urls_never_reach_saved_status(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.setattr(ops.subprocess, "run", Mock(side_effect=RuntimeError("https://provider?api_key=SECRET")))
    report = ops.run_research_operations(output_dir=tmp_path)
    assert report["status"] == "attention"
    assert "SECRET" not in json.dumps(report)


def test_disk_pressure_stops_collection_with_visible_reason(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    run = Mock()
    monkeypatch.setattr(ops.subprocess, "run", run)
    report = ops.run_research_operations(output_dir=tmp_path)
    assert report["status"] == "attention"
    assert all(job["status"] == "disk_pressure" for job in report["jobs"].values())
    run.assert_not_called()


def test_bad_start_record_does_not_permanently_block_recovery(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.setattr(ops.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0)))
    for started in ("broken", (datetime.now(timezone.utc) + timedelta(days=100)).isoformat()):
        ops._save({"schema_version": 1, "status": "completed", "jobs": {}, "started_at": started, "updated_at": datetime.now(timezone.utc).isoformat()}, tmp_path)
        assert ops.read_research_operations(tmp_path)["status"] == "unavailable"
        assert ops.run_research_operations(output_dir=tmp_path)["status"] == "completed"


def test_operations_http_read_does_not_collect(monkeypatch):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from monitoring import operations_dashboard as dashboard
    from monitoring.web_dashboard import create_app
    monkeypatch.setattr(dashboard, "get_operations_json", lambda: {"operations": {"status": "not_started"}})
    monkeypatch.setattr(ops, "launch_research_operations", Mock(side_effect=AssertionError("collection forbidden")))
    async def run():
        async with TestClient(TestServer(create_app())) as client:
            response = await client.get("/api/research/operations")
            assert response.status == 200
            assert (await response.json())["operations"]["status"] == "not_started"
    asyncio.run(run())


def test_cli_partial_failure_returns_nonzero_with_saved_detail(monkeypatch, capsys):
    from tools import research_operations as cli
    monkeypatch.setattr(cli.sys, "argv", ["research_operations.py", "--status"])
    monkeypatch.setattr(cli, "read_research_operations", lambda: {
        "status": "attention", "jobs": {"intelligence": {"status": "failed", "data_status": "partial"}}
    })
    monkeypatch.setattr(cli, "run_research_operations", Mock(side_effect=AssertionError("read-only status")))
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["jobs"]["intelligence"]["data_status"] == "partial"


def clock_at(monkeypatch, value):
    clock = [datetime.fromisoformat(value)]
    monkeypatch.setattr(ops, "_now", lambda: clock[0])
    return clock


def test_morning_success_never_suppresses_settled_close_phase(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = clock_at(monkeypatch, "2026-10-13T10:30:00+09:00")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    first = ops.run_research_operations(output_dir=tmp_path)
    assert first["collection_period"]["phase"] == "intraday"
    clock[0] = datetime.fromisoformat("2026-10-13T15:35:00+09:00")
    assert ops.run_research_operations(output_dir=tmp_path)["skipped"] == "already_collected_period"
    clock[0] = datetime.fromisoformat("2026-10-13T16:30:00+09:00")
    assert ops.read_research_operations(tmp_path)["status"] == "stale"
    second = ops.run_research_operations(output_dir=tmp_path)
    assert second["collection_period"]["phase"] == "post_market"
    assert second["status"] == "completed"
    assert second["jobs"]["market"]["reused"] is True
    assert not second["jobs"]["paper_validation"].get("reused")
    assert run.call_count == 5  # Valid T-1 prices reused, disclosures and paper rerun.
    assert ops.run_research_operations(output_dir=tmp_path)["skipped"] == "already_collected_period"


def test_phase_change_bypasses_previous_phase_retry_backoff(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = clock_at(monkeypatch, "2026-10-13T15:45:00+09:00")
    monkeypatch.setattr(ops, "_data_status", lambda name: "partial" if name == "intelligence" else "observing" if name == "paper_validation" else "ready")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    assert ops.run_research_operations(output_dir=tmp_path)["status"] == "attention"
    clock[0] = datetime.fromisoformat("2026-10-13T16:10:00+09:00")
    report = ops.run_research_operations(output_dir=tmp_path)
    assert "skipped" not in report
    assert report["jobs"]["paper_validation"]["status"] == "completed"
    assert run.call_count == 5


def test_partial_dart_retry_reuses_healthy_jobs_and_does_not_block_paper(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = clock_at(monkeypatch, "2026-10-13T10:00:00+09:00")
    monkeypatch.setattr(ops, "_data_status", lambda name: "partial" if name == "intelligence" else "observing" if name == "paper_validation" else "ready")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    first = ops.run_research_operations(output_dir=tmp_path)
    assert first["jobs"]["paper_validation"]["status"] == "completed"
    clock[0] = datetime.fromisoformat("2026-10-13T11:01:00+09:00")
    result = ops.run_research_operations(output_dir=tmp_path)
    assert result["status"] == "attention"
    assert result["jobs"]["market"]["reused"] is True
    assert result["jobs"]["paper_validation"]["reused"] is True
    assert run.call_count == 4


def test_forced_close_retry_reassesses_paper_without_reloading_valid_market(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock_at(monkeypatch, "2026-10-13T16:30:00+09:00")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    ops.run_research_operations(output_dir=tmp_path)
    second = ops.run_research_operations(output_dir=tmp_path, force=True)
    assert second["jobs"]["market"]["reused"] is True
    assert not second["jobs"]["paper_validation"].get("reused")
    assert run.call_count == 5


def test_weekend_elapsed_hours_do_not_expire_completed_session(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = clock_at(monkeypatch, "2026-10-17T08:30:00+09:00")
    monkeypatch.setattr(ops.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0)))
    result = ops.run_research_operations(output_dir=tmp_path)
    assert result["collection_period"]["expected_session"] == "2026-10-16"
    clock[0] = datetime.fromisoformat("2026-10-19T08:00:00+09:00")
    assert ops.read_research_operations(tmp_path)["status"] == "completed"
    clock[0] = datetime.fromisoformat("2026-10-19T16:30:00+09:00")
    assert ops.read_research_operations(tmp_path)["status"] == "stale"


def test_trading_holiday_keeps_last_session_but_new_session_marks_stale(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = clock_at(monkeypatch, "2026-10-09T08:30:00+09:00")  # Hangul Day.
    monkeypatch.setattr(ops.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0)))
    result = ops.run_research_operations(output_dir=tmp_path)
    assert result["collection_period"]["phase"] == "closed_day"
    assert result["collection_period"]["expected_session"] == "2026-10-08"
    clock[0] = datetime.fromisoformat("2026-10-12T08:00:00+09:00")
    assert ops.read_research_operations(tmp_path)["status"] == "completed"
    clock[0] = datetime.fromisoformat("2026-10-13T08:00:00+09:00")
    assert ops.read_research_operations(tmp_path)["status"] == "stale"


def test_changed_child_evidence_does_not_skip_already_completed_period(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock_at(monkeypatch, "2026-10-13T10:30:00+09:00")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(ops.subprocess, "run", run)
    ops.run_research_operations(output_dir=tmp_path)
    monkeypatch.setattr(ops, "_data_status", lambda name: "stale" if name == "market" else "observing" if name == "paper_validation" else "ready")
    result = ops.run_research_operations(output_dir=tmp_path)
    assert "skipped" not in result
    assert not result["jobs"]["market"].get("reused")
    assert run.call_count == 4

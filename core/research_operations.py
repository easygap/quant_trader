"""Durable daily collection, isolated from the order loop and operating DB writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from zoneinfo import ZoneInfo

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
COMMANDS = {
    "market": ("market_research.py",),
    "intelligence": ("market_intelligence.py",),
    "paper_validation": ("paper_validation.py", "collect"),
}


def operations_directory():
    return Path(os.environ.get("QUANT_RESEARCH_OPERATIONS_DIR", str(ROOT / "data" / "research_operations")))


def _config():
    data = yaml.safe_load((ROOT / "config" / "research_operations.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid research operations configuration")
    return data


def _now():
    return datetime.now(timezone.utc)


def _collection_period(now):
    """Allow an independent settled-close run, preserving T-1 price evidence."""
    from config.config_loader import load_yaml
    from core.market_research import _calendar, _sessions, expected_session

    local = now.astimezone(KST)
    day = local.date().isoformat()
    expected = expected_session(local)  # Also validates the calendar year.
    is_session = bool(len(_sessions(day, day, _calendar())))
    settings = load_yaml(str(ROOT / "config" / "settings.yaml"))
    close = datetime.strptime(settings.get("trading", {}).get("market_close", "15:30"), "%H:%M").time()
    settled = datetime.combine(local.date(), close, tzinfo=KST) + timedelta(minutes=30)
    phase = "post_market" if is_session and local >= settled else "intraday" if is_session else "closed_day"
    return {"key": f"{day}:{phase}", "phase": phase, "is_trading_day": is_session,
            "expected_session": expected, "market_data_policy": "previous_session_only"}


def _config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _save(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(directory / "latest.json")
    finally:
        temporary.unlink(missing_ok=True)


def read_research_operations(output_dir=None, *, now=None):
    directory = Path(output_dir) if output_dir is not None else operations_directory()
    try:
        report = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("invalid research report")
        updated = datetime.fromisoformat(report["updated_at"])
        started = datetime.fromisoformat(report["started_at"])
        if (report.get("schema_version") != 1 or updated.tzinfo is None or started.tzinfo is None
                or started > updated or report.get("status") not in {"running", "completed", "attention", "disabled"}
                or not isinstance(report.get("jobs"), dict)
                or any(not isinstance(job, dict) for job in report["jobs"].values())):
            raise ValueError("invalid research run")
        current = now or _now()
        age = current - updated
        if age < timedelta(minutes=-5):
            return {"status": "invalid", "issues": ["실행 기록 시각이 미래입니다."]}
        config = _config()
        period = _collection_period(current)
        captured = report.get("collection_period") or _collection_period(started)
        report["read_collection_period"] = period
        report["config_changed"] = report.get("config_hash") is not None and report["config_hash"] != _config_hash(config)
        phase_missing = period["phase"] == "post_market" and captured["key"] != period["key"]
        session_missing = captured["expected_session"] != period["expected_session"]
        # Wall-clock age alone makes valid Friday observations look stale over a
        # weekend/holiday. Check required sessions and the settled-close phase.
        if report.get("status") == "running" and age > timedelta(minutes=15):
            report = {**report, "status": "interrupted", "issues": ["수집 프로세스의 완료 기록이 없습니다."]}
        elif report.get("status") != "running" and (phase_missing or session_missing or report["config_changed"]):
            issues = []
            if phase_missing:
                issues.append("장마감 확정 시간 이후의 수집 실행 기록이 없습니다.")
            if session_missing:
                issues.append("최신 완료 거래일을 대상으로 한 수집 실행 기록이 없습니다.")
            if report["config_changed"]:
                issues.append("자동 수집 설정이 마지막 실행 이후 변경되었습니다.")
            report = {**report, "status": "stale", "issues": issues}
        report["automatic_enabled"] = bool(config.get("enabled")) and os.environ.get("QUANT_RESEARCH_AUTOMATION_DISABLED", "").lower() not in {"1", "true", "yes"}
        return report
    except FileNotFoundError:
        return {"status": "not_started", "issues": ["아직 자동 수집 실행 기록이 없습니다."]}
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError):
        return {"status": "unavailable", "issues": ["자동 수집 기록을 읽을 수 없습니다."]}


def _data_status(name):
    if name == "market":
        from core.market_research import read_market_research
        data = read_market_research()
    elif name == "intelligence":
        from core.market_intelligence import read_market_intelligence
        data = read_market_intelligence()
    else:
        from core.paper_validation import read_paper_validation
        data = read_paper_validation()
    # Collection completion and investment validation are deliberately different.
    return str(data.get("status", "unknown"))


def _healthy_data(name, status):
    return status in ({"ready"} if name != "paper_validation" else {"observing", "inactive", "collecting"})


def run_research_operations(*, force=False, trigger="manual", output_dir=None):
    from core.runtime_lock import process_runtime_lock

    config = _config()
    if not config.get("enabled", True) or os.environ.get("QUANT_RESEARCH_AUTOMATION_DISABLED", "").lower() in {"1", "true", "yes"}:
        return {"status": "disabled"}
    directory = Path(output_dir) if output_dir is not None else operations_directory()
    with process_runtime_lock(directory / ".collection.lock", label="연구 자료 수집") as acquired:
        if not acquired:
            return {"status": "busy"}
        now = _now()
        period = _collection_period(now)
        previous = read_research_operations(directory, now=now)
        previous_period = None
        if previous.get("started_at"):
            started = datetime.fromisoformat(previous["started_at"])
            if started.tzinfo is None:
                raise ValueError("invalid collection start time")
            previous_period = previous.get("collection_period") or _collection_period(started)
            same_period = previous_period["key"] == period["key"]
            fresh_completed = previous.get("status") == "completed" and all(
                not config.get("jobs", {}).get(name, {}).get("enabled", True) or _healthy_data(name, _data_status(name))
                for name in COMMANDS)
            if not force and same_period and fresh_completed:
                return {**previous, "skipped": "already_collected_period"}
            if not force and same_period and previous.get("status") != "completed" and not previous.get("config_changed") and now - started < timedelta(minutes=float(config.get("retry_minutes", 60))):
                return {**previous, "skipped": "retry_backoff"}
        report = {"schema_version": 1, "run_id": uuid.uuid4().hex, "trigger": trigger,
                  "started_at": now.isoformat(), "updated_at": now.isoformat(),
                  "status": "running", "research_only": True, "jobs": {}, "issues": [],
                  "collection_period": period, "config_hash": _config_hash(config)}
        _save(report, directory)
        for name, command in COMMANDS.items():
            setting = config.get("jobs", {}).get(name, {})
            if not setting.get("enabled", True):
                report["jobs"][name] = {"status": "disabled"}
                continue
            job = {"status": "running", "started_at": _now().isoformat()}
            report["jobs"][name] = job
            report["updated_at"] = _now().isoformat()
            _save(report, directory)
            try:
                old_job = previous.get("jobs", {}).get(name, {})
                # DART/auth failure must not repeatedly download healthy prices or
                # suppress paper observation. A new phase still reruns disclosure
                # and paper jobs, while unchanged T-1 price evidence is reusable.
                reusable_period = previous_period and (previous_period["key"] == period["key"] or name == "market")
                if (not force or name == "market") and not previous.get("config_changed") and old_job.get("status") == "completed" and reusable_period:
                    data_status = _data_status(name)
                    if _healthy_data(name, data_status):
                        job.update(status="completed", data_status=data_status, reused=True,
                                   source_completed_at=old_job.get("source_completed_at") or old_job.get("completed_at"),
                                   completed_at=_now().isoformat())
                        report["updated_at"] = job["completed_at"]
                        _save(report, directory)
                        continue
                if shutil.disk_usage(directory).free < float(config.get("minimum_free_mb", 256)) * 1024 * 1024:
                    job.update(status="disk_pressure", issue="자료 저장 공간이 부족하여 수집을 보류했습니다.")
                    job["completed_at"] = _now().isoformat()
                    report["updated_at"] = job["completed_at"]
                    _save(report, directory)
                    continue
                result = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / command[0]), *command[1:]],
                    cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=max(5, min(300, float(setting.get("timeout_seconds", 150)))),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
                )
                job["returncode"] = result.returncode
                job["status"] = "completed" if result.returncode == 0 else "failed"
                job["data_status"] = _data_status(name)
                if str(job["data_status"]).lower() in {"partial", "unavailable", "stale", "invalid", "error", "failed", "attention", "data_gaps", "corrupt", "not_started", "unknown"}:
                    job["status"] = "attention" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                job.update(status="timeout", issue="자료 수집 시간 제한 초과")
            except Exception as exc:
                # Exception messages can contain authenticated provider URLs.
                job.update(status="failed", issue=type(exc).__name__)
            job["completed_at"] = _now().isoformat()
            report["updated_at"] = job["completed_at"]
            _save(report, directory)
        report["status"] = "completed" if all(j["status"] in {"completed", "disabled"} for j in report["jobs"].values()) else "attention"
        report["updated_at"] = _now().isoformat()
        _save(report, directory)
        history = directory / "runs"
        history.mkdir(exist_ok=True)
        with (history / f"{report['run_id']}.json").open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return report


def launch_research_operations(trigger="runtime"):
    """Return immediately; the child owns locking, retry rules and durable status."""
    try:
        if os.environ.get("QUANT_RESEARCH_AUTOMATION_DISABLED", "").lower() in {"1", "true", "yes"} or not _config().get("enabled", True):
            return {"status": "disabled"}
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "research_operations.py"), "--trigger", trigger],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        # Reap while a long-running scheduler stays alive; a one-shot CLI may exit.
        threading.Thread(target=process.wait, daemon=True).start()
        return {"status": "launched", "pid": process.pid}
    except Exception as exc:
        logger.warning("연구 자료 자동 수집 시작 실패: {}", type(exc).__name__)
        return {"status": "failed", "issue": type(exc).__name__}

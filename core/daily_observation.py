"""Bounded daily observation runner: close valuation first, research second.

No order or deposit commands belong here. Child output is structured JSON only;
provider exceptions and stderr are never copied to the durable status report.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")


def _directory(output_dir=None):
    return Path(output_dir or os.environ.get("QUANT_DAILY_OBSERVATION_DIR") or ROOT / "data/daily_observation").resolve()


def _now():
    return datetime.now(timezone.utc)


def _expected_session(now):
    from config.config_loader import load_settings
    from core.market_research import _calendar, _sessions, expected_session
    calendar = _calendar()
    expected_session(now)  # Fail closed if the locally saved calendar is unknown.
    settings = load_settings()
    market_close = datetime.strptime(settings.get("trading", {}).get("market_close", "15:30"), "%H:%M").time()
    local = now.astimezone(KST).replace(tzinfo=None)
    # The scheduled close run starts one hour after the regular session ends.
    due = datetime.combine(local.date(), market_close) + timedelta(hours=1)
    day = local.date() if local >= due else local.date() - timedelta(days=1)
    while not len(_sessions(day.isoformat(), day.isoformat(), calendar)):
        day -= timedelta(days=1)
    return day.isoformat()


def _save(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(directory / "latest.json")
    finally:
        temporary.unlink(missing_ok=True)


def _checked_close_session(report):
    """Honor an early successful close without rewriting its saved attempt time."""
    expected = report.get("expected_close_session", "")
    try:
        job = report["jobs"]["close"]
        result = job["result"]
        coverage = result["coverage"]
        session = result["last_completed_session"]
        count = coverage["expected"]
        if (job["status"] == job["data_status"] == "completed" and job["returncode"] == 0
                and session == result["expected_session"] == coverage["session"]
                and coverage["status"] == "complete"
                and type(count) is int and count > 0
                and coverage["present"] == count and coverage["missing"] == 0
                and len(coverage["baskets"]) == count
                and all(row["status"] == "present" and row["close_count"] > 0
                        and row["latest_close_session"] == session for row in coverage["baskets"])
                and date.fromisoformat(session).isoformat() == session
                and session <= datetime.fromisoformat(report["updated_at"]).astimezone(KST).date().isoformat()):
            return max(expected, session)
    except (KeyError, TypeError, ValueError):
        pass  # Incomplete/old child reports cannot establish a newer close.
    return expected


def read_daily_observation(output_dir=None, *, now=None):
    """Read saved execution status without starting a collector or opening a DB."""
    try:
        report = json.loads((_directory(output_dir) / "latest.json").read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(report["updated_at"])
        current = now or _now()
        if (report.get("schema_version") != 1 or stamp.tzinfo is None
                or report.get("status") not in {"running", "completed", "attention", "disk_pressure", "awaiting_close"}
                or not isinstance(report.get("jobs"), dict) or stamp > current + timedelta(minutes=5)):
            raise ValueError("invalid daily observation report")
        if report["status"] == "running" and current - stamp > timedelta(minutes=20):
            return {**report, "status": "interrupted", "issues": ["일일 관측 실행의 완료 기록이 없습니다."]}
        checked_session = _checked_close_session(report)
        if checked_session < _expected_session(current):
            return {**report, "expected_close_session": checked_session,
                    "status": "stale", "issues": ["새 거래일의 종가 관측 실행이 필요합니다."]}
        return {**report, "expected_close_session": checked_session}
    except FileNotFoundError:
        return {"status": "not_started", "issues": ["아직 종가·수집 통합 실행 기록이 없습니다."]}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "unavailable", "issues": ["일일 관측 실행 기록을 읽을 수 없습니다."]}


def _run_child(command, timeout):
    process = subprocess.Popen([sys.executable, *command], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt",
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The research CLI itself spawns provider children. Kill this owned tree,
        # not just its parent, before releasing the daily runner's process lock.
        if os.name == "nt":
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=15)
        raise
    data = json.loads(stdout)
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        raise ValueError("invalid collector result")
    return process.returncode, data


def run_daily_observation(output_dir=None):
    from core.runtime_lock import process_runtime_lock
    directory = _directory(output_dir)
    with process_runtime_lock(directory / ".runner.lock", label="일일 종가·자료 관측") as acquired:
        if not acquired:
            return {"status": "busy", "issues": ["다른 일일 관측 실행이 진행 중입니다."]}
        now = _now()
        deadline = time.monotonic() + 900
        report = {"schema_version": 1, "run_id": uuid.uuid4().hex, "status": "running",
                  "started_at": now.isoformat(), "updated_at": now.isoformat(),
                  "expected_close_session": _expected_session(now), "orders_enabled": False,
                  "jobs": {}, "issues": []}
        _save(report, directory)
        commands = (
            ("close", [str(ROOT / "tools/collect_basket_closes.py"), "--json"], 240),
            ("research", [str(ROOT / "tools/research_operations.py"), "--force", "--trigger", "daily_observation"], 720),
        )
        for name, command, timeout in commands:
            job = {"status": "running", "started_at": _now().isoformat()}
            report["jobs"][name] = job
            report["updated_at"] = _now().isoformat()
            _save(report, directory)
            if shutil.disk_usage(directory).free < 256 * 1024 * 1024:
                job.update(status="disk_pressure", issue="저장 공간 부족으로 관측을 보류했습니다.")
            else:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("daily_observation", 900)
                    code, data = _run_child(command, min(timeout, remaining))
                    job.update(status="completed" if code == 0 else "attention", returncode=code,
                               data_status=data["status"])
                    if name == "close":
                        job["result"] = {key: data[key] for key in ("expected_session", "last_completed_session", "baskets", "coverage", "issues") if key in data}
                    else:
                        job["result"] = {"jobs": data.get("jobs", {}), "issues": data.get("issues", [])}
                    allowed = {"completed", "awaiting_close"} if name == "close" else {"completed"}
                    if data["status"] not in allowed:
                        job["status"] = "attention"
                    elif name == "close" and data["status"] == "awaiting_close" and code == 0:
                        job["status"] = "awaiting_close"
                except subprocess.TimeoutExpired:
                    job.update(status="timeout", issue="관측 실행 시간 제한 초과")
                except Exception as exc:
                    job.update(status="failed", issue=type(exc).__name__)
            job["completed_at"] = _now().isoformat()
            report["updated_at"] = job["completed_at"]
            _save(report, directory)
        report["status"] = "completed" if all(job["status"] in {"completed", "awaiting_close"} for job in report["jobs"].values()) else "attention"
        if report["status"] == "completed" and report["jobs"]["close"]["status"] == "awaiting_close":
            report["status"] = "awaiting_close"
        report["updated_at"] = _now().isoformat()
        report["expected_close_session"] = _checked_close_session(report)
        _save(report, directory)
        # One small audit record per attempt; no raw logs or database copies.
        runs = directory / "runs"
        runs.mkdir(exist_ok=True)
        with (runs / f"{report['run_id']}.json").open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return report

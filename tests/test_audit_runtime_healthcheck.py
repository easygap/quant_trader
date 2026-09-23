"""스케줄러 헬스체크 회귀 테스트 (감사: runtime-healthcheck-always-fails).

예전 헬스체크는 SQLAlchemy 2.0에서 문자열 SQL + Session.remove()로 정상 DB에서도
항상 'DB 연결 실패'를, live에서는 새 KISApi의 빈 토큰을 보고 '토큰 없음'을 냈다.
10분마다 critical 알림이 가서 진짜 장애와 구분할 수 없었다.
DB는 conftest가 격리한 임시 DB를 쓰고, KIS 네트워크는 호출하지 않는다.
"""

import sys
import time
import types
from collections import namedtuple
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.kis_api import KISApi, reset_shared_token_cache

VTS_URL = "https://openapivts.koreainvestment.com:29443"
_DiskUsage = namedtuple("_DiskUsage", "total used free")


@pytest.fixture
def healthy_host(monkeypatch):
    """디스크·메모리 검사가 실행 환경 상태에 좌우되지 않게 고정한다."""
    monkeypatch.setattr(
        "core.scheduler.shutil.disk_usage",
        lambda path: _DiskUsage(500 * 1024 ** 3, 100 * 1024 ** 3, 400 * 1024 ** 3),
    )
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.virtual_memory = lambda: SimpleNamespace(percent=10.0)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)


def _scheduler(mode="paper"):
    from config.config_loader import Config
    from core.scheduler import Scheduler

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.strategy_name = "scoring"
    scheduler.config = SimpleNamespace(
        database=Config.get().database,
        trading={"mode": mode},
    )
    return scheduler


@pytest.fixture
def kis_env():
    from config.config_loader import Config

    config = Config.get()
    orig = dict(config._settings.get("kis_api", {}))
    config._settings.setdefault("kis_api", {})
    config._settings["kis_api"].update({
        "app_key": "PS_audit_health_key",
        "app_secret": "audit_secret",
        "account_no": "12345678-01",
        "use_mock": True,
        "mock_url": VTS_URL,
    })
    reset_shared_token_cache()
    yield config
    reset_shared_token_cache()
    config._settings["kis_api"].clear()
    config._settings["kis_api"].update(orig)


def test_paper_healthcheck_is_clean_on_healthy_db(healthy_host):
    assert _scheduler("paper")._run_healthcheck() == []


def test_db_failure_is_reported_exactly(healthy_host, monkeypatch):
    def boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("database.models.get_session", boom)

    assert _scheduler("paper")._run_healthcheck() == ["DB 연결 실패: db unreachable"]


def _no_token_issuance():
    return patch(
        "api.kis_api.requests.post",
        side_effect=AssertionError("healthcheck must not issue a KIS token"),
    )


def test_live_healthcheck_accepts_shared_token_without_issuing(healthy_host, kis_env):
    state = KISApi()._get_token_state()
    with state["lock"]:
        state["access_token"] = "tok-shared"
        state["expires_at"] = datetime.now() + timedelta(hours=1)

    with _no_token_issuance():
        assert _scheduler("live")._run_healthcheck() == []


def test_live_healthcheck_before_first_request_is_not_an_issue(healthy_host, kis_env):
    """첫 KIS 요청 전(토큰 발급 전)은 정상 — 10분마다 '토큰 없음' 오경보를 내지 않는다."""
    with _no_token_issuance():
        assert _scheduler("live")._run_healthcheck() == []


def test_live_healthcheck_reports_failed_issuance(healthy_host, kis_env):
    state = KISApi()._get_token_state()
    with state["lock"]:
        state["error_until"] = time.monotonic() + 30
        state["last_error"] = "HTTP 403 / 접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"

    with _no_token_issuance():
        issues = _scheduler("live")._run_healthcheck()

    assert len(issues) == 1
    assert issues[0].startswith("KIS API 토큰 발급 실패 상태 — HTTP 403")
    assert "재발급 억제" in issues[0]

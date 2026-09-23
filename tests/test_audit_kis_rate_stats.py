"""KIS 사용량 통계 프로세스 공유 회귀 테스트 (감사: runtime-rate-limit-stats-per-instance).

호출 예산(토큰 버킷·분당 윈도우)은 이미 프로세스 공유였지만 총 요청·429·연결
오류 카운터는 인스턴스 필드였다. 스케줄러는 통계를 찍을 때마다 새 KISApi를
만들어서 '누적 0건, 429 0회'만 기록했고, 429 폭주가 로그에 드러나지 않았다.
네트워크는 전부 모킹한다.
"""

import uuid
from unittest.mock import patch

import pytest
import requests

from api.circuit_breaker import CircuitState, get_breaker
from api.kis_api import KISApi, reset_shared_token_cache

VTS_URL = "https://openapivts.koreainvestment.com:29443"


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _reset_breaker():
    b = get_breaker()
    with b._lock:
        b.state = CircuitState.CLOSED
        b.failure_count = 0
        b._half_open_probe_in_flight = False
        b._half_open_probe_started_at = 0.0


@pytest.fixture
def kis_env():
    from config.config_loader import Config

    config = Config.get()
    orig = dict(config._settings.get("kis_api", {}))
    config._settings.setdefault("kis_api", {})
    # 호출 예산 레지스트리는 프로세스 전역이라, 다른 테스트의 누적치와 섞이지 않게
    # 매번 새 app_key로 새 항목을 만든다.
    config._settings["kis_api"].update({
        "app_key": f"PS_audit_stats_{uuid.uuid4().hex[:8]}",
        "app_secret": "audit_secret",
        "account_no": "12345678-01",
        "use_mock": True,
        "mock_url": VTS_URL,
        "max_retry": 3,
    })
    reset_shared_token_cache()
    _reset_breaker()
    yield config
    reset_shared_token_cache()
    _reset_breaker()
    config._settings["kis_api"].clear()
    config._settings["kis_api"].update(orig)


def test_counters_are_shared_across_instances(kis_env):
    """인스턴스 A가 겪은 요청·429가 새 인스턴스 B의 통계에 보인다."""
    responses = [
        _Resp(429, headers={"Retry-After": "1"}),
        _Resp(200, {"rt_cd": "0", "output": {"stck_prpr": "70000"}}),
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        return responses.pop(0)

    token = _Resp(200, {"access_token": "tok-stats", "expires_in": 86400})
    with patch("api.kis_api.requests.post", return_value=token), \
         patch("api.kis_api.requests.get", side_effect=fake_get), \
         patch("api.kis_api.time.sleep", lambda s: None):
        a = KISApi()
        assert a.get_current_price("005930") is not None

    stats = KISApi().get_rate_limit_stats()
    assert stats["total_requests"] == 2
    assert stats["total_429s"] == 1
    assert stats["total_conn_errors"] == 0
    assert stats["requests_last_60s"] == 2


def test_connection_errors_are_counted_process_wide(kis_env):
    """연결 오류 누적도 새 인스턴스에서 그대로 보인다."""
    def fake_get(url, headers=None, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("RST")

    token = _Resp(200, {"access_token": "tok-stats", "expires_in": 86400})
    with patch("api.kis_api.requests.post", return_value=token), \
         patch("api.kis_api.requests.get", side_effect=fake_get), \
         patch("api.kis_api.time.sleep", lambda s: None):
        assert KISApi()._request("GET", "/quote", "TR", params={}, max_retries=2) == {}

    assert KISApi().get_rate_limit_stats()["total_conn_errors"] == 2


def test_bare_instance_counts_on_itself():
    """_rate_state가 없는 테스트 더블은 인스턴스 속성으로 센다(공유 상태 오염 없음)."""
    api = object.__new__(KISApi)
    api._total_429s = 0

    assert api._bump_usage_counter("total_429s") == 1
    assert api._bump_usage_counter("total_429s") == 2
    assert api._total_429s == 2

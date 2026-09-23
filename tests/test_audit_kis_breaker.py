"""서킷 브레이커 락·probe 회귀 테스트 (감사: runtime-breaker-alert-io-under-lock).

1) 발동 알림(Discord·SMTP I/O)을 락을 쥔 채 보내면 그동안 모든 KIS 호출이 멈춘다.
2) HALF_OPEN probe가 429/400/401/403 같은 확정 응답을 받으면 성공도 실패도 아니라
   probe 점유가 풀리지 않아 60초 동안 모든 요청이 이유 없이 막혔다.
네트워크는 전부 모킹한다.
"""

import threading
import time
from unittest.mock import patch

import pytest

from api.circuit_breaker import CircuitBreaker, CircuitState, get_breaker
from api.kis_api import KISApi


def _reset(b):
    with b._lock:
        b.state = CircuitState.CLOSED
        b.failure_count = 0
        b.last_failure_time = 0.0
        b._half_open_probe_in_flight = False
        b._half_open_probe_started_at = 0.0


@pytest.fixture(autouse=True)
def _reset_singleton_breaker():
    b = get_breaker()
    _reset(b)
    yield
    _reset(b)


class _BlockingNotifier:
    """send_message가 이벤트가 풀릴 때까지 멈추는 알림(느린 Discord 흉내)."""

    entered = None
    release = None

    def __init__(self, *args, **kwargs):
        pass

    def send_message(self, *args, **kwargs):
        type(self).entered.set()
        type(self).release.wait(timeout=5)


def test_alert_is_sent_after_releasing_the_lock(monkeypatch):
    """발동 알림 전송 중에도 다른 스레드의 can_request()는 즉시 반환된다."""
    _BlockingNotifier.entered = threading.Event()
    _BlockingNotifier.release = threading.Event()
    monkeypatch.setattr("core.notifier.Notifier", _BlockingNotifier)

    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
    for _ in range(4):
        breaker.on_failure()

    tripping = threading.Thread(target=breaker.on_failure, daemon=True)
    tripping.start()
    try:
        assert _BlockingNotifier.entered.wait(timeout=2), "알림 전송이 시작되지 않음"
        result = {}

        def probe():
            started = time.monotonic()
            result["allowed"] = breaker.can_request()
            result["elapsed"] = time.monotonic() - started

        prober = threading.Thread(target=probe, daemon=True)
        prober.start()
        prober.join(timeout=1.0)
        assert not prober.is_alive(), "알림 전송 동안 can_request()가 락에 막힘"
        assert result["elapsed"] < 0.5
        assert result["allowed"] is False
        assert breaker.state == CircuitState.OPEN
    finally:
        _BlockingNotifier.release.set()
        tripping.join(timeout=5)


def test_release_probe_keeps_half_open_and_allows_next_probe():
    """확정 응답 뒤 release_probe()는 닫지 않고(HALF_OPEN 유지) 다음 probe를 곧바로 허용한다."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)
    breaker.state = CircuitState.OPEN
    breaker.last_failure_time = time.monotonic() - 61.0

    assert breaker.can_request() is True
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_request() is False

    breaker.release_probe()

    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_request() is True
    assert breaker.can_request() is False


def test_release_probe_is_noop_when_closed():
    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
    breaker.on_failure()
    breaker.release_probe()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 1


def _bare_api():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api.base_url = "https://example.test"
    api._is_configured = lambda: True
    api._get_headers = lambda tr_id: {}
    api._wait_for_token = lambda: None
    api._backoff_with_jitter = lambda *a, **kw: 0.0
    return api


class _Resp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _open_breaker_ready_for_probe():
    b = get_breaker()
    with b._lock:
        b.state = CircuitState.OPEN
        b.last_failure_time = time.monotonic() - b.recovery_timeout - 1.0
    return b


@pytest.mark.parametrize("status_code", [400, 403])
def test_request_releases_probe_on_definitive_client_error(status_code):
    """HALF_OPEN probe가 400/403을 받으면 다음 요청이 60초를 기다리지 않는다."""
    breaker = _open_breaker_ready_for_probe()
    api = _bare_api()

    with patch("api.kis_api.requests.get", return_value=_Resp(status_code)):
        assert api._request("GET", "/quote", "TR", params={}, max_retries=1) == {}

    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_request() is True


def test_request_retries_after_429_probe_instead_of_blocking():
    """HALF_OPEN probe의 429 뒤 재시도가 '서킷 동작 — 재시도 중단'으로 끝나지 않는다."""
    breaker = _open_breaker_ready_for_probe()
    api = _bare_api()
    responses = [
        _Resp(429, headers={"Retry-After": "1"}),
        _Resp(200, {"rt_cd": "0", "output": {"ok": True}}),
    ]

    with patch("api.kis_api.requests.get", side_effect=lambda *a, **kw: responses.pop(0)), \
         patch("api.kis_api.time.sleep", lambda s: None):
        data = api._request("GET", "/quote", "TR", params={}, max_retries=2)

    assert data == {"rt_cd": "0", "output": {"ok": True}}
    assert breaker.state == CircuitState.CLOSED

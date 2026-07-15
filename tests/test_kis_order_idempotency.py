"""KIS _request 비멱등(주문) 재시도 안전성 회귀 테스트.

핵심: 주문 제출(POST)은 네트워크 응답 유실뿐 아니라 HTTP 429/5xx에서도
브로커가 요청을 처리했는지 단정할 수 없다. idempotent=False면 한 번만 보내고,
체결 여부 불명 예외(KISOrderResponseUnknown)를 던져 상위 재시도 래퍼가
재전송 대신 reconcile 경로로 분기하게 한다.
"""
import time
from unittest.mock import patch

import pytest
import requests

from api.kis_api import KISApi, KISOrderResponseUnknown
from api.circuit_breaker import get_breaker


def _make_api(max_retry=3):
    """__init__을 우회해 _request 검증에 필요한 최소 속성만 세팅."""
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api.base_url = "https://example.test"
    api._is_configured = lambda: True
    api._get_headers = lambda tr_id: {}
    api._wait_for_token = lambda: None
    api._backoff_with_jitter = lambda *a, **kw: 0.0  # 테스트에서 sleep 0
    api._token_error_until = 0.0
    api._total_429s = 0
    api._total_conn_errors = 0
    # max_retry config
    from config.config_loader import Config
    Config._instance = None
    return api


def _reset_breaker_state(b):
    """서킷 브레이커는 프로세스 전역 싱글톤이라 테스트 간 상태를 CLOSED로 되돌린다."""
    from api.circuit_breaker import CircuitState
    b.state = CircuitState.CLOSED
    b.failure_count = 0


@pytest.fixture(autouse=True)
def _reset_breaker():
    b = get_breaker()
    _reset_breaker_state(b)
    yield
    _reset_breaker_state(b)


def test_order_post_not_resubmitted_on_timeout():
    """idempotent=False: Timeout 시 재전송하지 않고 1회 POST 후 체결 불명 예외."""
    api = _make_api()
    calls = {"post": 0}

    def fake_post(*a, **kw):
        calls["post"] += 1
        raise requests.exceptions.Timeout("read timeout")

    with patch("api.kis_api.requests.post", side_effect=fake_post), \
         patch("api.kis_api.requests.get", side_effect=AssertionError("should not GET")):
        with pytest.raises(KISOrderResponseUnknown):
            api._request("POST", "/order", "TR", body={"x": 1}, idempotent=False)

    assert calls["post"] == 1  # 단 한 번만 제출(재전송 없음)


def test_order_post_not_resubmitted_on_connection_error():
    """idempotent=False: ConnectionError(응답 유실 가능)도 재전송 금지하고 체결 불명 예외."""
    api = _make_api()
    calls = {"post": 0}

    def fake_post(*a, **kw):
        calls["post"] += 1
        raise requests.exceptions.ConnectionError("RST")

    with patch("api.kis_api.requests.post", side_effect=fake_post):
        with pytest.raises(KISOrderResponseUnknown):
            api._request("POST", "/order", "TR", body={"x": 1}, idempotent=False)

    assert calls["post"] == 1


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_order_post_not_resubmitted_on_ambiguous_http_status(status_code):
    """비멱등 주문은 429/5xx 응답을 UNKNOWN으로 올리고 단 1회만 POST한다."""
    api = _make_api()
    calls = {"post": 0}

    class FakeResp:
        headers = {"Retry-After": "1"}

        def __init__(self, code):
            self.status_code = code

    def fake_post(*a, **kw):
        calls["post"] += 1
        return FakeResp(status_code)

    with patch("api.kis_api.requests.post", side_effect=fake_post), \
         patch("api.kis_api.time.sleep", side_effect=AssertionError("order must not retry")):
        with pytest.raises(KISOrderResponseUnknown, match=f"HTTP {status_code}"):
            api._request(
                "POST",
                "/order",
                "TR",
                body={"x": 1},
                max_retries=3,
                idempotent=False,
            )

    assert calls["post"] == 1


def test_idempotent_get_still_retries_on_timeout():
    """idempotent=True(기본): GET 조회는 기존처럼 재시도한다."""
    api = _make_api(max_retry=3)
    calls = {"get": 0}

    def fake_get(*a, **kw):
        calls["get"] += 1
        raise requests.exceptions.Timeout("read timeout")

    with patch("api.kis_api.requests.get", side_effect=fake_get):
        result = api._request("GET", "/quote", "TR", params={"x": 1}, max_retries=3)

    assert result == {}
    assert calls["get"] == 3  # 3회 재시도


def test_retry_after_http_date_does_not_crash():
    """429 Retry-After가 HTTP-date 형식이어도 ValueError로 죽지 않는다."""
    api = _make_api(max_retry=2)

    class FakeResp:
        status_code = 429
        headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}

    calls = {"get": 0}

    def fake_get(*a, **kw):
        calls["get"] += 1
        return FakeResp()

    slept = []
    with patch("api.kis_api.requests.get", side_effect=fake_get), \
         patch("api.kis_api.time.sleep", side_effect=lambda s: slept.append(s)):
        result = api._request("GET", "/quote", "TR", params={}, max_retries=2)

    # 죽지 않고 정상 종료(최종 빈 응답), 기본 backoff(5초)로 대기
    assert result == {}
    assert calls["get"] == 2
    assert 5 in slept

"""KIS 5xx 업무 오류 회귀 테스트 (감사: runtime-kis-5xx-business-errors, 조회 경로만).

KIS 게이트웨이는 초당 한도 초과(EGW00201)와 토큰 무효·만료(EGW00121/EGW00123)를
HTTP 500 + JSON 본문으로 돌려줄 수 있다. 예전에는 본문을 보지 않고 전부 서버
장애로 세어, 조회 두 건의 재시도만으로 서킷이 열리고(60초간 손절 SELL까지 차단)
무효 토큰은 로컬 만료 시각까지 갱신되지 않았다.

주문(비멱등) POST는 의도적으로 기존 '응답 불명' 처리를 유지한다 — 5xx에서는
브로커가 주문을 접수했는지 단정할 수 없으므로 재전송 금지(test_kis_order_idempotency).
네트워크는 전부 모킹한다.
"""

from unittest.mock import patch

import pytest
import requests

from api.circuit_breaker import CircuitState, get_breaker
from api.kis_api import KISApi, KISOrderResponseUnknown, reset_shared_token_cache

VTS_URL = "https://openapivts.koreainvestment.com:29443"
_OK_BODY = {"rt_cd": "0", "output": {"stck_prpr": "70000"}}


class _Resp:
    def __init__(self, status_code, payload=None, headers=None, json_error=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400
        self.text = "<html>Internal Server Error</html>" if json_error else ""
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _reset(b):
    with b._lock:
        b.state = CircuitState.CLOSED
        b.failure_count = 0
        b._half_open_probe_in_flight = False
        b._half_open_probe_started_at = 0.0


@pytest.fixture(autouse=True)
def _isolate_breaker():
    b = get_breaker()
    _reset(b)
    yield
    _reset(b)


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


def test_get_rate_limit_body_retries_without_breaker_failure():
    """GET 500 + EGW00201은 429처럼 대기 후 재시도하고 서킷 실패로 세지 않는다."""
    api = _bare_api()
    responses = [
        _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
        _Resp(200, _OK_BODY),
    ]
    slept = []
    with patch("api.kis_api.requests.get", side_effect=lambda *a, **kw: responses.pop(0)), \
         patch("api.kis_api.time.sleep", side_effect=lambda s: slept.append(s)):
        data = api._request("GET", "/quote", "TR", params={}, max_retries=3)

    assert data == _OK_BODY
    assert get_breaker().failure_count == 0
    assert slept == [1]
    assert api._total_429s == 1


def test_get_rate_limit_bodies_do_not_open_breaker():
    """EGW00201이 연달아 와도(조회 여러 건) 서킷이 열리지 않는다."""
    api = _bare_api()
    busy = _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"})
    with patch("api.kis_api.requests.get", return_value=busy), \
         patch("api.kis_api.time.sleep", lambda s: None):
        for _ in range(3):
            assert api._request("GET", "/quote", "TR", params={}, max_retries=3) == {}

    breaker = get_breaker()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_get_plain_5xx_is_still_a_breaker_failure():
    """본문이 JSON이 아닌 진짜 서버 장애는 기존처럼 서킷 실패로 누적하고 재시도한다."""
    api = _bare_api()
    calls = {"get": 0}

    def fake_get(*a, **kw):
        calls["get"] += 1
        return _Resp(503, json_error=True)

    with patch("api.kis_api.requests.get", side_effect=fake_get), \
         patch("api.kis_api.time.sleep", lambda s: None):
        assert api._request("GET", "/quote", "TR", params={}, max_retries=3) == {}

    assert calls["get"] == 3
    assert get_breaker().failure_count == 3


def test_order_post_with_rate_limit_body_keeps_response_unknown():
    """주문 POST는 본문이 EGW00201이어도 재전송하지 않고 '응답 불명'으로 올린다(의도된 설계)."""
    api = _bare_api()
    calls = {"post": 0}

    def fake_post(*a, **kw):
        calls["post"] += 1
        return _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"})

    with patch("api.kis_api.requests.post", side_effect=fake_post), \
         patch("api.kis_api.time.sleep", side_effect=AssertionError("order must not retry")):
        with pytest.raises(KISOrderResponseUnknown, match="HTTP 500"):
            api._request("POST", "/order", "TR", body={"x": 1}, max_retries=3, idempotent=False)

    assert calls["post"] == 1


@pytest.fixture
def kis_env(monkeypatch):
    from config.config_loader import Config

    config = Config.get()
    orig = dict(config._settings.get("kis_api", {}))
    config._settings.setdefault("kis_api", {})
    config._settings["kis_api"].update({
        "app_key": "PS_audit_5xx_key",
        "app_secret": "audit_secret",
        "account_no": "12345678-01",
        "use_mock": True,
        "mock_url": VTS_URL,
        "max_retry": 3,
    })
    reset_shared_token_cache()
    monkeypatch.setattr(KISApi, "_wait_for_token", lambda self: None)
    monkeypatch.setattr(KISApi, "_notify_auth_failure", lambda self, message: None)
    yield config
    reset_shared_token_cache()
    config._settings["kis_api"].clear()
    config._settings["kis_api"].update(orig)


@pytest.mark.parametrize("msg_cd", ["EGW00121", "EGW00123"])
def test_get_token_error_body_refreshes_token_once(kis_env, msg_cd):
    """GET 500 + 토큰 무효 코드는 서킷 실패가 아니라 토큰 재발급 후 재시도로 간다."""
    post_calls, auth_headers = [], []

    def fake_post(url, json=None, timeout=None, **kw):
        post_calls.append(url)
        return _Resp(200, {"access_token": f"tok-{len(post_calls)}", "expires_in": 86400})

    def fake_get(url, headers=None, params=None, timeout=None):
        auth_headers.append(headers["authorization"])
        if headers["authorization"] == "Bearer tok-1":
            return _Resp(500, {"rt_cd": "1", "msg_cd": msg_cd, "msg1": "유효하지 않은 token 입니다."})
        return _Resp(200, _OK_BODY)

    with patch("api.kis_api.requests.post", side_effect=fake_post), \
         patch("api.kis_api.requests.get", side_effect=fake_get):
        quote = KISApi().get_current_price("005930")

    assert quote is not None and quote["price"] == 70000.0
    assert post_calls == [f"{VTS_URL}/oauth2/tokenP"] * 2
    assert auth_headers == ["Bearer tok-1", "Bearer tok-2"]
    assert get_breaker().failure_count == 0

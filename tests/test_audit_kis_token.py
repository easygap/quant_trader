"""KIS 접근 토큰 프로세스 공유 회귀 테스트 (감사: runtime-kis-token-not-shared).

KIS는 토큰 발급을 1분당 1회로 제한한다(EGW00133). 예전에는 KISApi 인스턴스마다
토큰을 따로 발급해, live 한 사이클(동기화·잔고 요약·매수마다 새 인스턴스)에서
두 번째 발급부터 거절되고 잔고 확인 실패 → live 매수 전면 보류 → 인증 실패 알림
폭주로 이어졌다. 이제 (base_url, app_key)별 공유 토큰 하나를 쓰고, 발급 실패는
공유 60초 쿨다운으로 재발급·알림을 한 번으로 모은다.

네트워크는 전부 모킹한다 — 실제 KIS 엔드포인트를 부르지 않는다.
"""

import json
import time
from unittest.mock import patch

import pytest
import requests

from api.circuit_breaker import CircuitState, get_breaker
from api.kis_api import KISApi, reset_shared_token_cache

VTS_URL = "https://openapivts.koreainvestment.com:29443"
REAL_URL = "https://openapi.koreainvestment.com:9443"

_PRICE_BODY = {
    "rt_cd": "0",
    "output": {
        "stck_prpr": "70000",
        "stck_oprc": "69000",
        "stck_hgpr": "71000",
        "stck_lwpr": "68000",
        "acml_vol": "1000",
        "prdy_ctrt": "1.0",
        "stck_sdpr": "69300",
    },
}


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 400
        self.text = json.dumps(self._payload, ensure_ascii=False)

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
def kis_env(monkeypatch):
    """KIS가 '설정됨'으로 보이게 하고 토큰 캐시·서킷·레이트리밋 대기를 격리한다."""
    from config.config_loader import Config

    config = Config.get()
    orig = dict(config._settings.get("kis_api", {}))
    config._settings.setdefault("kis_api", {})
    config._settings["kis_api"].update({
        "app_key": "PS_audit_token_key",
        "app_secret": "audit_secret",
        "account_no": "12345678-01",
        "use_mock": True,
        "mock_url": VTS_URL,
        "base_url": REAL_URL,
        "max_retry": 2,
    })
    reset_shared_token_cache()
    _reset_breaker()
    monkeypatch.setattr(KISApi, "_wait_for_token", lambda self: None)
    alerts = []
    monkeypatch.setattr(
        KISApi, "_notify_auth_failure", lambda self, message: alerts.append(message)
    )
    yield {"config": config, "alerts": alerts}
    reset_shared_token_cache()
    _reset_breaker()
    config._settings["kis_api"].clear()
    config._settings["kis_api"].update(orig)


def _token_post_factory(post_calls, *, status=200):
    def fake_post(url, json=None, timeout=None, **kwargs):
        post_calls.append(url)
        if status != 200:
            return _Resp(status, {
                "error_code": "EGW00133",
                "error_description": "접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)",
            })
        return _Resp(200, {"access_token": f"tok-{len(post_calls)}", "expires_in": 86400})

    return fake_post


def test_instances_share_one_token_issuance(kis_env):
    """인스턴스 3개가 각각 시세를 조회해도 토큰 발급은 1번이다."""
    post_calls, auth_headers = [], []

    def fake_get(url, headers=None, params=None, timeout=None):
        auth_headers.append(headers["authorization"])
        return _Resp(200, _PRICE_BODY)

    with patch("api.kis_api.requests.post", side_effect=_token_post_factory(post_calls)), \
         patch("api.kis_api.requests.get", side_effect=fake_get):
        for _ in range(3):
            quote = KISApi().get_current_price("005930")
            assert quote is not None and quote["price"] == 70000.0

    assert post_calls == [f"{VTS_URL}/oauth2/tokenP"]
    assert auth_headers == ["Bearer tok-1"] * 3


def test_explicit_authenticate_reuses_valid_shared_token(kis_env):
    """executor 생성·live 시작 사전 발급처럼 authenticate()를 직접 불러도 재발급하지 않는다."""
    post_calls = []
    with patch("api.kis_api.requests.post", side_effect=_token_post_factory(post_calls)):
        first = KISApi()
        assert first.authenticate() is True
        second = KISApi()
        # 새 인스턴스는 생성 시점에 이미 공유 토큰을 미러로 들고 있다(헬스체크·
        # order_executor가 읽는 _access_token 호환).
        assert second._access_token == "tok-1"
        assert second.authenticate() is True

    assert len(post_calls) == 1
    assert second._access_token == first._access_token == "tok-1"


def test_failed_issuance_sets_shared_cooldown_and_alerts_once(kis_env):
    """발급 실패는 공유 쿨다운 — 다른 인스턴스도 재발급·알림·'Bearer None' 요청을 하지 않는다."""
    post_calls, get_calls = [], []

    def fake_get(url, headers=None, params=None, timeout=None):
        get_calls.append(headers.get("authorization"))
        return _Resp(200, _PRICE_BODY)

    with patch(
        "api.kis_api.requests.post",
        side_effect=_token_post_factory(post_calls, status=403),
    ), patch("api.kis_api.requests.get", side_effect=fake_get):
        a, b = KISApi(), KISApi()
        assert a.get_current_price("005930") is None
        assert b.get_current_price("005930") is None
        assert b.authenticate() is False

    assert len(post_calls) == 1
    assert len(kis_env["alerts"]) == 1
    assert "EGW00133" in kis_env["alerts"][0] or "1분당 1회" in kis_env["alerts"][0]
    assert get_calls == []  # 토큰 없이 서버를 때리지 않는다
    status = b.token_status()
    assert status["valid"] is False
    assert status["cooldown_remaining"] > 0
    assert "HTTP 403" in status["last_error"]
    assert a.get_rate_limit_stats()["token_cooldown_active"] is True


def test_success_clears_shared_failure_state(kis_env):
    """쿨다운이 끝난 뒤 다른 인스턴스가 발급에 성공하면 실패 상태가 즉시 사라진다."""
    post_calls = []
    with patch(
        "api.kis_api.requests.post",
        side_effect=_token_post_factory(post_calls, status=403),
    ):
        failing = KISApi()
        assert failing.authenticate() is False

    # 쿨다운 만료를 흉내 낸다(공유 상태의 종료 시각을 과거로).
    failing._get_token_state()["error_until"] = time.monotonic() - 1.0

    with patch("api.kis_api.requests.post", side_effect=_token_post_factory(post_calls)):
        other = KISApi()
        assert other.authenticate() is True

    status = failing.token_status()
    assert status["valid"] is True
    assert status["cooldown_remaining"] == 0.0
    assert status["last_error"] == ""
    assert failing._token_error_until == 0.0


def test_mock_and_real_domains_never_share_a_token(kis_env):
    """모의(VTS)와 실전은 base_url이 달라 토큰을 절대 공유하지 않는다."""
    post_calls = []
    config = kis_env["config"]
    with patch("api.kis_api.requests.post", side_effect=_token_post_factory(post_calls)):
        vts = KISApi()
        assert vts.authenticate() is True
        config._settings["kis_api"]["use_mock"] = False
        real = KISApi()
        assert real._access_token is None
        assert real.authenticate() is True

    assert post_calls == [f"{VTS_URL}/oauth2/tokenP", f"{REAL_URL}/oauth2/tokenP"]
    assert vts._access_token == "tok-1"
    assert real._access_token == "tok-2"


def test_401_refresh_is_single_flight_across_instances(kis_env):
    """401은 거절된 토큰만 폐기하고 한 번 재발급한다. 이미 갱신됐으면 재발급하지 않는다."""
    post_calls, auth_headers = [], []

    def fake_get(url, headers=None, params=None, timeout=None):
        auth_headers.append(headers["authorization"])
        if headers["authorization"] == "Bearer tok-1":
            return _Resp(401, {"rt_cd": "1", "msg1": "기간이 만료된 token 입니다."})
        return _Resp(200, _PRICE_BODY)

    with patch("api.kis_api.requests.post", side_effect=_token_post_factory(post_calls)), \
         patch("api.kis_api.requests.get", side_effect=fake_get):
        a, b = KISApi(), KISApi()
        assert a.authenticate() is True
        assert b.authenticate() is True
        assert a.get_current_price("005930") is not None
        # b는 옛 토큰(tok-1)으로 401을 받은 상황 — 이미 a가 갱신했으므로 발급 없이 채택.
        assert b._acquire_token(rejected_token="tok-1") is True

    assert len(post_calls) == 2
    assert auth_headers == ["Bearer tok-1", "Bearer tok-2"]
    assert b._access_token == "tok-2"


def test_token_status_never_issues_a_token(kis_env):
    """헬스체크용 상태 조회는 발급 요청을 만들지 않는다."""
    with patch(
        "api.kis_api.requests.post",
        side_effect=AssertionError("token_status must not issue a token"),
    ):
        status = KISApi().token_status()

    assert status["has_token"] is False
    assert status["valid"] is False
    assert status["cooldown_remaining"] == 0.0


def test_bare_instance_does_not_touch_shared_registry(kis_env):
    """__init__을 거치지 않은 테스트 더블은 공유 쿨다운을 오염시키지 않는다."""
    bare = object.__new__(KISApi)
    bare._token_error_until = time.monotonic() + 600

    assert KISApi()._token_error_until == 0.0
    assert bare._token_error_until > time.monotonic()

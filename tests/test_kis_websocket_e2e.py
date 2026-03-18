"""
KIS API · WebSocket 모의 E2E 테스트
- 실제 네트워크/증권사 호출 없이, 모킹으로 인증·승인키·연결검증·웹소켓 플로우 검증
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# KIS API 모의 E2E
# ---------------------------------------------------------------------------

class TestKISApiMockE2E:
    """KIS API 인증/approval_key/verify_connection 모의 응답으로 플로우 검증"""

    @pytest.fixture(autouse=True)
    def _kis_config(self):
        """KIS API가 '설정됨'으로 인식되도록 config._settings['kis_api'] 수정 (property 미사용)"""
        from config.config_loader import Config
        config = Config.get()
        mock_kis = {
            "app_key": "PS_test_key_12345",
            "app_secret": "secret_67890",
            "account_no": "12345678-01",
            "use_mock": True,
            "mock_url": "https://openapivts.koreainvestment.com:29443",
            "base_url": "https://openapi.koreainvestment.com:9443",
        }
        orig = dict(config._settings.get("kis_api", {}))
        config._settings.setdefault("kis_api", {})
        config._settings["kis_api"].update(mock_kis)
        yield
        config._settings["kis_api"].clear()
        config._settings["kis_api"].update(orig)

    def test_authenticate_success_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "access_token": "mock_access_token_xyz",
            "expires_in": 86400,
        }
        with patch("api.kis_api.requests.post", return_value=mock_response):
            api = KISApi()
            assert api.authenticate() is True
            assert api._access_token == "mock_access_token_xyz"

    def test_authenticate_failure_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 401
        mock_response.json.return_value = {"error_description": "invalid_client"}
        with patch("api.kis_api.requests.post", return_value=mock_response):
            api = KISApi()
            assert api.authenticate() is False

    def test_get_approval_key_success_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"approval_key": "mock_approval_key_abc"}
        with patch("api.kis_api.requests.post", return_value=mock_response):
            api = KISApi()
            key = api.get_approval_key()
            assert key == "mock_approval_key_abc"

    def test_get_approval_key_failure_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.json.return_value = {"msg": "bad request"}
        with patch("api.kis_api.requests.post", return_value=mock_response):
            api = KISApi()
            assert api.get_approval_key() == ""

    def test_verify_connection_success_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        api = KISApi()
        with patch.object(api, "get_balance", return_value={"cash": 1_000_000, "positions": []}):
            assert api.verify_connection() is True

    def test_verify_connection_failure_with_mock(self, _kis_config):
        from api.kis_api import KISApi
        api = KISApi()
        with patch.object(api, "get_balance", return_value=None):
            assert api.verify_connection() is False


# ---------------------------------------------------------------------------
# WebSocket 연결 플로우 모의 E2E
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebSocketMockE2E:
    """WebSocketHandler connect 플로우: 승인키 모킹 후 연결 시도·구독·disconnect 정상 동작"""

    @pytest.fixture
    def _ws_config(self):
        """웹소켓 핸들러가 '키 설정됨'으로 인식하도록 config._settings 수정"""
        from config.config_loader import Config
        config = Config.get()
        mock_kis = {"app_key": "PS_ws_test", "app_secret": "secret", "use_mock": True}
        orig = dict(config._settings.get("kis_api", {}))
        config._settings.setdefault("kis_api", {})
        config._settings["kis_api"].update(mock_kis)
        yield
        config._settings["kis_api"].clear()
        config._settings["kis_api"].update(orig)

    async def test_connect_exits_cleanly_when_connection_refused(self, _ws_config):
        """연결 거부 시 예외 처리 후 재시도 대기로 진입하고, disconnect 시 루프 종료"""
        from api.websocket_handler import WebSocketHandler
        with patch("api.kis_api.KISApi") as MockKIS:
            MockKIS.return_value.get_approval_key.return_value = "mock_approval"
            MockKIS.return_value._mask_key.return_value = "mock****key"
            with patch("api.websocket_handler.websockets.connect", new_callable=AsyncMock) as mock_connect:
                mock_connect.side_effect = ConnectionRefusedError("refused")
                # 백오프 대기 시간 제거
                with patch("api.websocket_handler.asyncio.sleep", new_callable=AsyncMock):
                    handler = WebSocketHandler()
                    handler._should_reconnect = False  # 첫 실패 후 재시도 없이 종료하려면 루프 1회만
                    task = asyncio.create_task(handler.connect(["005930"]))
                    await asyncio.sleep(0.15)
                    handler._should_reconnect = False
                    await asyncio.sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        assert handler.is_connected is False

    async def test_connect_with_mock_ws_then_disconnect(self, _ws_config):
        """모의 웹소켓 연결 성공 → 구독 요청 → disconnect()로 정상 종료"""
        from api.websocket_handler import WebSocketHandler

        class MockWs:
            def __init__(self):
                self._closed = False
                self.send = AsyncMock()
                self.close = AsyncMock(side_effect=self._do_close)
                self.ping = AsyncMock()

            def _do_close(self):
                self._closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                while not self._closed:
                    await asyncio.sleep(0.02)
                raise Exception("closed")

        class AsyncCtxManager:
            """async with 에서 await 되도록 실제 async context manager"""
            def __init__(self, ws):
                self.ws = ws
            async def __aenter__(self):
                return self.ws
            async def __aexit__(self, *args):
                return None

        mock_ws = MockWs()
        ctx_manager = AsyncCtxManager(mock_ws)

        def fake_connect_sync(*args, **kwargs):
            return ctx_manager

        with patch("api.kis_api.KISApi") as MockKIS:
            MockKIS.return_value.get_approval_key.return_value = "mock_key"
            MockKIS.return_value._mask_key.return_value = "****"
            with patch("api.websocket_handler.websockets.connect", fake_connect_sync):
                handler = WebSocketHandler()
                task = asyncio.create_task(handler.connect(["005930"]))
                await asyncio.sleep(0.25)
                await handler.disconnect()
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        assert handler.is_connected is False

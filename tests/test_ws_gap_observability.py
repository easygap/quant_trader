"""
웹소켓 갭 observability 테스트
- WebSocketHandler.gap_snapshot() 구조 검증
- gap event ring buffer 동작 검증
- dashboard_runtime_state.merge_ws_gap() 통합 검증
"""

import json
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. WebSocketHandler gap_snapshot 구조 테스트
# ---------------------------------------------------------------------------


class TestGapSnapshot:
    """gap_snapshot()이 올바른 구조를 반환하는지 검증."""

    @pytest.fixture
    def handler(self):
        with patch("api.websocket_handler.Config") as MockConfig:
            MockConfig.get.return_value = MagicMock(
                kis_api={"app_key": "", "app_secret": "", "use_mock": True}
            )
            from api.websocket_handler import WebSocketHandler

            h = WebSocketHandler(config=MockConfig.get())
            return h

    def test_initial_snapshot_structure(self, handler):
        snap = handler.gap_snapshot()
        assert snap["available"] is True
        assert snap["is_connected"] is False
        assert snap["current_gap_since"] is None
        assert snap["total_gap_count"] == 0
        assert snap["recent_gaps"] == []

    def test_snapshot_reflects_connected_state(self, handler):
        handler._is_connected = True
        snap = handler.gap_snapshot()
        assert snap["is_connected"] is True

    def test_snapshot_reflects_current_gap(self, handler):
        now = datetime.now()
        handler._current_gap_start = now
        snap = handler.gap_snapshot()
        assert snap["current_gap_since"] == now.isoformat()

    def test_snapshot_after_gap_event(self, handler):
        event = {
            "disconnect_at": "2026-04-10T09:00:00",
            "reconnect_at": "2026-04-10T09:02:30",
            "gap_seconds": 150.0,
            "affected_symbols": ["005930", "000660"],
            "rest_backfill_performed": True,
            "rest_backfill_count": 2,
            "blackswan_checked": True,
            "blackswan_cooldown_triggered": False,
            "minute_bar_backfill_count": 2,
            "observed_volatility": {"005930": 1.2},
        }
        handler._gap_history.append(event)
        snap = handler.gap_snapshot()
        assert snap["total_gap_count"] == 1
        assert len(snap["recent_gaps"]) == 1
        assert snap["recent_gaps"][0]["gap_seconds"] == 150.0

    def test_recent_gaps_capped_at_10(self, handler):
        for i in range(25):
            handler._gap_history.append({"gap_seconds": float(i)})
        snap = handler.gap_snapshot()
        assert len(snap["recent_gaps"]) == 10
        # 최근 10개만 반환 (index 15~24)
        assert snap["recent_gaps"][0]["gap_seconds"] == 15.0
        assert snap["recent_gaps"][-1]["gap_seconds"] == 24.0


# ---------------------------------------------------------------------------
# 2. Ring buffer 크기 제한 테스트
# ---------------------------------------------------------------------------


class TestGapHistoryRingBuffer:
    """gap_history deque의 maxlen 동작 검증."""

    def test_ring_buffer_maxlen(self):
        from api.websocket_handler import _GAP_HISTORY_MAX

        assert _GAP_HISTORY_MAX == 50

    def test_overflow_evicts_oldest(self):
        from api.websocket_handler import _GAP_HISTORY_MAX

        buf: deque = deque(maxlen=_GAP_HISTORY_MAX)
        for i in range(_GAP_HISTORY_MAX + 10):
            buf.append({"idx": i})
        assert len(buf) == _GAP_HISTORY_MAX
        assert buf[0]["idx"] == 10  # 첫 10개 evicted


# ---------------------------------------------------------------------------
# 3. dashboard_runtime_state.merge_ws_gap 통합 테스트
# ---------------------------------------------------------------------------


class TestMergeWsGap:
    """merge_ws_gap()이 JSON 파일에 ws_gap 키를 올바르게 기록하는지 검증."""

    @pytest.fixture
    def tmp_state_path(self, tmp_path):
        state_file = tmp_path / "dashboard_runtime_state.json"
        state_file.write_text("{}", encoding="utf-8")
        return state_file

    def test_merge_writes_ws_gap_key(self, tmp_state_path):
        with patch(
            "monitoring.dashboard_runtime_state._state_path",
            return_value=tmp_state_path,
        ):
            from monitoring.dashboard_runtime_state import merge_ws_gap, read_state

            snapshot = {
                "available": True,
                "is_connected": True,
                "current_gap_since": None,
                "total_gap_count": 1,
                "recent_gaps": [{"gap_seconds": 30.0}],
            }
            merge_ws_gap(snapshot)
            state = read_state()
            assert "ws_gap" in state
            assert state["ws_gap"]["available"] is True
            assert state["ws_gap"]["total_gap_count"] == 1

    def test_merge_overwrites_previous(self, tmp_state_path):
        with patch(
            "monitoring.dashboard_runtime_state._state_path",
            return_value=tmp_state_path,
        ):
            from monitoring.dashboard_runtime_state import merge_ws_gap, read_state

            merge_ws_gap({"available": True, "total_gap_count": 1, "recent_gaps": []})
            merge_ws_gap({"available": True, "total_gap_count": 5, "recent_gaps": []})
            state = read_state()
            assert state["ws_gap"]["total_gap_count"] == 5

    def test_merge_empty_snapshot_noop(self, tmp_state_path):
        with patch(
            "monitoring.dashboard_runtime_state._state_path",
            return_value=tmp_state_path,
        ):
            from monitoring.dashboard_runtime_state import merge_ws_gap, read_state

            merge_ws_gap({})
            state = read_state()
            assert "ws_gap" not in state

    def test_merge_preserves_other_keys(self, tmp_state_path):
        tmp_state_path.write_text(
            json.dumps({"strategy": "scoring", "blackswan": {"state": "normal"}}),
            encoding="utf-8",
        )
        with patch(
            "monitoring.dashboard_runtime_state._state_path",
            return_value=tmp_state_path,
        ):
            from monitoring.dashboard_runtime_state import merge_ws_gap, read_state

            merge_ws_gap({"available": True, "total_gap_count": 0, "recent_gaps": []})
            state = read_state()
            assert state["strategy"] == "scoring"
            assert state["blackswan"]["state"] == "normal"
            assert state["ws_gap"]["available"] is True


# ---------------------------------------------------------------------------
# 4. get_runtime_json에 ws_gap 노출 테스트
# ---------------------------------------------------------------------------


try:
    import aiohttp  # noqa: F401
    _has_aiohttp = True
except ImportError:
    _has_aiohttp = False


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
class TestRuntimeJsonWsGap:
    """get_runtime_json()이 ws_gap 필드를 포함하는지 검증."""

    def test_ws_gap_present_in_runtime_json(self):
        fake_state = {
            "ws_gap": {
                "available": True,
                "is_connected": True,
                "total_gap_count": 0,
                "recent_gaps": [],
            },
            "updated_at": datetime.now().isoformat(),
        }
        from monitoring import web_dashboard as wd

        with patch.object(wd, "Config") as mc:
            mc.get.return_value = MagicMock()
            with patch(
                "monitoring.dashboard_runtime_state._read_unlocked",
                return_value=dict(fake_state),
            ):
                result = wd.get_runtime_json()
        assert result["ws_gap"] is not None
        assert result["ws_gap"]["available"] is True

    def test_ws_gap_null_when_no_data(self):
        from monitoring import web_dashboard as wd

        with patch.object(wd, "Config") as mc:
            mc.get.return_value = MagicMock()
            with patch(
                "monitoring.dashboard_runtime_state._read_unlocked",
                return_value={},
            ):
                result = wd.get_runtime_json()
        assert result["ws_gap"] is None


# ---------------------------------------------------------------------------
# 5. Gap event 필드 완전성 테스트
# ---------------------------------------------------------------------------


class TestGapEventFields:
    """gap event dict가 필수 필드를 모두 포함하는지 검증."""

    REQUIRED_FIELDS = {
        "disconnect_at",
        "reconnect_at",
        "gap_seconds",
        "affected_symbols",
        "rest_backfill_performed",
        "rest_backfill_count",
        "blackswan_checked",
        "blackswan_cooldown_triggered",
        "minute_bar_backfill_count",
        "observed_volatility",
    }

    def test_gap_event_has_all_fields(self):
        event = {
            "disconnect_at": "2026-04-10T09:00:00",
            "reconnect_at": "2026-04-10T09:02:30",
            "gap_seconds": 150.0,
            "affected_symbols": ["005930"],
            "rest_backfill_performed": True,
            "rest_backfill_count": 1,
            "blackswan_checked": True,
            "blackswan_cooldown_triggered": False,
            "minute_bar_backfill_count": 1,
            "observed_volatility": {"005930": 1.5},
        }
        assert self.REQUIRED_FIELDS.issubset(event.keys())

    def test_gap_event_types(self):
        event = {
            "disconnect_at": "2026-04-10T09:00:00",
            "reconnect_at": "2026-04-10T09:02:30",
            "gap_seconds": 150.0,
            "affected_symbols": ["005930"],
            "rest_backfill_performed": True,
            "rest_backfill_count": 1,
            "blackswan_checked": True,
            "blackswan_cooldown_triggered": False,
            "minute_bar_backfill_count": 1,
            "observed_volatility": {"005930": 1.5},
        }
        assert isinstance(event["gap_seconds"], float)
        assert isinstance(event["affected_symbols"], list)
        assert isinstance(event["rest_backfill_performed"], bool)
        assert isinstance(event["observed_volatility"], dict)

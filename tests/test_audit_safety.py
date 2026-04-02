"""
감사 결과 대응 — 안전 동작 검증 테스트

감사 항목:
- C-1/C-2: Live hard gate 우회 방지
- C-8/C-9: 데이터 소스 일관성
- 문서-코드 불일치: 전략 레지스트리, 파라미터 값
- WebSocket gap 임계값
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta


# ── 1. 전략 레지스트리 일관성 ──


class TestStrategyRegistry:
    """strategies/__init__.py 레지스트리 정합성 검증."""

    def test_no_duplicate_registry_entries(self):
        """_STRATEGY_REGISTRY에 중복 키가 없어야 한다 (dict 특성상 마지막만 남으므로 소스 검사)."""
        import ast

        init_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "strategies",
            "__init__.py",
        )
        with open(init_path, "r", encoding="utf-8") as f:
            source = f.read()

        # _STRATEGY_REGISTRY 딕셔너리 리터럴에서 키 중복 확인
        keys = []
        in_registry = False
        for line in source.splitlines():
            if "_STRATEGY_REGISTRY" in line and "=" in line:
                in_registry = True
                continue
            if in_registry:
                if line.strip() == "}":
                    break
                stripped = line.strip()
                if stripped.startswith('"') and ":" in stripped:
                    key = stripped.split(":")[0].strip().strip('"').strip("'")
                    keys.append(key)

        duplicates = [k for k in keys if keys.count(k) > 1]
        assert not duplicates, f"중복 등록된 전략: {set(duplicates)}"

    def test_no_duplicate_status_entries(self):
        """STRATEGY_STATUS에 중복 키가 없어야 한다 — 런타임 딕셔너리 기준."""
        from strategies import STRATEGY_STATUS
        # Python dict는 중복 키를 허용하지 않으므로 (마지막 값만 유지)
        # 소스 코드 수준 중복은 registry 테스트에서 확인하고,
        # 여기서는 런타임에 모든 전략이 등록되어 있는지 확인
        expected = {"scoring", "mean_reversion", "trend_following", "trend_pullback",
                    "fundamental_factor", "fundamental_first", "momentum_factor",
                    "ensemble", "breakout_volume", "relative_strength_rotation"}
        actual = set(STRATEGY_STATUS.keys())
        assert expected == actual, f"누락 또는 초과: {expected.symmetric_difference(actual)}"

    def test_rotation_is_provisional_candidate(self):
        """rotation은 provisional_paper_candidate, BV는 disabled (승격 규칙 v3 자동 판정)."""
        from strategies import get_strategy_status

        rot = get_strategy_status("relative_strength_rotation")
        bv = get_strategy_status("breakout_volume")
        assert rot["status"] == "provisional_paper_candidate", f"rotation: {rot['status']}"
        assert bv["status"] == "disabled", f"breakout_volume: {bv['status']}"
        assert "paper" in rot["allowed_modes"]
        assert "paper" not in bv["allowed_modes"]

    def test_no_strategy_is_live_candidate(self):
        """현재 어떤 전략도 live_candidate가 아니어야 한다."""
        from strategies import STRATEGY_STATUS

        live_candidates = [
            name for name, st in STRATEGY_STATUS.items()
            if st["status"] == "live_candidate"
        ]
        assert not live_candidates, f"live_candidate 전략이 존재: {live_candidates}"

    def test_live_mode_blocked_for_all_strategies(self):
        """모든 전략이 live 모드에서 차단되어야 한다."""
        from strategies import is_strategy_allowed, STRATEGY_STATUS

        for name in STRATEGY_STATUS:
            allowed, reason = is_strategy_allowed(name, "live")
            assert not allowed, f"{name}이 live 허용됨: {reason}"


# ── 2. 리스크 파라미터 문서-코드 일치 ──


class TestRiskParamsConsistency:
    """risk_params.yaml 값이 문서 기술과 일치하는지 검증."""

    @pytest.fixture(autouse=True)
    def load_config(self):
        from config.config_loader import Config

        self.config = Config.get()

    def test_min_holding_days_is_5(self):
        """min_holding_days가 5일이어야 한다 (문서: 3→5 강화)."""
        val = self.config.risk_params.get("position_limits", {}).get("min_holding_days")
        assert val == 5, f"min_holding_days={val}, 예상=5"

    def test_max_monthly_roundtrips_is_8(self):
        """max_monthly_roundtrips가 8이어야 한다 (문서: 5→8 완화)."""
        val = self.config.risk_params.get("position_limits", {}).get(
            "max_monthly_roundtrips"
        )
        assert val == 8, f"max_monthly_roundtrips={val}, 예상=8"

    def test_liquidity_filter_enabled(self):
        """유동성 필터가 활성화되어 있어야 한다."""
        lf = self.config.risk_params.get("liquidity_filter", {})
        assert lf.get("enabled") is True
        assert lf.get("check_on_entry") is True

    def test_backtest_universe_historical(self):
        """backtest_universe.mode가 historical이어야 한다."""
        bu = self.config.risk_params.get("backtest_universe", {})
        assert bu.get("mode") == "historical", f"mode={bu.get('mode')}"


# ── 3. 데이터 소스 안전성 ──


class TestDataSourceSafety:
    """데이터 수집기의 소스 추적·경고 동작 검증."""

    def test_check_source_consistency_returns_warnings(self):
        """KIS 혼용 시 경고 메시지를 반환해야 한다."""
        from core.data_collector import DataCollector

        dc = DataCollector()
        dc._source_history = {
            "005930": "FinanceDataReader",
            "000660": "KIS",
        }
        warnings = dc.check_source_consistency(mode="paper")
        assert len(warnings) > 0
        assert any("불일치" in w for w in warnings)

    def test_check_source_consistency_live_mode_extra_warning(self):
        """live 모드에서 KIS 사용 시 추가 경고가 나와야 한다."""
        from core.data_collector import DataCollector

        dc = DataCollector()
        dc._source_history = {"005930": "KIS"}
        warnings = dc.check_source_consistency(mode="live")
        assert len(warnings) >= 1
        assert any("Live" in w or "live" in w.lower() for w in warnings)

    def test_has_kis_fallback_symbols(self):
        """KIS 폴백 종목 목록을 정확히 반환해야 한다."""
        from core.data_collector import DataCollector

        dc = DataCollector()
        dc._source_history = {
            "005930": "FinanceDataReader",
            "000660": "KIS",
            "035720": "yfinance",
        }
        kis_list = dc.has_kis_fallback_symbols()
        assert kis_list == ["000660"]

    def test_no_duplicate_check_source_consistency_methods(self):
        """check_source_consistency 메서드가 하나만 존재해야 한다 (중복 제거 확인)."""
        from core.data_collector import DataCollector
        import inspect

        source = inspect.getsource(DataCollector)
        count = source.count("def check_source_consistency(")
        assert count == 1, f"check_source_consistency 정의 {count}개 (1개여야 함)"

    def test_kis_fallback_blocked_when_disabled(self):
        """allow_kis_fallback=false 시 KIS 폴백이 차단되어야 한다."""
        from core.data_collector import DataCollector, DataCollectionError

        dc = DataCollector()
        dc._allow_kis_fallback = False
        dc._preferred_source = "auto"

        # FDR과 yfinance 모두 실패하도록 mock
        with patch.object(dc, "_try_fdr", return_value=None):
            import pandas as pd

            with patch.object(
                dc,
                "_fetch_korean_stock_via_yfinance",
                return_value=pd.DataFrame(),
            ):
                with pytest.raises(DataCollectionError):
                    dc._fetch_korean_stock_uncached("999999", "2025-01-01", "2025-12-31")


# ── 4. WebSocket gap 임계값 ──


class TestWebSocketGapThresholds:
    """WebSocket 갭 처리 임계값이 감사 권고에 맞게 설정되어 있는지 검증."""

    def test_blackswan_recheck_threshold_lowered(self):
        """BlackSwan 재체크 임계값이 5분에서 2분으로 낮아져야 한다."""
        from api.websocket_handler import WebSocketHandler

        assert WebSocketHandler._GAP_BLACKSWAN_RECHECK <= timedelta(minutes=2), (
            f"_GAP_BLACKSWAN_RECHECK={WebSocketHandler._GAP_BLACKSWAN_RECHECK}, "
            "2분 이하여야 함 (감사 H-1 대응)"
        )

    def test_rest_refresh_threshold_lowered(self):
        """REST 갱신 임계값이 3분에서 2분으로 낮아져야 한다."""
        from api.websocket_handler import WebSocketHandler

        assert WebSocketHandler._GAP_REST_REFRESH <= timedelta(minutes=2), (
            f"_GAP_REST_REFRESH={WebSocketHandler._GAP_REST_REFRESH}, "
            "2분 이하여야 함"
        )


# ── 5. SOURCE_ADJUSTED_MAP 정합성 ──


class TestSourceAdjustedMap:
    """수정주가 여부 매핑이 올바른지 검증."""

    def test_fdr_is_adjusted(self):
        from core.data_collector import DataCollector

        assert DataCollector.SOURCE_ADJUSTED_MAP["FinanceDataReader"] is True

    def test_yfinance_is_adjusted(self):
        from core.data_collector import DataCollector

        assert DataCollector.SOURCE_ADJUSTED_MAP["yfinance"] is True

    def test_kis_is_not_adjusted(self):
        from core.data_collector import DataCollector

        assert DataCollector.SOURCE_ADJUSTED_MAP["KIS"] is False

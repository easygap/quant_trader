"""대시보드 /api/baskets — 바스켓 트랙 '내 돈' 화면 데이터 테스트.

계약: DB 전용(네트워크 조회 없음), 최신 스냅샷(TWR 반영값) + 원금(초기+입금) +
배치율 + 보유를 바스켓별로 반환한다. 적립식 계정의 "내가 넣은 돈 대비 얼마"에 답한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from unittest.mock import patch

import pytest

from database.models import init_database


def _seed_pocket(basket_name):
    """격리 DB에 kr_pocket풍 데이터: 스냅샷 + 입금 + 포지션.

    세션 공유 격리 DB이므로 테스트마다 고유 바스켓 이름을 써서 서로 오염되지 않게 한다.
    """
    from core.basket_rebalancer import rebalance_live_strategy_id
    from database.models import PortfolioSnapshot, Position, get_session
    from database.repositories import record_cash_flow

    acct = rebalance_live_strategy_id(basket_name)
    init_database()
    session = get_session()
    try:
        session.add(PortfolioSnapshot(
            account_key=acct, date=datetime(2026, 7, 6),
            total_value=400_126, cash=171_846, invested=228_280,
            cumulative_return=0.04, mdd=0.0, peak_value=400_126,
        ))
        session.add(Position(
            account_key=acct, symbol="069500", avg_price=128_135,
            quantity=1, total_invested=128_135,
        ))
        session.commit()
    finally:
        session.close()
    record_cash_flow(100_000, account_key=acct, occurred_at=datetime(2026, 7, 6, 9, 0))
    return acct


def _cfg(basket_name):
    return {
        basket_name: {
            "name": "소액 적립 (KODEX200 50/50)",
            "enabled": True,
            "initial_capital": 300_000,
            "target_stock_weight": 0.5,
            "holdings": {"069500": 1.0},
        }
    }


class TestGetBasketsJson:
    def test_principal_snapshot_deployment_positions(self):
        name = "kr_pocket_t1"
        acct = _seed_pocket(name)
        from monitoring import web_dashboard as wd

        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            return_value=[name],
        ), patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_cfg(name),
        ):
            data = wd.get_baskets_json()

        b = data["baskets"][0]
        assert b["basket"] == name
        assert b["account_key"] == acct
        # 원금 = 초기 30만 + 입금 10만
        assert b["principal"] == pytest.approx(400_000)
        assert b["deposits_total"] == pytest.approx(100_000)
        # 스냅샷 값 그대로(TWR 반영치) — 재계산하지 않는다
        assert b["snapshot"]["total_value"] == pytest.approx(400_126)
        assert b["snapshot"]["cumulative_return"] == pytest.approx(0.04)
        # 원금 대비 손익 = 평가금 - 원금
        assert b["profit_vs_principal"] == pytest.approx(126)
        # 배치율 = (총-현금)/총, 설계 = target_stock_weight
        assert b["deployment_ratio"] == pytest.approx((400_126 - 171_846) / 400_126)
        assert b["design_fraction"] == pytest.approx(0.5)
        # 보유
        assert b["positions"] == [{
            "symbol": "069500", "quantity": 1,
            "avg_price": 128_135.0, "invested": 128_135.0,
        }]

    def test_no_snapshot_yet_is_null_not_crash(self):
        name = "kr_pocket_empty"  # 시드 없음 — 운영 전 상태
        init_database()
        from monitoring import web_dashboard as wd

        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            return_value=[name],
        ), patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_cfg(name),
        ):
            data = wd.get_baskets_json()
        b = data["baskets"][0]
        assert b["snapshot"] is None
        assert b["profit_vs_principal"] is None
        assert b["deployment_ratio"] is None
        assert b["principal"] == pytest.approx(300_000)  # 입금 없으면 초기자본


try:
    import aiohttp  # noqa: F401
    _has_aiohttp = True
except ImportError:
    _has_aiohttp = False


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_baskets_endpoint_serves_json():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from monitoring import web_dashboard as wd

    name = "kr_pocket_http"
    _seed_pocket(name)

    async def run():
        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            return_value=[name],
        ), patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_cfg(name),
        ):
            app = wd.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                res = await client.get("/api/baskets")
                assert res.status == 200
                data = await res.json()
            finally:
                await client.close()
            assert data["baskets"][0]["basket"] == name

    asyncio.run(run())


def test_html_page_contains_basket_tracks_section():
    from monitoring.web_dashboard import _html_page

    html = _html_page()
    assert "basketTracks" in html          # 섹션
    assert "/api/baskets" in html          # 폴링 대상
    assert "chartAccount" in html          # 차트 계정 선택기
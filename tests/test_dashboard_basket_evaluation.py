"""대시보드 /api/basket_evaluation 회귀 테스트 — 승격 진행률 웹 노출(read-only).

게이트와 같은 수집기(collect_basket_paper_evaluation)를 쓰므로 웹 판정 = 게이트 판정.
include_benchmark=False(외부 조회 없음 — 10초 폴링 경로) 계약도 고정한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch

try:
    import aiohttp  # noqa: F401
    _has_aiohttp = True
except ImportError:
    _has_aiohttp = False


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_basket_evaluation_endpoint_returns_progress():
    from aiohttp.test_utils import TestClient, TestServer
    import asyncio
    from monitoring import web_dashboard as wd

    fake_result = {
        "verdict": "WAIT",
        "progress_days": 2,
        "min_trading_days": 60,
        "snapshot_coverage": 1.0,
        "issues": [],
    }

    from monitoring import web_dashboard as wd0
    wd0._BASKET_EVAL_CACHE.update({"at": 0.0, "data": None})

    async def run():
        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            return_value=["kr_diversified_hold"],
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            return_value=(fake_result, "kr_diversified_hold"),
        ) as collect:
            app = wd.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                res = await client.get("/api/basket_evaluation")
                assert res.status == 200
                data = await res.json()
            finally:
                await client.close()
            assert collect.call_args.kwargs["include_benchmark"] is False
            assert collect.call_args.kwargs["basket_name"] == "kr_diversified_hold"
            ev = data["evaluations"][0]
            assert ev["verdict"] == "WAIT"
            assert ev["progress_days"] == 2
            assert ev["min_trading_days"] == 60
            assert ev["snapshot_coverage"] == 1.0

    asyncio.run(run())


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_basket_evaluation_endpoint_fails_soft():
    """수집 실패 시 500 + error JSON (대시보드 다른 카드에 영향 없음)."""
    from aiohttp.test_utils import TestClient, TestServer
    import asyncio
    from monitoring import web_dashboard as wd

    from monitoring import web_dashboard as wd0
    wd0._BASKET_EVAL_CACHE.update({"at": 0.0, "data": None})

    async def run():
        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            side_effect=RuntimeError("db down"),
        ):
            app = wd.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                res = await client.get("/api/basket_evaluation")
                assert res.status == 500
                data = await res.json()
                assert "error" in data
            finally:
                await client.close()

    asyncio.run(run())


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_index_page_serves_200_with_progress_section():
    """메인 페이지 '/' 서빙 회귀 — aiohttp 3.13+에서 content_type에 charset을 섞으면
    ValueError로 페이지 전체가 500이 된다(실제로 그렇게 죽어 있던 운영 결함).
    API만 검증하고 페이지를 안 보면 이런 결함이 가려진다."""
    from aiohttp.test_utils import TestClient, TestServer
    import asyncio
    from monitoring import web_dashboard as wd

    async def run():
        client = TestClient(TestServer(wd.create_app()))
        await client.start_server()
        try:
            res = await client.get("/")
            assert res.status == 200
            html = await res.text()
        finally:
            await client.close()
        assert "basketEval" in html and "모의 운용 검증" in html

    asyncio.run(run())


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_basket_evaluation_endpoint_caches_for_ttl():
    """60초 TTL 캐시 — 10초 폴링이 매번 수집기(TradingHours 생성 포함)를 부르지 않는다."""
    from aiohttp.test_utils import TestClient, TestServer
    import asyncio
    from monitoring import web_dashboard as wd

    wd._BASKET_EVAL_CACHE.update({"at": 0.0, "data": None})
    fake_result = {"verdict": "WAIT", "progress_days": 2, "min_trading_days": 60,
                   "snapshot_coverage": 1.0, "issues": []}

    async def run():
        with patch(
            "core.basket_rebalancer.BasketRebalancer.get_enabled_baskets",
            return_value=["kr_diversified_hold"],
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            return_value=(fake_result, "kr_diversified_hold"),
        ) as collect:
            client = TestClient(TestServer(wd.create_app()))
            await client.start_server()
            try:
                r1 = await client.get("/api/basket_evaluation")
                r2 = await client.get("/api/basket_evaluation")
                assert r1.status == 200 and r2.status == 200
            finally:
                await client.close()
            assert collect.call_count == 1  # 두 번째 요청은 캐시

    asyncio.run(run())

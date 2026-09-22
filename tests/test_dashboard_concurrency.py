"""느린 장부 조회와 동시 접속이 화면 전체를 멈추지 않는지 확인한다."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer

from monitoring import web_dashboard as wd


@pytest.mark.parametrize("path,target,payload", [
    ("/api/baskets", "monitoring.web_dashboard.get_baskets_json", {"baskets": []}),
    ("/api/snapshots", "monitoring.web_dashboard.get_snapshots_json", {"snapshots": []}),
    ("/api/cash_flows?basket=kr_pocket", "database.repositories.get_recent_cash_flows", []),
])
def test_slow_ledger_read_does_not_block_the_page(path, target, payload):
    entered, release = threading.Event(), threading.Event()

    def read(*args, **kwargs):
        entered.set()
        release.wait(2)
        return payload

    async def run():
        async with TestClient(TestServer(wd.create_app())) as client:
            with patch(target, side_effect=read):
                task = asyncio.create_task(client.get(path))
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    # 조회를 끝내 주기 전에도 HTML 요청에 응답해야 한다.
                    response = await asyncio.wait_for(client.get("/"), 1)
                    assert response.status == 200
                    assert not task.done(), "장부 조회 때문에 페이지 요청도 함께 기다렸습니다"
                finally:
                    release.set()
                    await task

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["runtime", "basket_evaluation"])
def test_simultaneous_cache_misses_collect_only_once(endpoint):
    async def run():
        entered, release = threading.Event(), threading.Event()
        calls = []

        def collect(*args, **kwargs):
            calls.append(1)
            entered.set()
            assert release.wait(3)
            return {"timestamp": "2026-09-22T15:00:00"} if endpoint == "runtime" else ({"verdict": "WAIT"}, "kr_pocket")

        target = "monitoring.web_dashboard.get_runtime_json" if endpoint == "runtime" else "core.basket_evaluation.collect_basket_paper_evaluation"
        with patch(target, side_effect=collect), patch.object(wd, "_get_trading_halt_json", return_value={"halted": False}), patch("core.basket_rebalancer.BasketRebalancer.get_enabled_baskets", return_value=["kr_pocket"]):
            async with TestClient(TestServer(wd.create_app())) as client:
                tasks = [asyncio.create_task(client.get(f"/api/{endpoint}")) for _ in range(6)]
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    # 전송한 요청이 모두 서버에 들어갈 시간을 준다.
                    await asyncio.sleep(0.05)
                finally:
                    release.set()
                responses = await asyncio.gather(*tasks)
                assert all(r.status == 200 for r in responses)
                assert len(calls) == 1

    asyncio.run(run())


def test_runtime_cache_is_separate_for_each_app():
    async def run():
        with patch.object(wd, "_get_trading_halt_json", return_value={"halted": False}), patch.object(wd, "get_runtime_json", side_effect=[{"strategy": "first"}, {"strategy": "second"}]) as collect:
            async with TestClient(TestServer(wd.create_app())) as first, TestClient(TestServer(wd.create_app())) as second:
                one = await (await first.get("/api/runtime")).json()
                two = await (await second.get("/api/runtime")).json()
                assert one["strategy"] == "first"
                assert two["strategy"] == "second"
                assert collect.call_count == 2

    asyncio.run(run())


def test_cache_retries_after_failure_and_refreshes_when_mode_changes():
    async def run():
        with patch.object(wd, "_get_trading_halt_json", return_value={"halted": False}), patch.object(wd, "_active_ledger_mode", return_value="paper") as mode, patch.object(wd, "get_runtime_json", side_effect=[RuntimeError("조회 실패"), {"strategy": "paper"}, {"strategy": "live"}]) as collect:
            async with TestClient(TestServer(wd.create_app())) as client:
                assert (await client.get("/api/runtime")).status == 500
                assert (await (await client.get("/api/runtime")).json())["strategy"] == "paper"
                mode.return_value = "live"
                assert (await (await client.get("/api/runtime")).json())["strategy"] == "live"
                assert collect.call_count == 3

    asyncio.run(run())


def test_cache_ttl_starts_when_collection_finishes():
    clock = {"now": 0.0}

    def collect():
        clock["now"] += 20.0
        return {"strategy": "test"}

    async def run():
        with patch.object(wd, "time", SimpleNamespace(monotonic=lambda: clock["now"])), patch.object(wd, "get_runtime_json", side_effect=collect) as read, patch.object(wd, "_get_trading_halt_json", return_value={"halted": False}):
            async with TestClient(TestServer(wd.create_app())) as client:
                assert (await client.get("/api/runtime")).status == 200
                # 수집 시작 후 70초, 완료 후 50초: 아직 같은 자료를 사용한다.
                clock["now"] = 70.0
                assert (await client.get("/api/runtime")).status == 200
                assert read.call_count == 1
                clock["now"] = 81.0
                assert (await client.get("/api/runtime")).status == 200
                assert read.call_count == 2

    asyncio.run(run())

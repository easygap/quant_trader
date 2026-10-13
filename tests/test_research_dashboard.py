"""Research refresh is isolated from trading and cannot block ordinary page reads."""
import asyncio
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from monitoring import research_dashboard as rd
from monitoring.web_dashboard import create_app


def test_research_read_does_not_collect_or_initialize_database():
    async def run():
        with patch.object(rd, "get_research_desk_json", return_value={"research_only": True}), \
             patch.object(rd, "run_research_refresh", new_callable=AsyncMock) as collect, \
             patch("database.models.init_database", side_effect=AssertionError("DB write")):
            async with TestClient(TestServer(create_app())) as client:
                response = await client.get("/api/research")
                assert response.status == 200
                assert (await response.json())["research_only"] is True
                collect.assert_not_called()
    asyncio.run(run())


def test_refresh_coalesces_concurrent_requests_without_blocking_page():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def collect():
            entered.set()
            await release.wait()
        with patch.object(rd, "run_research_refresh", side_effect=collect) as refresh, \
             patch.object(rd, "get_research_desk_json", return_value={"market": {"status": "ready"}}):
            async with TestClient(TestServer(create_app())) as client:
                tasks = [asyncio.create_task(client.post("/api/research/refresh", headers={"X-Requested-With": "quant-dashboard"})) for _ in range(3)]
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    assert (await asyncio.wait_for(client.get("/"), 1)).status == 200
                finally:
                    release.set()
                assert all(response.status == 200 for response in await asyncio.gather(*tasks))
                assert refresh.call_count == 1
    asyncio.run(run())


def test_failed_refresh_is_retryable_and_not_reported_as_fresh():
    async def run():
        with patch.object(rd, "run_research_refresh", side_effect=[asyncio.TimeoutError(), None]) as refresh, \
             patch.object(rd, "get_research_desk_json", return_value={"market": {"status": "partial"}}):
            async with TestClient(TestServer(create_app())) as client:
                headers = {"X-Requested-With": "quant-dashboard"}
                assert (await client.post("/api/research/refresh", headers=headers)).status == 504
                response = await client.post("/api/research/refresh", headers=headers)
                assert response.status == 200
                assert (await response.json())["market"]["status"] == "partial"
                assert refresh.call_count == 2
    asyncio.run(run())


def test_concurrent_timeouts_share_one_child_and_later_click_can_retry():
    async def run():
        service = rd.ResearchRefresh()
        entered, release = asyncio.Event(), asyncio.Event()
        async def collect():
            entered.set()
            await release.wait()
            raise asyncio.TimeoutError()
        with patch.object(rd, "run_research_refresh", side_effect=collect) as refresh:
            tasks = [asyncio.create_task(service.refresh()) for _ in range(3)]
            await entered.wait()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(result, asyncio.TimeoutError) for result in results)
            assert refresh.call_count == 1
        with patch.object(rd, "run_research_refresh", new_callable=AsyncMock), \
             patch.object(rd, "get_research_desk_json", return_value={"fresh": True}):
            assert await service.refresh() == {"fresh": True}
    asyncio.run(run())


def test_refresh_requires_same_local_dashboard_request():
    async def run():
        with patch.object(rd, "run_research_refresh", new_callable=AsyncMock) as collect:
            async with TestClient(TestServer(create_app())) as client:
                for headers in ({}, {"X-Requested-With": "quant-dashboard", "Origin": "https://outside.example"}):
                    assert (await client.post("/api/research/refresh", headers=headers)).status == 403
                collect.assert_not_called()
    asyncio.run(run())


def test_stuck_refresh_child_is_killed(monkeypatch):
    class Process:
        returncode = None
        killed = False
        async def wait(self):
            if not self.killed:
                await asyncio.sleep(10)
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -1
    process = Process()
    original_wait_for = asyncio.wait_for
    monkeypatch.setattr(rd.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(rd.asyncio, "wait_for", lambda task, timeout: original_wait_for(task, timeout=0.01))
    async def run():
        try:
            await rd.run_research_refresh()
            raise AssertionError("timeout required")
        except asyncio.TimeoutError:
            assert process.killed
    asyncio.run(run())

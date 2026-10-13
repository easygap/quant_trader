"""Read-only research view; explicit refresh collects prices in a bounded child.

This module does not import the trading executor or initialize a database. Opening
the dashboard reads saved research; only a refresh request contacts data providers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

from aiohttp import web
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent


def get_research_desk_json() -> dict:
    from core.market_research import read_market_research
    from core.research_evidence import read_evidence_journal

    # A broken research file must not hide the other half of the desk.
    try:
        market = read_market_research()
    except Exception:
        logger.warning("시장 관찰 자료 읽기 실패")
        market = {"status": "unavailable", "issues": ["저장 자료를 읽을 수 없습니다."]}
    try:
        evidence = read_evidence_journal()
    except Exception:
        logger.warning("투자 가설 자료 읽기 실패")
        evidence = {"status": "ATTENTION", "entries": [], "issues": ["투자 가설 기록을 읽을 수 없습니다."]}
    return {"research_only": True, "market": market, "evidence": evidence}


async def run_research_refresh() -> None:
    """Kill a stuck vendor fetch without blocking the web server or trading loop."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "tools" / "market_research.py"),
        cwd=str(ROOT), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await asyncio.wait_for(process.wait(), timeout=120)
        if code != 0:
            raise RuntimeError("market research collection failed")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class ResearchRefresh:
    def __init__(self):
        self.task = None
        self.completed = None

    async def _collect(self):
        await run_research_refresh()
        self.completed = time.monotonic()
        return await asyncio.to_thread(get_research_desk_json)

    def _finished(self, task):
        # Retrieve exceptions even if every browser disconnected in the meantime.
        if not task.cancelled():
            task.exception()
        if self.task is task:
            self.task = None

    async def refresh(self):
        if self.task is None:
            if self.completed is not None and time.monotonic() - self.completed < 30:
                return await asyncio.to_thread(get_research_desk_json)
            self.task = asyncio.create_task(self._collect())
            self.task.add_done_callback(self._finished)
        # Every concurrent tab receives the same success OR failure. Disconnecting
        # one client cannot kill the shared collection or queue repeated timeouts.
        return await asyncio.shield(self.task)

    async def close(self):
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


REFRESH_KEY = web.AppKey("research_refresh", ResearchRefresh)


async def handle_research(_request):
    return web.json_response(await asyncio.to_thread(get_research_desk_json))


async def handle_refresh(request):
    from monitoring.web_dashboard import _is_local_origin

    if not _is_local_origin(request) or request.headers.get("X-Requested-With") != "quant-dashboard":
        return web.json_response({"error": "이 컴퓨터의 대시보드에서 갱신해 주세요."}, status=403)
    try:
        return web.json_response(await request.app[REFRESH_KEY].refresh())
    except asyncio.TimeoutError:
        return web.json_response({"error": "자료 수집 시간이 초과되었습니다. 이전 자료의 기준일을 확인해 주세요."}, status=504)
    except Exception:
        logger.warning("시장 관찰 자료 갱신 실패")
        return web.json_response({"error": "자료를 갱신하지 못했습니다. 잠시 후 다시 시도해 주세요."}, status=503)


def register_research_routes(app):
    app[REFRESH_KEY] = ResearchRefresh()
    async def close_research(application):
        await application[REFRESH_KEY].close()
    app.on_cleanup.append(close_research)
    app.router.add_get("/api/research", handle_research)
    app.router.add_post("/api/research/refresh", handle_refresh)

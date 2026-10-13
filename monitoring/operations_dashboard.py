"""Read-only evidence desk for collection, execution costs and forward paper runs."""
import asyncio
from aiohttp import web


def get_operations_json():
    from core.research_operations import read_research_operations
    result = {"research_only": True, "operations": read_research_operations()}
    readers = {
        "daily_observation": ("core.daily_observation", "read_daily_observation"),
        "intelligence": ("core.market_intelligence", "read_market_intelligence"),
        "validation": ("core.paper_validation", "read_paper_validation"),
        "execution": ("core.execution_analytics", "execution_cost_report"),
    }
    import importlib
    for key, (module, function) in readers.items():
        try:
            reader = getattr(importlib.import_module(module), function)
            result[key] = reader()
        except Exception:
            result[key] = {"status": "unavailable", "issues": ["저장 자료를 읽을 수 없습니다."]}
    return result


async def handle_operations(_request):
    return web.json_response(await asyncio.to_thread(get_operations_json))


def register_operations_routes(app):
    app.router.add_get("/api/research/operations", handle_operations)

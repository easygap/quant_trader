"""
눈금(NUNGUM) 웹 대시보드.

대시보드는 장부와 런타임 상태를 읽어 사용자가 오늘 해야 할 일, 장기 성과,
실전 전환 준비도를 한 화면에서 이해하도록 돕는다. 웹에서 가능한 쓰기는
적립금 기록뿐이며 매매와 설정 변경은 의도적으로 제공하지 않는다.
"""

from __future__ import annotations

from datetime import datetime
import ipaddress
from pathlib import Path
import re
from typing import Optional

try:
    from aiohttp import web
except ModuleNotFoundError:
    web = None
from loguru import logger

from config.config_loader import Config
from database.repositories import get_portfolio_snapshots
from monitoring.dashboard import Dashboard


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATE_PATH = _DASHBOARD_DIR / "templates" / "dashboard.html"
_STATIC_PATH = _DASHBOARD_DIR / "static"


def _require_aiohttp_web():
    if web is None:
        raise RuntimeError("웹 대시보드 실행에는 aiohttp 설치가 필요합니다.")
    return web


def _active_ledger_mode(config=None) -> str:
    """현재 설정의 장부 모드를 paper/live 두 값으로 정규화한다."""
    cfg = config or Config.get()
    return "live" if str(cfg.trading.get("mode", "paper")).lower() == "live" else "paper"


def _serialize_snapshots(df):
    """DataFrame 스냅샷을 JSON 직렬화 가능한 리스트로 변환한다."""
    if df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        item = row.to_dict()
        for key, value in item.items():
            if hasattr(value, "strftime"):
                item[key] = (
                    value.strftime("%Y-%m-%d")
                    if key == "date"
                    else value.strftime("%Y-%m-%d %H:%M:%S")
                )
            elif hasattr(value, "item"):
                item[key] = value.item()
        out.append(item)
    return out


_DASH = None


def get_portfolio_json(current_prices: Optional[dict] = None) -> dict:
    """레거시 기본 계정의 현재 포트폴리오 요약을 반환한다."""
    global _DASH
    config = Config.get()
    if _DASH is None:
        _DASH = Dashboard(config=config)
    dash = _DASH
    summary = dash.portfolio_manager.get_portfolio_summary(current_prices or {})
    return {
        "timestamp": datetime.now().isoformat(),
        "mode": _active_ledger_mode(config),
        "initial_capital": dash.initial_capital,
        "total_value": summary["total_value"],
        "cash": summary["cash"],
        "invested": summary["invested"],
        "current_value": summary["current_value"],
        "total_return": summary["total_return"],
        "mdd": summary["mdd"],
        "position_count": summary["position_count"],
        "realized_pnl": summary["realized_pnl"],
        "unrealized_pnl": summary["unrealized_pnl"],
        "positions": summary["positions"],
    }


def get_snapshots_json(days: int = 30, account_key: Optional[str] = None) -> dict:
    """최근 N일 스냅샷을 활성 장부 모드에서 반환한다."""
    config = Config.get()
    ledger_mode = _active_ledger_mode(config)
    df = get_portfolio_snapshots(
        days=days,
        account_key=account_key,
        mode=ledger_mode,
    )
    return {
        "snapshots": _serialize_snapshots(df),
        "days": days,
        "mode": ledger_mode,
    }


def get_baskets_json() -> dict:
    """활성 바스켓별 원금·평가금·배치율·보유 현황을 DB에서만 읽는다."""
    from core.basket_deploy import effective_stock_fraction
    from core.basket_rebalancer import BasketRebalancer, rebalance_live_strategy_id
    from database.models import PortfolioSnapshot, get_session
    from database.repositories import get_all_positions, get_cash_flow_total

    config = Config.get()
    ledger_mode = _active_ledger_mode(config)
    baskets_cfg = BasketRebalancer._load_baskets_config()
    global_capital = (config.risk_params.get("position_sizing") or {}).get(
        "initial_capital", 10_000_000
    )

    out = []
    for name in BasketRebalancer.get_enabled_baskets():
        basket_config = baskets_cfg.get(name) or {}
        account_key = rebalance_live_strategy_id(name)
        initial_capital = float(basket_config.get("initial_capital") or global_capital)
        deposits_total = float(
            get_cash_flow_total(account_key=account_key, mode=ledger_mode) or 0
        )
        principal = initial_capital + deposits_total

        session = get_session()
        try:
            latest = (
                session.query(PortfolioSnapshot)
                .filter(
                    PortfolioSnapshot.mode == ledger_mode,
                    PortfolioSnapshot.account_key == account_key,
                )
                .order_by(PortfolioSnapshot.date.desc())
                .first()
            )
            snapshot = None
            deployment_ratio = None
            if latest is not None:
                total_value = float(latest.total_value or 0)
                cash = float(latest.cash or 0)
                deployment_ratio = (
                    max(0.0, (total_value - cash) / total_value)
                    if total_value > 0
                    else None
                )
                snapshot = {
                    "date": str(latest.date)[:10],
                    "total_value": total_value,
                    "cash": cash,
                    "cumulative_return": float(latest.cumulative_return or 0),
                    "mdd": float(latest.mdd or 0),
                }
        finally:
            session.close()

        holding_names = basket_config.get("holding_names") or {}
        positions = [
            {
                "symbol": position.symbol,
                "name": holding_names.get(position.symbol),
                "quantity": int(position.quantity or 0),
                "avg_price": float(position.avg_price or 0),
                "invested": float(
                    (position.quantity or 0) * (position.avg_price or 0)
                ),
            }
            for position in (
                get_all_positions(account_key=account_key, mode=ledger_mode) or []
            )
            if (position.quantity or 0) > 0
        ]

        is_primary = bool(
            basket_config.get("primary", name == "kr_pocket")
        )
        plan_config = basket_config.get("contribution_plan") or {}
        contribution_plan = {
            "enabled": bool(plan_config.get("enabled", False)),
            "cadence": str(plan_config.get("cadence") or ""),
            "amount": float(plan_config.get("amount") or 0),
        }
        out.append(
            {
                "basket": name,
                "account_key": account_key,
                "display_name": basket_config.get("name") or name,
                "purpose": basket_config.get("purpose")
                or ("월 적립 중심" if is_primary else "장기 관찰용"),
                "is_primary": is_primary,
                "contribution_plan": contribution_plan,
                "initial_capital": initial_capital,
                "deposits_total": deposits_total,
                "principal": principal,
                "snapshot": snapshot,
                "profit_vs_principal": (
                    snapshot["total_value"] - principal if snapshot else None
                ),
                "deployment_ratio": deployment_ratio,
                "design_fraction": effective_stock_fraction(
                    basket_config, config.risk_params
                ),
                "positions": positions,
            }
        )

    return {
        "baskets": out,
        "mode": ledger_mode,
        "timestamp": datetime.now().isoformat(),
    }


def _get_trading_halt_json() -> Optional[dict]:
    """전역 HALT를 DB에서 매번 새로 읽어 JSON 형태로 반환한다."""
    try:
        from database.repositories import get_trading_halt_state

        halt_state = get_trading_halt_state()
        created_at = halt_state.get("created_at")
        if hasattr(created_at, "isoformat"):
            halt_state["created_at"] = created_at.isoformat()
        return halt_state
    except Exception as exc:
        logger.debug("get_runtime_json trading_halt: {}", exc)
        return None


def get_runtime_json() -> dict:
    """시장·스케줄러 상태를 수집한다. HALT는 응답 직전 다시 확인한다."""
    out: dict = {
        "timestamp": datetime.now().isoformat(),
        "market_regime": None,
        "trading_halt": None,
        "signals_today": None,
        "signals_date": None,
        "strategy": None,
        "kis_stats": None,
        "kis_stats_source": None,
        "blackswan": None,
        "loop_metrics": None,
        "ws_gap": None,
        "runtime_file_updated_at": None,
    }

    out["trading_halt"] = _get_trading_halt_json()

    try:
        from core.data_collector import DataCollector
        from core.market_regime import check_market_regime

        config = Config.get()
        regime = check_market_regime(config, DataCollector())
        out["market_regime"] = {
            "regime": regime.get("regime"),
            "position_scale": regime.get("position_scale"),
            "allow_buys": regime.get("allow_buys"),
        }
    except Exception as exc:
        logger.debug("get_runtime_json market_regime: {}", exc)

    try:
        from monitoring.dashboard_runtime_state import read_state

        runtime_state = read_state()
        out["runtime_file_updated_at"] = runtime_state.get("updated_at")
        raw_signals = runtime_state.get("signals_today")
        out["signals_today"] = raw_signals if isinstance(raw_signals, list) else []
        out["signals_date"] = runtime_state.get("signals_date")
        out["strategy"] = runtime_state.get("strategy")
        out["loop_metrics"] = runtime_state.get("loop_metrics")
        out["blackswan"] = runtime_state.get("blackswan")
        out["ws_gap"] = runtime_state.get("ws_gap")
        if runtime_state.get("kis_stats") is not None:
            out["kis_stats"] = runtime_state.get("kis_stats")
            out["kis_stats_source"] = "scheduler_file"
    except Exception as exc:
        logger.debug("get_runtime_json read_state: {}", exc)
        out["signals_today"] = None

    return out


def _html_page() -> str:
    """파일 기반 템플릿을 읽어 UI와 Python 데이터 계층을 분리한다."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _api_error(label: str, exc: Exception, message: str) -> web.Response:
    """내부 예외는 로그에만 남기고 브라우저에는 고정 문구만 반환한다."""
    logger.exception("{}: {}", label, exc)
    return web.json_response({"error": message}, status=500)


async def _security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


async def handle_index(_request: web.Request) -> web.Response:
    return web.Response(
        text=_html_page(),
        content_type="text/html",
        charset="utf-8",
    )


async def handle_api_portfolio(_request: web.Request) -> web.Response:
    import asyncio

    try:
        data = await asyncio.to_thread(get_portfolio_json)
        return web.json_response(data)
    except Exception as exc:
        return _api_error(
            "API /api/portfolio 오류", exc, "포트폴리오를 불러오지 못했습니다"
        )


async def handle_api_baskets(_request: web.Request) -> web.Response:
    try:
        return web.json_response(get_baskets_json())
    except Exception as exc:
        return _api_error(
            "API /api/baskets 오류", exc, "포트폴리오를 불러오지 못했습니다"
        )


async def handle_api_deposit(request: web.Request) -> web.Response:
    """적립금 기록. 커스텀 헤더로 cross-site 브라우저 요청을 차단한다."""
    if request.headers.get("X-Requested-With") != "quant-dashboard":
        return web.json_response(
            {"ok": False, "error": "대시보드 외 요청 차단(CSRF 방어)"},
            status=403,
        )
    request_id = str(request.headers.get("Idempotency-Key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,63}", request_id):
        return web.json_response(
            {"ok": False, "error": "유효한 입금 요청 키가 필요합니다"}, status=400
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "JSON 본문이 필요합니다"}, status=400
        )

    try:
        from tools.record_deposit import record_basket_deposit

        result = record_basket_deposit(
            str(body.get("basket") or ""),
            body.get("amount"),
            note=str(body.get("note") or ""),
            request_id=request_id,
        )
        if not result.get("ok"):
            return web.json_response(result, status=400)
        logger.info(
            "웹 입금 기록: {} +{:,.0f}원 (누적 입금 {:,.0f}원)",
            result["account_key"],
            result["amount"],
            result["deposits_total"],
        )
        return web.json_response(result)
    except Exception as exc:
        logger.exception("API /api/deposit 오류: {}", exc)
        return web.json_response(
            {"ok": False, "error": "적립금을 기록하지 못했습니다"}, status=500
        )


async def handle_api_cash_flows(request: web.Request) -> web.Response:
    """선택한 바스켓의 최근 적립금 기록을 활성 장부 모드에서 반환한다."""
    try:
        from core.basket_rebalancer import rebalance_live_strategy_id
        from database.repositories import get_recent_cash_flows

        basket = request.query.get("basket") or ""
        if not basket:
            return web.json_response(
                {"error": "basket 파라미터 필요"}, status=400
            )
        ledger_mode = _active_ledger_mode()
        account_key = rebalance_live_strategy_id(basket)
        return web.json_response(
            {
                "basket": basket,
                "mode": ledger_mode,
                "flows": get_recent_cash_flows(
                    account_key, mode=ledger_mode
                ),
            }
        )
    except Exception as exc:
        return _api_error(
            "API /api/cash_flows 오류", exc, "적립 기록을 불러오지 못했습니다"
        )


_RUNTIME_CACHE: dict = {"at": 0.0, "data": None}
_RUNTIME_TTL_SEC = 60.0


async def handle_api_runtime(_request: web.Request) -> web.Response:
    """느린 외부 시장 조회를 이벤트 루프 밖에서 실행하고 60초간 캐시한다."""
    import asyncio
    import time as _time

    try:
        now = _time.monotonic()
        if (
            _RUNTIME_CACHE["data"] is not None
            and now - _RUNTIME_CACHE["at"] < _RUNTIME_TTL_SEC
        ):
            cached = _RUNTIME_CACHE["data"]
        else:
            cached = await asyncio.to_thread(get_runtime_json)
            _RUNTIME_CACHE["at"] = now
            _RUNTIME_CACHE["data"] = cached
        # HALT는 안전 판단의 현재값이므로 느린 시장 상태 캐시와 분리한다.
        data = dict(cached)
        data["trading_halt"] = await asyncio.to_thread(_get_trading_halt_json)
        return web.json_response(data)
    except Exception as exc:
        return _api_error(
            "API /api/runtime 오류", exc, "안전 상태를 불러오지 못했습니다"
        )


async def handle_api_snapshots(request: web.Request) -> web.Response:
    try:
        requested_days = int(request.query.get("days", 30))
        days = max(1, min(3650, requested_days))
        raw_key = request.query.get("account_key")
        account_key = raw_key if raw_key is not None else None
        return web.json_response(
            get_snapshots_json(days=days, account_key=account_key)
        )
    except Exception as exc:
        return _api_error(
            "API /api/snapshots 오류", exc, "성과 기록을 불러오지 못했습니다"
        )


_BASKET_EVAL_CACHE: dict = {"at": 0.0, "data": None}
_BASKET_EVAL_TTL_SEC = 60.0


async def handle_api_basket_evaluation(_request: web.Request) -> web.Response:
    """바스켓 paper 운영 평가를 읽기 전용으로 반환한다."""
    import asyncio
    import time as _time

    def _collect_all() -> dict:
        from core.basket_evaluation import collect_basket_paper_evaluation
        from core.basket_rebalancer import BasketRebalancer

        evaluations = []
        for name in BasketRebalancer.get_enabled_baskets():
            result, basket_name = collect_basket_paper_evaluation(
                include_benchmark=False,
                basket_name=name,
            )
            evaluations.append(
                {
                    "basket": basket_name,
                    "verdict": result.get("verdict"),
                    "progress_days": result.get("progress_days"),
                    "min_trading_days": result.get("min_trading_days"),
                    "snapshot_coverage": result.get("snapshot_coverage"),
                    "issues": result.get("issues", []),
                }
            )
        return {"evaluations": evaluations}

    try:
        now = _time.monotonic()
        if (
            _BASKET_EVAL_CACHE["data"] is not None
            and now - _BASKET_EVAL_CACHE["at"] < _BASKET_EVAL_TTL_SEC
        ):
            return web.json_response(_BASKET_EVAL_CACHE["data"])

        payload = await asyncio.to_thread(_collect_all)
        _BASKET_EVAL_CACHE["at"] = now
        _BASKET_EVAL_CACHE["data"] = payload
        return web.json_response(payload)
    except Exception as exc:
        return _api_error(
            "API /api/basket_evaluation 오류",
            exc,
            "모의 운용 검증 상태를 불러오지 못했습니다",
        )


def create_app() -> web.Application:
    web_mod = _require_aiohttp_web()
    app = web_mod.Application(middlewares=[web_mod.middleware(_security_headers)])
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/portfolio", handle_api_portfolio)
    app.router.add_get("/api/runtime", handle_api_runtime)
    app.router.add_get("/api/snapshots", handle_api_snapshots)
    app.router.add_get("/api/baskets", handle_api_baskets)
    app.router.add_post("/api/deposit", handle_api_deposit)
    app.router.add_get("/api/cash_flows", handle_api_cash_flows)
    app.router.add_get("/api/basket_evaluation", handle_api_basket_evaluation)
    app.router.add_static(
        "/static/",
        path=str(_STATIC_PATH),
        name="dashboard_static",
        show_index=False,
    )
    return app


def _config_settings_dict(config) -> dict:
    settings = getattr(config, "settings", {})
    if callable(settings):
        settings = settings()
    return settings if isinstance(settings, dict) else {}


def resolve_dashboard_bind(
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> tuple[str, int]:
    """대시보드 바인드 주소를 해석한다. 기본은 로컬 루프백이다."""
    try:
        config = Config.get()
        settings = _config_settings_dict(config)
        dashboard_config = settings.get("dashboard") or {}
        host = (
            host
            or str(dashboard_config.get("host") or "").strip()
            or DEFAULT_HOST
        )
        port = (
            port
            if port is not None
            else dashboard_config.get("port") or DEFAULT_PORT
        )
    except Exception:
        host = host or DEFAULT_HOST
        port = port if port is not None else DEFAULT_PORT
    normalized_host = str(host).strip().lower()
    try:
        is_loopback = (
            normalized_host == "localhost"
            or ipaddress.ip_address(normalized_host).is_loopback
        )
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ValueError(
            "웹 대시보드는 인증을 제공하지 않으므로 loopback 주소에만 바인딩할 수 있습니다"
        )
    return str(host), int(port)


def run_web_dashboard(
    host: Optional[str] = None,
    port: Optional[int] = None,
):
    """웹 대시보드 서버를 실행한다."""
    host, port = resolve_dashboard_bind(host=host, port=port)
    web_mod = _require_aiohttp_web()
    logger.info("눈금 웹 대시보드 시작: http://{}:{}/", host, port)
    web_mod.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="눈금 NUNGUM 웹 대시보드")
    parser.add_argument(
        "--host", default=None, help="바인드 주소 (기본: config 또는 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="포트 (기본: config 또는 8080)"
    )
    args = parser.parse_args()

    from database.models import init_database
    from monitoring.logger import setup_logger

    setup_logger()
    init_database()
    run_web_dashboard(host=args.host, port=args.port)

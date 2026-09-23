"""
눈금(NUNGUM) 웹 대시보드.

대시보드는 장부와 런타임 상태를 읽어 사용자가 오늘 해야 할 일, 장기 성과,
실전 전환 준비도를 한 화면에서 이해하도록 돕는다. 웹에서 가능한 쓰기는
적립금 기록뿐이며 매매와 설정 변경은 의도적으로 제공하지 않는다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import ipaddress
from pathlib import Path
import re
import time
from typing import Optional

try:
    from aiohttp import web
except ModuleNotFoundError:
    web = None
from loguru import logger

from config.config_loader import Config
from database.repositories import get_portfolio_snapshots


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


def get_portfolio_json(current_prices: Optional[dict] = None) -> dict:
    """이전 기본 계정('' 계정)의 요약 — DB만 읽는다.

    예전에는 계정 키 ''를 '전 계정 합산'으로 해석하는 경로를 거쳐, 바스켓 두 계정의
    체결을 섞은 가짜 계좌(-4.44%, ETF 행 중복)를 '이전 기본 계좌'로 보여 줬다. live에서는
    조회할 때마다 증권사 토큰을 새로 받을 수 있었다. 이 화면은 증권사에 연결하지 않는다.
    기본 계정에 아무 기록이 없으면 {"empty": True}를 돌려주고 화면은 패널을 숨긴다.
    """
    from database.repositories import (
        get_all_positions,
        get_cash_flow_total,
        get_trade_cash_summary,
        get_trade_history,
    )

    config = Config.get()
    ledger_mode = _active_ledger_mode(config)
    base = {"timestamp": datetime.now().isoformat(), "mode": ledger_mode}
    positions = [
        p for p in (get_all_positions(account_key="", mode=ledger_mode) or [])
        if (p.quantity or 0) > 0
    ]
    trades = get_trade_history(mode=ledger_mode, account_key="") or []
    if not positions and not trades:
        return {**base, "empty": True}

    initial = float(
        (config.risk_params.get("position_sizing") or {}).get("initial_capital", 10_000_000)
    )
    prices = current_prices or {}
    deposits = float(get_cash_flow_total(account_key="", mode=ledger_mode) or 0)
    cash_delta = float(
        (get_trade_cash_summary(mode=ledger_mode, account_key="") or {}).get("cash_delta") or 0
    )
    cash = initial + deposits + cash_delta
    details = []
    invested = current_value = 0.0
    for p in positions:
        qty = int(p.quantity or 0)
        avg = float(p.avg_price or 0)
        price = float(prices.get(p.symbol, avg) or avg)
        invested += avg * qty
        current_value += price * qty
        details.append({
            "symbol": p.symbol, "quantity": qty, "avg_price": avg,
            "current_price": price, "invested": avg * qty,
            "current_value": price * qty, "pnl": (price - avg) * qty,
            "pnl_rate": ((price / avg) - 1) * 100 if avg > 0 else 0.0,
        })
    total_value = cash + current_value
    principal = initial + deposits
    return {
        **base,
        "empty": False,
        "initial_capital": initial,
        "total_value": total_value,
        "cash": cash,
        "invested": invested,
        "current_value": current_value,
        "total_return": ((total_value / principal) - 1) * 100 if principal > 0 else 0.0,
        "mdd": None,
        "position_count": len(details),
        "realized_pnl": cash + invested - principal,
        "unrealized_pnl": current_value - invested,
        "positions": details,
    }


_CALENDAR = None


def _calendar():
    """KRX 거래일 달력(프로세스당 한 번 로드)."""
    global _CALENDAR
    if _CALENDAR is None:
        from core.trading_hours import TradingHours

        _CALENDAR = TradingHours()
    return _CALENDAR


def _snapshot_freshness(last_date, now: Optional[datetime] = None) -> dict:
    """일일 사이클이 남겼어야 할 마지막 기록일과, 그 뒤로 빠진 거래일 수(KRX 달력 기준).

    예전 화면 규칙은 달력 날짜(4일)와 스케줄러 루프 나이(720분)였다. 이 배포에서는
    스케줄러가 돌지 않아 루프 경보가 밤·주말·휴장일마다 울렸고, 반대로 이틀 연속
    사이클이 죽어도 '정상'이었다. 사이클은 평일 10시대에 돌므로 10:30 전의 오늘은
    아직 기대하지 않는다.
    """
    from datetime import time as _time, timedelta as _td
    from core.trading_hours import _now_kst

    now = now or _now_kst()
    th = _calendar()
    day = now.date()
    if not (th.is_trading_day(datetime(day.year, day.month, day.day)) and now.time() >= _time(10, 30)):
        day -= _td(days=1)
    for _ in range(20):
        if th.is_trading_day(datetime(day.year, day.month, day.day)):
            break
        day -= _td(days=1)
    expected = day
    if last_date is None:
        return {"expected_snapshot_date": expected.isoformat(), "missed_trading_days": None}
    last = last_date.date() if hasattr(last_date, "date") else last_date
    missed = 0
    d = last + _td(days=1)
    while d <= expected and missed < 60:
        if th.is_trading_day(datetime(d.year, d.month, d.day)):
            missed += 1
        d += _td(days=1)
    return {"expected_snapshot_date": expected.isoformat(), "missed_trading_days": missed}


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
    from core.risk_overlays import (
        overlay_target_weights,
        describe_decision,
        load_overlay_state,
        parse_overlay_config,
    )
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
            latest_measured = None
            if latest is not None:
                # 스냅샷을 실제로 찍은 시각. 이 시각 뒤의 입금은 아직 평가액에 없다.
                latest_measured = latest.created_at or datetime.combine(
                    latest.date.date() if hasattr(latest.date, "date") else latest.date,
                    datetime.max.time(),
                )
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

        # 원금 대비 손익은 스냅샷 시점의 원금으로 잰다. 전체 원금(방금 넣은 적립 포함)과
        # 비교하면 적립 직후 다음 사이클까지 '원금 대비 -21%' 같은 가짜 손실이 보인다
        # (연휴 앞 적립이면 며칠씩). 아직 반영 안 된 적립은 따로 알려 준다.
        principal_at_snapshot = principal
        pending_deposits = 0.0
        if latest_measured is not None:
            deposits_at_snapshot = float(
                get_cash_flow_total(
                    account_key=account_key, until=latest_measured, mode=ledger_mode,
                ) or 0
            )
            principal_at_snapshot = initial_capital + deposits_at_snapshot
            pending_deposits = deposits_total - deposits_at_snapshot
        try:
            freshness = _snapshot_freshness(latest.date if latest is not None else None)
        except Exception as exc:
            logger.warning("바스켓 '{}' 기록 공백 계산 실패: {}", name, exc)
            freshness = {"expected_snapshot_date": None, "missed_trading_days": None}

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
        base_fraction = effective_stock_fraction(basket_config, config.risk_params)
        # 리스크 오버레이(추세 필터·낙폭 제어)가 켜진 바스켓은 마지막 실행이 남긴 배수를
        # 곱한 '적용 비중'이 그날의 목표다. 화면의 목표 비중·목표 범위 판정은 적용 비중을 쓴다.
        overlay_cfg = parse_overlay_config(basket_config)
        overlay_state = load_overlay_state(name, mode=ledger_mode) if overlay_cfg.any_enabled else None
        scale = float((overlay_state or {}).get("scale", 1.0))
        target_weights = overlay_target_weights(
            basket_config.get("holdings") or {}, base_fraction, scale,
            (basket_config.get("overlays") or {}).get("defensive_symbol"),
        )
        design_fraction = sum(target_weights.values())
        overlay = None
        if overlay_cfg.any_enabled:
            overlay = {
                "enabled": True,
                "scale": scale if overlay_state else None,
                "summary": describe_decision(overlay_state),
                "reasons": list((overlay_state or {}).get("reasons") or []),
                "data_issues": list((overlay_state or {}).get("data_issues") or []),
                "evaluated_at": (overlay_state or {}).get("evaluated_at"),
                "trend_filter": overlay_cfg.trend.enabled,
                "drawdown_guard": overlay_cfg.drawdown.enabled,
                "volatility_target": overlay_cfg.volatility.enabled,
                "trend_below": (overlay_state or {}).get("trend_below"),
                "drawdown_active": (overlay_state or {}).get("drawdown_active"),
                "defensive_symbol": (basket_config.get("overlays") or {}).get("defensive_symbol"),
                "source_dates": (overlay_state or {}).get("source_dates") or {},
            }
        # 종목별 목표 비중(총자산 대비) = 바스켓 내 비중 정규화 × 적용 투자 비중.
        # 현재가는 장부에 저장하지 않으므로 화면은 매입금액 기준 비중과 나란히 보여준다.
        holdings_cost = float(sum(position["invested"] for position in positions))
        holdings_value = (
            snapshot["total_value"] - snapshot["cash"] if snapshot else None
        )
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
                "principal_at_snapshot": principal_at_snapshot,
                "pending_deposits": pending_deposits,
                "snapshot": snapshot,
                "profit_vs_principal": (
                    snapshot["total_value"] - principal_at_snapshot if snapshot else None
                ),
                "expected_snapshot_date": freshness["expected_snapshot_date"],
                "missed_trading_days": freshness["missed_trading_days"],
                "deployment_ratio": deployment_ratio,
                "design_fraction": design_fraction,
                "base_stock_fraction": base_fraction,
                "overlay": overlay,
                "target_weights": target_weights,
                "holdings_cost": holdings_cost,
                "holdings_value": holdings_value,
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

        from core.market_regime import resolve_market_regime_config

        config = Config.get()
        if not resolve_market_regime_config(config).get("enabled"):
            # 필터가 꺼져 있으면 check_market_regime은 매수 허용용 기본값(상승)을 돌려준다.
            # 그걸 그대로 '상승 추세'로 보여 주면 시장과 무관하게 늘 초록불이다.
            out["market_regime"] = {"regime": "disabled", "position_scale": None, "allow_buys": None}
        else:
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

    out.update(_scheduler_freshness(out))
    return out


def _scheduler_freshness(runtime: dict, now: Optional[datetime] = None) -> dict:
    """상시 스케줄러를 쓰는 경우에만, 장중에 루프가 멈췄는지 판정한다.

    이 배포의 매매는 일일 CLI 사이클이 한다(스케줄러 없음). 스케줄러 기록이 최근
    7일 안에 없으면 '사용 안 함'으로 보고 경보하지 않는다. 쓰는 중이면 장이 열려
    있는 동안 60분 넘게 루프 기록이 없을 때만 멈춘 것으로 본다(밤·주말·휴장일 제외).
    """
    from datetime import timedelta as _td
    from core.trading_hours import _now_kst

    now = now or _now_kst()

    def _parse(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed.replace(tzinfo=None)

    loop = runtime.get("loop_metrics") or {}
    last = _parse(loop.get("last_success")) or _parse(runtime.get("runtime_file_updated_at"))
    in_use = last is not None and now - last <= _td(days=7)
    stale = False
    if in_use:
        try:
            stale = bool(_calendar().is_market_open(now)) and now - last > _td(minutes=60)
        except Exception as exc:
            logger.warning("스케줄러 멈춤 여부 계산 실패: {}", exc)
    return {"scheduler_in_use": in_use, "scheduler_stale": stale}


def _html_page() -> str:
    """파일 기반 템플릿을 읽어 UI와 Python 데이터 계층을 분리한다."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _api_error(label: str, exc: Exception, message: str) -> web.Response:
    """내부 예외는 로그에만 남기고 브라우저에는 고정 문구만 반환한다."""
    logger.exception("{}: {}", label, exc)
    return web.json_response({"error": message}, status=500)


async def _security_headers(request: web.Request, handler):
    response = await handler(request)
    static_asset = request.path.startswith("/static/")
    # 정적 파일은 변경 여부를 확인해 재사용한다. 계좌 응답은 디스크에 캐시하지 않는다.
    response.headers["Cache-Control"] = "no-cache" if static_asset else "no-store"
    if static_asset and request.path.endswith((".css", ".js", ".svg")):
        response.enable_compression()
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
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
        return web.json_response(await asyncio.to_thread(get_baskets_json))
    except Exception as exc:
        return _api_error(
            "API /api/baskets 오류", exc, "포트폴리오를 불러오지 못했습니다"
        )


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_local_origin(request) -> bool:
    """Host가 루프백이고, Origin/Referer가 있으면 같은 호스트여야 한다.

    커스텀 헤더만으로는 DNS 리바인딩(외부 도메인이 127.0.0.1을 가리키게 하는 공격)을
    막지 못한다 — 그 경우 브라우저는 같은 출처로 보고 헤더를 붙여 보낸다. Host가
    루프백이 아니면 이 대시보드로 온 요청이 아니다. 포트는 테스트·캡처마다 달라 보지 않는다.
    """
    from urllib.parse import urlsplit

    host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") \
        else (request.host or "").split("]", 1)[0] + "]"
    if host.lower() not in _LOOPBACK_HOSTS:
        return False
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value:
            origin_host = (urlsplit(value).hostname or "").lower()
            if origin_host not in {h.strip("[]") for h in _LOOPBACK_HOSTS}:
                return False
    return True


async def handle_api_deposit(request: web.Request) -> web.Response:
    """적립금 기록. 커스텀 헤더로 cross-site 브라우저 요청을 차단한다."""
    if not _is_local_origin(request):
        return web.json_response(
            {"ok": False, "error": "이 컴퓨터에서 연 대시보드에서만 기록할 수 있습니다"},
            status=403,
        )
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
        from core.basket_rebalancer import BasketRebalancer
        from tools.record_deposit import record_basket_deposit

        basket_name = str(body.get("basket") or "")
        if basket_name not in BasketRebalancer.get_enabled_baskets():
            return web.json_response(
                {"ok": False, "error": "운용 중인 계좌가 아닙니다"}, status=400
            )
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
                # 차트·CSV의 누적 원금은 전체 기록으로 계산해야 한다(12건 제한이면
                # 13번째 적립부터 원금이 적게 잡힌다).
                "flows": await asyncio.to_thread(
                    get_recent_cash_flows, account_key, None, ledger_mode
                ),
            }
        )
    except Exception as exc:
        return _api_error(
            "API /api/cash_flows 오류", exc, "적립 기록을 불러오지 못했습니다"
        )


class _ReadCache:
    """같은 앱의 동시 조회는 한 번만 수집하고, 완료 시점부터 캐시한다."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._data = None
        self._mode = None
        self._expires_at = 0.0

    async def get(self, collect, ttl: float, mode: str):
        async with self._lock:
            if (
                self._data is not None
                and self._mode == mode
                and time.monotonic() < self._expires_at
            ):
                return self._data
            data = await asyncio.to_thread(collect)
            self._data = data
            self._mode = mode
            self._expires_at = time.monotonic() + ttl
            return data


_RUNTIME_CACHE_KEY = web.AppKey("runtime_cache", _ReadCache) if web else None
_BASKET_EVAL_CACHE_KEY = web.AppKey("basket_eval_cache", _ReadCache) if web else None
_RUNTIME_TTL_SEC = 60.0


async def handle_api_runtime(request: web.Request) -> web.Response:
    """느린 외부 시장 조회를 이벤트 루프 밖에서 실행하고 60초간 캐시한다."""
    try:
        cached = await request.app[_RUNTIME_CACHE_KEY].get(
            get_runtime_json, _RUNTIME_TTL_SEC, _active_ledger_mode()
        )
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
            await asyncio.to_thread(get_snapshots_json, days=days, account_key=account_key)
        )
    except Exception as exc:
        return _api_error(
            "API /api/snapshots 오류", exc, "성과 기록을 불러오지 못했습니다"
        )


_BASKET_EVAL_TTL_SEC = 60.0


async def handle_api_basket_evaluation(request: web.Request) -> web.Response:
    """바스켓 paper 운영 평가를 읽기 전용으로 반환한다."""
    def _collect_all() -> dict:
        from core.basket_evaluation import collect_basket_paper_evaluation
        from core.basket_rebalancer import BasketRebalancer

        evaluations = []
        for name in BasketRebalancer.get_enabled_baskets():
            try:
                result, basket_name = collect_basket_paper_evaluation(
                    include_benchmark=False,
                    basket_name=name,
                )
            except Exception as exc:
                # 한 계좌의 실패가 다른 계좌의 판정까지 가리지 않게 따로 남긴다
                logger.warning("바스켓 '{}' 검증 상태 조회 실패: {}", name, exc)
                evaluations.append({"basket": name, "error": "검증 상태를 확인할 수 없습니다"})
                continue
            evaluations.append(
                {
                    "basket": basket_name,
                    "verdict": result.get("verdict"),
                    "paper_only": bool(result.get("paper_only", False)),
                    "review_note": result.get("review_note"),
                    "progress_days": result.get("progress_days"),
                    "min_trading_days": result.get("min_trading_days"),
                    "snapshot_coverage": result.get("snapshot_coverage"),
                    "measured_coverage": result.get("measured_coverage"),
                    "reconstructed_days": result.get("reconstructed_days", 0),
                    "issues": result.get("issues", []),
                }
            )
        return {"evaluations": evaluations}

    try:
        payload = await request.app[_BASKET_EVAL_CACHE_KEY].get(
            _collect_all, _BASKET_EVAL_TTL_SEC, _active_ledger_mode()
        )
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
    app[_RUNTIME_CACHE_KEY] = _ReadCache()
    app[_BASKET_EVAL_CACHE_KEY] = _ReadCache()
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

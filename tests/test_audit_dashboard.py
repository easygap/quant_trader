"""대시보드 회귀 테스트 (2026-09-23 점검).

- 기록 공백은 KRX 거래일로 잰다(달력 날짜·스케줄러 루프 나이 규칙은 밤·주말·휴장일마다
  거짓 경보를 내고, 평일 이틀 공백은 '정상'으로 보였다).
- 원금 대비 손익은 스냅샷 시점 원금으로 잰다(적립 직후 가짜 손실 방지).
- '이전 기본 계좌'는 기본 계정만 DB에서 읽는다(전 계정 합산 가짜 계좌 금지).
- 적립 기록은 로컬 대시보드에서, 운용 중인 계좌에만.
"""

from datetime import date, datetime
from unittest.mock import patch

import pytest

from database.models import init_database


# ------------------------------------------------------------ 기록 공백

def test_freshness_counts_trading_days_only():
    from monitoring.web_dashboard import _snapshot_freshness

    # 추석 연휴 뒤 첫 거래일(9/28 월) 11:00 — 마지막 기록 9/23이면 9/28 하루만 빠진 것
    out = _snapshot_freshness(date(2026, 9, 23), now=datetime(2026, 9, 28, 11, 0))
    assert out == {"expected_snapshot_date": "2026-09-28", "missed_trading_days": 1}


def test_freshness_does_not_expect_today_before_cycle_time():
    from monitoring.web_dashboard import _snapshot_freshness

    out = _snapshot_freshness(date(2026, 9, 23), now=datetime(2026, 9, 28, 9, 0))
    assert out["expected_snapshot_date"] == "2026-09-23"
    assert out["missed_trading_days"] == 0


def test_freshness_quiet_over_weekend_and_holidays():
    from monitoring.web_dashboard import _snapshot_freshness

    for now in (datetime(2026, 9, 24, 12, 0), datetime(2026, 9, 26, 12, 0),
                datetime(2026, 9, 27, 23, 0)):
        assert _snapshot_freshness(date(2026, 9, 23), now=now)["missed_trading_days"] == 0


def test_freshness_flags_two_dead_weekdays():
    from monitoring.web_dashboard import _snapshot_freshness

    out = _snapshot_freshness(date(2026, 9, 16), now=datetime(2026, 9, 18, 11, 0))
    assert out["missed_trading_days"] == 2


# ------------------------------------------------------------ 스케줄러 감시

def test_unused_scheduler_is_never_stale():
    from monitoring.web_dashboard import _scheduler_freshness

    out = _scheduler_freshness({}, now=datetime(2026, 9, 22, 11, 0))
    assert out == {"scheduler_in_use": False, "scheduler_stale": False}


def test_scheduler_stale_only_during_market_hours():
    from monitoring.web_dashboard import _scheduler_freshness

    runtime = {"loop_metrics": {"last_success": "2026-09-22T09:10:00"}}
    assert _scheduler_freshness(runtime, now=datetime(2026, 9, 22, 11, 0))["scheduler_stale"] is True
    assert _scheduler_freshness(runtime, now=datetime(2026, 9, 22, 20, 0))["scheduler_stale"] is False


# ------------------------------------------------------------ 시장 국면 표시

def test_disabled_regime_filter_is_not_shown_as_bullish(monkeypatch):
    from monitoring import web_dashboard as wd

    monkeypatch.setattr("core.market_regime.resolve_market_regime_config",
                        lambda config, **kw: {"enabled": False})
    out = wd.get_runtime_json()
    assert out["market_regime"]["regime"] == "disabled"


# ------------------------------------------------------------ 이전 기본 계좌

def test_legacy_panel_is_empty_when_default_account_has_no_records():
    from monitoring import web_dashboard as wd

    init_database()
    with patch("database.repositories.get_all_positions", return_value=[]), \
            patch("database.repositories.get_trade_history", return_value=[]):
        out = wd.get_portfolio_json()
    assert out["empty"] is True


# ------------------------------------------------------------ 스냅샷 시점 원금

def test_principal_is_measured_at_snapshot_time():
    """스냅샷(9/22 10:07) 뒤 적립 10만 원은 아직 평가액에 없다 — 손익 비교에서 뺀다."""
    from monitoring import web_dashboard as wd
    from database.repositories import record_cash_flow, save_portfolio_snapshot

    init_database()
    name = "kr_pocket_pending"
    key = f"basket_rebalance:{name}"
    cfg = {name: {"enabled": True, "initial_capital": 300_000,
                  "holdings": {"069500": 0.5, "357870": 0.5},
                  "target_stock_weight": 0.95}}
    save_portfolio_snapshot(
        total_value=390_000, cash=100_000, invested=290_000, account_key=key,
        snapshot_date=datetime(2026, 9, 22), mode="paper",
        measured_at=datetime(2026, 9, 22, 10, 7),
    )
    record_cash_flow(100_000, account_key=key, occurred_at=datetime(2026, 9, 22, 18, 0))
    with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config", return_value=cfg):
        out = wd.get_baskets_json()
    b = next(x for x in out["baskets"] if x["basket"] == name)
    assert b["principal"] == pytest.approx(400_000)
    assert b["principal_at_snapshot"] == pytest.approx(300_000)
    assert b["pending_deposits"] == pytest.approx(100_000)
    assert b["profit_vs_principal"] == pytest.approx(90_000)   # -1만이 아니라 +9만


# ------------------------------------------------------------ 적립 기록 보호

def _deposit(host=None, origin=None, basket="kr_pocket_host", enabled=True):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from monitoring import web_dashboard as wd

    init_database()
    cfg = {basket: {"enabled": enabled, "initial_capital": 300_000,
                    "holdings": {"069500": 1.0}}}
    result = {}

    async def run():
        with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                   return_value=cfg):
            client = TestClient(TestServer(wd.create_app()))
            await client.start_server()
            try:
                headers = {"X-Requested-With": "quant-dashboard",
                           "Idempotency-Key": f"test-host-check-{basket}-0001"}
                if host:
                    headers["Host"] = host
                if origin:
                    headers["Origin"] = origin
                res = await client.post("/api/deposit", json={"basket": basket, "amount": 1000},
                                        headers=headers)
                result["status"] = res.status
            finally:
                await client.close()

    asyncio.run(run())
    return result["status"]


def test_deposit_rejects_non_loopback_host():
    """DNS 리바인딩: 외부 도메인이 127.0.0.1을 가리켜도 Host 헤더는 그 도메인이다."""
    assert _deposit(host="evil.example:8080") == 403


def test_deposit_rejects_cross_origin():
    assert _deposit(origin="https://evil.example") == 403


def test_deposit_rejects_disabled_basket():
    assert _deposit(basket="kr_pocket_off", enabled=False) == 400


def test_deposit_accepts_local_request():
    assert _deposit(origin="http://127.0.0.1:8080", basket="kr_pocket_ok") == 200


# ------------------------------------------------------------ 경보 채널

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def _bot():
    from monitoring.discord_bot import DiscordBot

    bot = DiscordBot.__new__(DiscordBot)
    bot.enabled = True
    bot.webhook_url = "https://discord.example/api/webhooks/1/SECRET"
    bot.username = "t"
    bot.avatar_url = ""
    return bot


def test_discord_rejection_is_logged_without_webhook_secret(monkeypatch):
    import monitoring.discord_bot as db
    from loguru import logger

    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        monkeypatch.setattr(db.req, "post", lambda *a, **k: _Resp(404, text="Unknown Webhook"))
        assert _bot().send_message("hi") is False
    finally:
        logger.remove(sink)
    joined = "".join(lines)
    assert "ALERT_FAILED" in joined and "404" in joined
    assert "SECRET" not in joined


def test_discord_429_retries_once(monkeypatch):
    import monitoring.discord_bot as db

    calls = []

    def _post(*a, **k):
        calls.append(1)
        return _Resp(429, {"retry_after": 0}) if len(calls) == 1 else _Resp(204)

    monkeypatch.setattr(db.req, "post", _post)
    assert _bot().send_message("hi") is True
    assert len(calls) == 2


def test_lost_alert_is_reported_when_every_channel_fails(monkeypatch):
    from core.notifier import Notifier

    n = Notifier.__new__(Notifier)
    lost = []
    monkeypatch.setattr(n, "_discord_send_message", lambda text: False, raising=False)
    monkeypatch.setattr(n, "_send_email_tracked", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(n, "_discord_deliverable", lambda: True, raising=False)
    monkeypatch.setattr(n, "_report_lost_alert", lambda t, x: lost.append(t), raising=False)
    n.send_message("경보", critical=True)
    assert lost == ["알림"]

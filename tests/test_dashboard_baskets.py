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


class TestSnapshotsSerialization:
    """HIGH 회귀 고정: created_at(pd.Timestamp) 컬럼 추가 후 /api/snapshots가
    매 폴링 500이 나고 차트가 조용히 죽던 문제 — 비어 있지 않은 DF로 검증해야 잡힌다."""

    def test_serializer_handles_all_datetime_columns(self):
        import json
        import pandas as pd
        from monitoring.web_dashboard import _serialize_snapshots

        df = pd.DataFrame([{
            "date": pd.Timestamp("2026-07-07"),
            "created_at": pd.Timestamp("2026-07-07 10:07:12"),
            "total_value": 300_126.0,
            "cumulative_return": 0.04,
        }])
        out = _serialize_snapshots(df)
        json.dumps(out)  # 직렬화 가능해야 한다 (회귀 시 TypeError)
        assert out[0]["date"] == "2026-07-07"
        assert out[0]["created_at"].startswith("2026-07-07 10:07")

    @pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
    def test_snapshots_endpoint_200_with_real_rows(self):
        # 실제 스냅샷 행(created_at 포함)이 있을 때 200 — 빈 DF 경로만 타던 구멍 방지.
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd

        name = "kr_pocket_snap200"
        acct = _seed_pocket(name)

        async def run():
            app = wd.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                res = await client.get(
                    "/api/snapshots?days=30&account_key=" + acct
                )
                assert res.status == 200
                data = await res.json()
            finally:
                await client.close()
            assert len(data["snapshots"]) == 1
            assert data["snapshots"][0]["total_value"] == 400_126

        asyncio.run(run())

    @pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
    def test_empty_account_key_filters_default_account_only(self):
        # account_key=(빈 값)은 기본 계정('')만 — 무필터(전 계정 혼합)로 강등되면
        # 10M/30만 스케일 시계열이 한 차트에 섞인다.
        import asyncio
        from datetime import datetime as _dt
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd
        from database.models import PortfolioSnapshot, get_session

        _seed_pocket("kr_pocket_mix")  # 바스켓 계정 행
        session = get_session()
        try:
            session.add(PortfolioSnapshot(
                account_key="", date=_dt(2026, 7, 7),
                total_value=10_000_000, cash=10_000_000, invested=0,
            ))
            session.commit()
        finally:
            session.close()

        async def run():
            app = wd.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                res = await client.get("/api/snapshots?days=30&account_key=")
                assert res.status == 200
                data = await res.json()
            finally:
                await client.close()
            vals = [s["total_value"] for s in data["snapshots"]]
            assert vals == [10_000_000]  # 기본 계정 행만 — 바스켓 행 미포함

        asyncio.run(run())


def test_html_page_contains_basket_tracks_section():
    from monitoring.web_dashboard import _html_page

    html = _html_page()
    assert "basketTracks" in html          # 섹션
    assert "/api/baskets" in html          # 폴링 대상
    assert "chartAccount" in html          # 차트 계정 선택기
    assert "/api/deposit" in html          # 웹 입금 폼
    assert "depositOverlay" in html        # 입금 모달


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
class TestDepositEndpoint:
    """POST /api/deposit — 웹의 유일한 쓰기. CLI와 동일한 검증 경로 계약."""

    def _serve(self, coro):
        import asyncio
        asyncio.run(coro)

    def test_deposit_records_and_returns_totals(self):
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd
        from database.repositories import get_cash_flow_total

        name = "kr_pocket_dep"
        init_database()

        async def run():
            with patch(
                "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                return_value=_cfg(name),
            ):
                app = wd.create_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    res = await client.post(
                        "/api/deposit",
                        json={"basket": name, "amount": 100000, "note": "웹 테스트"},
                        headers={"X-Requested-With": "quant-dashboard"},
                    )
                    assert res.status == 200
                    data = await res.json()
                finally:
                    await client.close()
                assert data["ok"] is True
                assert data["deposits_total"] == 100000
                assert data["principal"] == 400000  # 초기 30만 + 입금 10만

        asyncio.run(run())
        from core.basket_rebalancer import rebalance_live_strategy_id
        assert get_cash_flow_total(
            account_key=rebalance_live_strategy_id(name)
        ) == pytest.approx(100_000)

    def test_deposit_rejects_bad_amount_and_unknown_basket(self):
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd

        init_database()

        async def run():
            with patch(
                "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                return_value=_cfg("kr_pocket_dep2"),
            ):
                app = wd.create_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    h = {"X-Requested-With": "quant-dashboard"}
                    r1 = await client.post("/api/deposit", json={"basket": "kr_pocket_dep2", "amount": 0}, headers=h)
                    r2 = await client.post("/api/deposit", json={"basket": "no_such", "amount": 1000}, headers=h)
                    r3 = await client.post("/api/deposit", data=b"not-json", headers=h)
                    assert r1.status == 400 and (await r1.json())["ok"] is False
                    assert r2.status == 400 and (await r2.json())["ok"] is False
                    assert r3.status == 400
                finally:
                    await client.close()

        asyncio.run(run())

    def test_deposit_without_csrf_header_is_403(self):
        # CSRF 방어: 커스텀 헤더 없는 POST(브라우저 경유 cross-site 요청 모사)는
        # 검증 전에 차단되고 아무것도 기록되지 않아야 한다.
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd
        from core.basket_rebalancer import rebalance_live_strategy_id
        from database.repositories import get_cash_flow_total

        name = "kr_pocket_csrf"
        init_database()

        async def run():
            with patch(
                "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                return_value=_cfg(name),
            ):
                app = wd.create_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    res = await client.post(
                        "/api/deposit", json={"basket": name, "amount": 100000},
                    )
                    assert res.status == 403
                finally:
                    await client.close()

        asyncio.run(run())
        assert get_cash_flow_total(
            account_key=rebalance_live_strategy_id(name)
        ) == 0.0

    def test_deposit_rejects_nonfinite_json_literals(self):
        # python json.loads는 Infinity/NaN 리터럴을 기본 허용 — float('inf')>0 은 True,
        # nan<=0 은 False라 기존 양수 검사를 둘 다 통과해 무한대/NaN 입금이 기록되던
        # 실제 구멍. isfinite 검증으로 400이어야 한다.
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from monitoring import web_dashboard as wd
        from core.basket_rebalancer import rebalance_live_strategy_id
        from database.repositories import get_cash_flow_total

        name = "kr_pocket_inf"
        init_database()

        async def run():
            with patch(
                "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                return_value=_cfg(name),
            ):
                app = wd.create_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    for payload in (
                        b'{"basket": "kr_pocket_inf", "amount": Infinity}',
                        b'{"basket": "kr_pocket_inf", "amount": NaN}',
                        b'{"basket": "kr_pocket_inf", "amount": -Infinity}',
                    ):
                        res = await client.post(
                            "/api/deposit", data=payload,
                            headers={
                                "Content-Type": "application/json",
                                "X-Requested-With": "quant-dashboard",
                            },
                        )
                        assert res.status == 400, f"payload {payload!r} → {res.status}"
                finally:
                    await client.close()

        asyncio.run(run())
        assert get_cash_flow_total(
            account_key=rebalance_live_strategy_id(name)
        ) == 0.0  # 아무것도 기록되지 않아야 한다

    def test_deposit_trims_overlong_note(self):
        # SQLite는 String(200)을 강제하지 않는다 — 서버측 절단 계약.
        from tools.record_deposit import record_basket_deposit
        from core.basket_rebalancer import rebalance_live_strategy_id
        from database.repositories import get_recent_cash_flows

        name = "kr_pocket_note"
        init_database()
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_cfg(name),
        ):
            out = record_basket_deposit(name, 10_000, note="가" * 500)
        assert out["ok"] is True
        flows = get_recent_cash_flows(rebalance_live_strategy_id(name))
        assert len(flows[0]["note"]) == 200


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_cash_flows_endpoint_lists_recent():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from monitoring import web_dashboard as wd
    from database.repositories import record_cash_flow
    from core.basket_rebalancer import rebalance_live_strategy_id

    name = "kr_pocket_flows"
    init_database()
    record_cash_flow(
        100_000, account_key=rebalance_live_strategy_id(name),
        occurred_at=datetime(2026, 7, 6, 9, 0), note="7월 적립",
    )

    async def run():
        app = wd.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            res = await client.get("/api/cash_flows?basket=" + name)
            assert res.status == 200
            data = await res.json()
        finally:
            await client.close()
        assert data["flows"][0]["amount"] == 100000
        assert data["flows"][0]["note"] == "7월 적립"

    asyncio.run(run())


@pytest.mark.skipif(not _has_aiohttp, reason="aiohttp 미설치")
def test_cash_flows_endpoint_uses_active_ledger_mode():
    """실전 대시보드에서 동일 바스켓의 paper 입금 내역을 노출하지 않는다."""
    import asyncio
    from types import SimpleNamespace
    from aiohttp.test_utils import TestClient, TestServer
    from monitoring import web_dashboard as wd
    from database.repositories import record_cash_flow
    from core.basket_rebalancer import rebalance_live_strategy_id

    name = "kr_pocket_flows_mode_isolation"
    account_key = rebalance_live_strategy_id(name)
    init_database()
    record_cash_flow(
        100_000,
        account_key=account_key,
        occurred_at=datetime(2026, 7, 6, 9, 0),
        note="paper-only",
        mode="paper",
    )
    record_cash_flow(
        200_000,
        account_key=account_key,
        occurred_at=datetime(2026, 7, 6, 10, 0),
        note="live-only",
        mode="live",
    )

    async def run():
        app = wd.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch.object(
                wd.Config,
                "get",
                return_value=SimpleNamespace(trading={"mode": "live"}),
            ):
                res = await client.get("/api/cash_flows?basket=" + name)
                assert res.status == 200
                data = await res.json()
        finally:
            await client.close()
        assert [flow["note"] for flow in data["flows"]] == ["live-only"]

    asyncio.run(run())

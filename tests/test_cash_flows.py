"""적립(입금) + TWR 수익률 — docs/POCKET_TRACK_PLAN.md §4 구현 테스트.

핵심 계약:
1) 입금은 수익이 아니다 — 입금 직후 수익률·실현손익 점프 0.
2) MDD는 TWR 지수 기준 — 입금이 낙폭을 가짜 회복시키지 않는다.
3) 입금 0건 계정은 기존 산식과 완전히 동일(하위 호환).
"""
from datetime import datetime, timedelta

import pytest

from core.portfolio_manager import PortfolioManager, twr_period_return
from database.models import init_database
from database.repositories import (
    get_cash_flow_total,
    get_cash_flow_total_between,
    record_cash_flow,
)


class TestTwrPeriodReturn:
    def test_no_flow(self):
        assert twr_period_return(100, 110) == pytest.approx(0.10)

    def test_deposit_neutralized(self):
        # 300 → 입금 100 → 400: 수익 0
        assert twr_period_return(300, 400, flow=100) == pytest.approx(0.0)

    def test_deposit_plus_gain(self):
        # (300+100)=400 기반에서 440으로 → +10%
        assert twr_period_return(300, 440, flow=100) == pytest.approx(0.10)

    def test_zero_base_returns_zero(self):
        assert twr_period_return(0, 100, flow=0) == 0.0
        assert twr_period_return(100, 50, flow=-200) == 0.0  # 음수 분모 방어


class TestCashFlowRepo:
    def test_record_and_total(self):
        init_database()
        acct = "basket_rebalance:test_cf_total"
        record_cash_flow(100_000, account_key=acct)
        record_cash_flow(50_000, account_key=acct)
        assert get_cash_flow_total(account_key=acct) == pytest.approx(150_000)

    def test_zero_amount_rejected(self):
        init_database()
        with pytest.raises(ValueError):
            record_cash_flow(0, account_key="basket_rebalance:test_cf_zero")

    def test_nonfinite_amount_rejected_at_repo_layer(self):
        # NaN은 truthy라 `if not amount`를 통과하고, 기록되면 모든 합산이 오염된다
        # — 최종 방어선(repo)에서 차단.
        init_database()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                record_cash_flow(bad, account_key="basket_rebalance:test_cf_nonfinite")
        assert get_cash_flow_total(account_key="basket_rebalance:test_cf_nonfinite") == 0.0

    def test_between_boundaries(self):
        # after 배타 / until 포함
        init_database()
        acct = "basket_rebalance:test_cf_between"
        t0 = datetime(2026, 7, 1, 10, 0)
        t1 = datetime(2026, 7, 2, 9, 0)
        record_cash_flow(10_000, account_key=acct, occurred_at=t0)
        record_cash_flow(20_000, account_key=acct, occurred_at=t1)
        assert get_cash_flow_total_between(acct, after=t0, until=t1) == pytest.approx(20_000)
        assert get_cash_flow_total_between(
            acct, after=t0 - timedelta(seconds=1), until=t1,
        ) == pytest.approx(30_000)

    def test_isolated_by_account(self):
        init_database()
        record_cash_flow(10_000, account_key="basket_rebalance:test_cf_a")
        assert get_cash_flow_total(account_key="basket_rebalance:test_cf_b") == 0.0


def _pm(monkeypatch, account, initial=300_000, cash_delta=0.0, deposits=0.0,
        prev_snapshot=None, flow_since=0.0, hist_max_cum=None):
    """요약 산식 검증용 PortfolioManager — 저장소 의존을 모두 결정론으로 고정."""
    import core.portfolio_manager as pm_mod

    monkeypatch.setattr(pm_mod, "get_latest_peak_value", lambda account_key="": None)
    pm = PortfolioManager(account_key=account, initial_capital=initial)
    monkeypatch.setattr(pm_mod, "get_all_positions", lambda account_key=None: [])
    monkeypatch.setattr(
        pm_mod, "get_trade_cash_summary",
        lambda mode=None, account_key=None: {"cash_delta": cash_delta},
    )
    monkeypatch.setattr(pm_mod, "get_cash_flow_total", lambda account_key="": deposits)
    monkeypatch.setattr(
        pm_mod, "get_latest_snapshot_summary", lambda account_key="": prev_snapshot,
    )
    monkeypatch.setattr(
        pm_mod, "get_cash_flow_total_between", lambda ak, a, u: flow_since,
    )
    monkeypatch.setattr(
        pm_mod, "get_max_cumulative_return", lambda account_key="": hist_max_cum,
    )
    return pm


class TestBasketCapitalResolution:
    """바스켓 계정 키만으로 PM을 열어도 baskets.yaml 자본이 분모여야 한다.

    6차 점검 E2E 실측: 주문 리스크 가드(_drawdown_pre_order_check)가 account_key만으로
    PM을 열어 전역 10M 폴백 → 30만 pocket의 손실 한도가 사실상 무력화되던 결함.
    """

    def test_basket_key_resolves_basket_capital(self, monkeypatch):
        import core.portfolio_manager as pm_mod

        monkeypatch.setattr(pm_mod, "get_latest_peak_value", lambda account_key="": None)
        from unittest.mock import patch
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value={"kr_pocket": {"initial_capital": 300_000}},
        ):
            pm = PortfolioManager(account_key="basket_rebalance:kr_pocket")
        assert pm.initial_capital == 300_000

    def test_unknown_basket_falls_back_to_global(self, monkeypatch):
        import core.portfolio_manager as pm_mod

        monkeypatch.setattr(pm_mod, "get_latest_peak_value", lambda account_key="": None)
        from unittest.mock import patch
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value={},
        ):
            pm = PortfolioManager(account_key="basket_rebalance:no_such")
        assert pm.initial_capital >= 1_000_000  # 전역 폴백(설정값)

    def test_non_basket_key_unchanged(self, monkeypatch):
        import core.portfolio_manager as pm_mod

        monkeypatch.setattr(pm_mod, "get_latest_peak_value", lambda account_key="": None)
        pm = PortfolioManager(account_key="scoring")
        assert pm.initial_capital >= 1_000_000  # 기존 동작 그대로

    def test_explicit_capital_still_wins(self, monkeypatch):
        import core.portfolio_manager as pm_mod

        monkeypatch.setattr(pm_mod, "get_latest_peak_value", lambda account_key="": None)
        pm = PortfolioManager(
            account_key="basket_rebalance:kr_pocket", initial_capital=777,
        )
        assert pm.initial_capital == 777


class TestSummaryWithDeposits:
    def test_no_flows_keeps_legacy_formula(self, monkeypatch):
        # 입금 0건: 총평가/초기자본 그대로 (하위 호환)
        pm = _pm(monkeypatch, "basket_rebalance:test_legacy", cash_delta=30_000)
        out = pm.get_portfolio_summary()
        assert out["total_value"] == 330_000
        assert out["total_return"] == pytest.approx(10.0)
        assert out["deposits_total"] == 0
        assert out["principal"] == 300_000

    def test_deposit_is_not_return_first_measure(self, monkeypatch):
        # 첫 측정 전 입금 100k: 총평가 400k인데 수익률 0, 실현손익 0
        pm = _pm(monkeypatch, "basket_rebalance:test_dep1", deposits=100_000)
        out = pm.get_portfolio_summary()
        assert out["total_value"] == 400_000
        assert out["total_return"] == pytest.approx(0.0)
        assert out["realized_pnl"] == 0
        assert out["principal"] == 400_000

    def test_chain_links_prev_snapshot(self, monkeypatch):
        # 직전 스냅샷 cum -10% (V=270k) → 입금 100k → 현재 370k: 수익률 -10% 유지
        prev = {
            "date": datetime(2026, 7, 1), "created_at": datetime(2026, 7, 1, 10, 7),
            "total_value": 270_000.0, "cumulative_return": -10.0,
        }
        pm = _pm(
            monkeypatch, "basket_rebalance:test_dep2",
            cash_delta=-30_000, deposits=100_000,
            prev_snapshot=prev, flow_since=100_000, hist_max_cum=0.0,
        )
        out = pm.get_portfolio_summary()
        assert out["total_value"] == 370_000
        assert out["total_return"] == pytest.approx(-10.0)
        # MDD도 TWR 지수 기준: 입금으로 원화 가치가 최고치여도 낙폭 10% 유지
        assert out["mdd"] == pytest.approx(10.0)

    def test_gain_after_deposit(self, monkeypatch):
        # 직전 400k cum 0 → 유입 0 → 440k: +10%
        prev = {
            "date": datetime(2026, 7, 2), "created_at": datetime(2026, 7, 2, 10, 7),
            "total_value": 400_000.0, "cumulative_return": 0.0,
        }
        pm = _pm(
            monkeypatch, "basket_rebalance:test_dep3",
            cash_delta=40_000 + 100_000, deposits=100_000,  # 현금 440k 구성
            prev_snapshot=prev, flow_since=0.0, hist_max_cum=0.0,
        )
        # cash = 300k + 100k(입금) + 140k? — cash_delta는 거래 델타만이어야 하므로 40k로 재설정
        import core.portfolio_manager as pm_mod
        monkeypatch.setattr(
            pm_mod, "get_trade_cash_summary",
            lambda mode=None, account_key=None: {"cash_delta": 40_000},
        )
        out = pm.get_portfolio_summary()
        assert out["total_value"] == 440_000
        assert out["total_return"] == pytest.approx(10.0)
        assert out["mdd"] == pytest.approx(0.0)

    def test_realized_pnl_excludes_deposit(self, monkeypatch):
        pm = _pm(monkeypatch, "basket_rebalance:test_dep4", deposits=100_000)
        out = pm.get_portfolio_summary()
        assert out["realized_pnl"] == 0
        assert out["cash"] == 400_000


class TestUpsertRefreshesCreatedAt:
    def test_same_day_upsert_advances_measurement_time(self):
        # 재실행(upsert) 시 created_at 미갱신이면, 재실행 전 반영된 입금이 다음 날
        # TWR 구간에 이중 산입돼 수익률이 영구 왜곡된다(적대적 리뷰 HIGH).
        init_database()
        from database.repositories import get_latest_snapshot_summary, save_portfolio_snapshot

        acct = "basket_rebalance:test_upsert_ts"
        save_portfolio_snapshot(
            total_value=300_000, cash=300_000, invested=0, account_key=acct,
            snapshot_date=datetime(2026, 7, 6),
        )
        first = get_latest_snapshot_summary(account_key=acct)["created_at"]
        import time
        time.sleep(0.01)
        save_portfolio_snapshot(
            total_value=400_000, cash=400_000, invested=0, account_key=acct,
            snapshot_date=datetime(2026, 7, 6),
        )
        second = get_latest_snapshot_summary(account_key=acct)["created_at"]
        assert second > first  # 측정 시각이 재실행 시각으로 전진


class TestHasCashFlowsBranch:
    def test_net_zero_flows_still_twr_branch(self, monkeypatch):
        # +100k 뒤 -100k(순합 0)이어도 구간 수익률은 흐름 영향을 받았으므로 TWR 유지.
        prev = {
            "date": datetime(2026, 7, 1), "created_at": datetime(2026, 7, 1, 10, 7),
            "total_value": 400_000.0, "cumulative_return": 25.0,
        }
        pm = _pm(
            monkeypatch, "basket_rebalance:test_netzero",
            cash_delta=0.0, deposits=0.0,  # 순합 0
            prev_snapshot=prev, flow_since=-100_000, hist_max_cum=25.0,
        )
        import core.portfolio_manager as pm_mod
        monkeypatch.setattr(pm_mod, "has_cash_flows", lambda account_key="": True)
        out = pm.get_portfolio_summary()
        # V=300k, 유입 -100k → r = 300/(400-100)-1 = 0 → 누적 25% 유지 (legacy면 0%로 붕괴)
        assert out["total_return"] == pytest.approx(25.0)


class TestTimeWeightedCapital:
    def test_late_deposit_barely_counts(self):
        from datetime import date
        from core.basket_evaluation import time_weighted_capital
        # 60일 기간, 59일차 3M 입금 → 기여 1/60만
        out = time_weighted_capital(
            10_000_000,
            [(datetime(2026, 3, 1) + timedelta(days=59), 3_000_000)],
            date(2026, 3, 1), date(2026, 3, 1) + timedelta(days=60),
        )
        assert out == pytest.approx(10_000_000 + 3_000_000 * (1 / 60), rel=1e-6)

    def test_day_one_deposit_counts_fully(self):
        from datetime import date
        from core.basket_evaluation import time_weighted_capital
        out = time_weighted_capital(
            300_000, [(datetime(2026, 3, 1), 100_000)],
            date(2026, 3, 1), date(2026, 3, 31),
        )
        assert out == pytest.approx(400_000)

    def test_no_flows_is_initial(self):
        from datetime import date
        from core.basket_evaluation import time_weighted_capital
        assert time_weighted_capital(300_000, [], date(2026, 3, 1), date(2026, 3, 31)) == 300_000


class TestRestartToolMovesCashFlows:
    def test_apply_archives_cash_flows(self, monkeypatch):
        # 재시작 시 CashFlow도 아카이브 키로 이전 — 남기면 새 트랙 현금·TWR 왜곡.
        init_database()
        import tools.restart_basket_track_record as rt

        acct = "basket_rebalance:kr_diversified_hold"
        record_cash_flow(50_000, account_key=acct, occurred_at=datetime(2026, 7, 1))
        monkeypatch.setattr(
            "sys.argv",
            ["restart_basket_track_record.py", "--basket", "kr_diversified_hold",
             "--archive-suffix", "t-arch", "--apply"],
        )
        assert rt.main() == 0
        assert get_cash_flow_total(account_key=acct) == 0.0
        assert get_cash_flow_total(account_key=f"{acct}@t-arch") == pytest.approx(50_000)


class TestEvaluatorWithFlows:
    def test_nav_uses_snapshot_twr_when_flows_exist(self):
        # 입금이 있으면 평가의 NAV는 총평가/초기자본이 아니라 스냅샷 TWR 누적치.
        init_database()
        from database.models import PortfolioSnapshot, get_session
        from core.basket_evaluation import collect_basket_paper_evaluation

        acct = "basket_rebalance:kr_diversified_hold"
        # 주의: conftest가 DB를 임시 경로로 격리하므로 운영 DB와 무관.
        session = get_session()
        try:
            session.add(PortfolioSnapshot(
                account_key=acct, date=datetime(2026, 7, 3),
                total_value=400_000, cash=200_000, invested=200_000,
                cumulative_return=-3.21, peak_value=410_000,
            ))
            session.commit()
        finally:
            session.close()
        record_cash_flow(100_000, account_key=acct, occurred_at=datetime(2026, 7, 3, 12, 0))

        result, _ = collect_basket_paper_evaluation(
            basket_name="kr_diversified_hold", include_benchmark=False,
        )
        assert result["metrics"]["nav_return_pct"] == pytest.approx(-3.21)


class TestRecordDepositCli:
    def test_records_deposit(self, monkeypatch):
        init_database()
        import tools.record_deposit as rd

        monkeypatch.setattr(
            "sys.argv",
            ["record_deposit.py", "--account-key", "basket_rebalance:test_cli", "--amount", "100000"],
        )
        assert rd.main() == 0
        assert get_cash_flow_total(account_key="basket_rebalance:test_cli") == pytest.approx(100_000)

    def test_rejects_nonpositive(self, monkeypatch):
        import tools.record_deposit as rd

        monkeypatch.setattr(
            "sys.argv",
            ["record_deposit.py", "--account-key", "basket_rebalance:test_cli2", "--amount", "0"],
        )
        assert rd.main() == 1

    def test_rejects_unknown_basket(self, monkeypatch):
        import tools.record_deposit as rd

        monkeypatch.setattr(
            "sys.argv",
            ["record_deposit.py", "--basket", "no_such_basket", "--amount", "1000"],
        )
        assert rd.main() == 1

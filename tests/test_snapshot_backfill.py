"""결측 스냅샷 복원 + 거래일 달력 정합 테스트.

배경(2026-08-26): 승격 기준인 스냅샷 커버리지 95%가 두 트랙 다 미달로 나왔다.
파고 보니 원인이 둘이었고, 큰 쪽은 결측이 아니었다.

  1) 거래일 달력 오류 — config/holidays.yaml에 2026-07-17이 빠져 있어 휴장일이
     영업일로 집계됐다. 분모만 부풀어 kr_diversified_hold 96.3%가 94.5%로,
     kr_pocket 96.9%가 93.9%로 보였다. 전 구간을 시장 데이터와 대조하니 7건이
     더 나왔다(설 연휴가 2025년 날짜로 들어가 있었다).
  2) 진짜 결측 — 6/26, 8/18. 사이클이 안 돈 날이다. `_nav_attribution_date`가
     '오늘이 거래일이면 오늘'로 귀속해 다음날 실행이 어제를 못 메운다.

복원은 원장 재생으로 포지션·현금을 정확히 되살리고 NAV만 당일 OHLC로 근사한다.
근사분은 reconstructed=True로 표시해 평가가 실측과 나눠 표기한다 — 커버리지 게이트는
'시스템이 실제로 돌았는가'를 보는 장치라, 보정분이 실측인 척하면 목적을 잃는다.
"""

from datetime import date, datetime, timedelta

import pytest

from core.basket_evaluation import evaluate_basket_paper_operation
from core.snapshot_backfill import replay_ledger
from database.models import init_database
from database.models import TradeHistory, get_session
from database.repositories import record_cash_flow, save_portfolio_snapshot


def _trade(key, symbol, action, price, qty, when, commission=0, tax=0):
    """executed_at을 명시해 과거 거래를 심는다.

    save_trade는 executed_at을 컬럼 기본값(now)으로 채우므로 과거 시점 원장을
    만들 수 없다 — 재생 로직은 그 시각으로 자르기 때문에 직접 넣어야 한다.
    """
    session = get_session()
    try:
        session.add(TradeHistory(
            account_key=key, symbol=symbol, action=action, price=price,
            quantity=qty, total_amount=price * qty, commission=commission,
            tax=tax, slippage=0, strategy=key, mode="paper",
            executed_at=when, order_at=when,
        ))
        session.commit()
    finally:
        session.close()


# ------------------------------------------------------------- 원장 재생

class TestReplayLedger:
    """포지션·현금은 근사가 아니라 정확히 복원돼야 한다."""

    def test_buys_then_partial_sell(self):
        init_database()
        key = "basket_rebalance:replay_a"
        _trade(key, "005930", "BUY", 1000, 10, datetime(2026, 6, 10, 10, 0), commission=15)
        _trade(key, "005930", "SELL", 1200, 4, datetime(2026, 6, 12, 10, 0),
               commission=18, tax=24)

        state = replay_ledger(key, date(2026, 6, 30), 100_000)
        assert state["positions"] == {"005930": 6}
        # 현금 = 10만 - (1만 + 15) + (4,800 - 42)
        assert state["cash"] == pytest.approx(100_000 - 10_015 + 4_758)
        # 원가는 매도분만큼 비례 차감 (10주 1만원 → 6주 6천원)
        assert state["cost_basis"]["005930"] == pytest.approx(6_000)

    def test_asof_date_excludes_later_trades(self):
        init_database()
        key = "basket_rebalance:replay_b"
        _trade(key, "005930", "BUY", 1000, 5, datetime(2026, 6, 10, 10, 0))
        _trade(key, "000660", "BUY", 2000, 3, datetime(2026, 7, 20, 10, 0))

        early = replay_ledger(key, date(2026, 6, 30), 100_000)
        assert early["positions"] == {"005930": 5}, "이후 거래가 과거 상태에 섞였다"

        late = replay_ledger(key, date(2026, 7, 31), 100_000)
        assert late["positions"] == {"005930": 5, "000660": 3}

    def test_deposits_are_included_in_cash(self):
        init_database()
        key = "basket_rebalance:replay_c"
        record_cash_flow(amount=50_000, account_key=key, mode="paper", note="적립")
        state = replay_ledger(key, date.today(), 100_000)
        assert state["cash"] == pytest.approx(150_000)

    def test_fully_closed_position_disappears(self):
        init_database()
        key = "basket_rebalance:replay_d"
        _trade(key, "005930", "BUY", 1000, 5, datetime(2026, 6, 10, 10, 0))
        _trade(key, "005930", "SELL", 1100, 5, datetime(2026, 6, 11, 10, 0))
        assert replay_ledger(key, date(2026, 6, 30), 100_000)["positions"] == {}


# ------------------------------------------- 복원 표시가 평가에 드러나는가

def _snap(key, d, value, reconstructed=False):
    save_portfolio_snapshot(
        total_value=value, cash=value, invested=0,
        cumulative_return=0.0, mdd=0.0, position_count=0,
        account_key=key, mode="paper",
        snapshot_date=datetime(d.year, d.month, d.day),
        reconstructed=reconstructed,
    )


class TestReconstructedDisclosure:
    """보정분이 실측과 섞여 보이면 게이트가 목적을 잃는다."""

    def test_measured_and_reconstructed_are_split(self):
        r = evaluate_basket_paper_operation(
            operation_start=date(2026, 6, 1), today=date(2026, 6, 30),
            trading_days_total=20, snapshot_days=20, reconstructed_days=3,
            pending_failed_orders=0, total_costs=0, initial_capital=1_000_000,
        )
        assert r["snapshot_coverage"] == pytest.approx(1.0)
        assert r["reconstructed_days"] == 3
        assert r["measured_days"] == 17
        assert r["measured_coverage"] == pytest.approx(0.85)

    def test_no_reconstruction_keeps_measured_equal_to_total(self):
        r = evaluate_basket_paper_operation(
            operation_start=date(2026, 6, 1), today=date(2026, 6, 30),
            trading_days_total=20, snapshot_days=19,
            pending_failed_orders=0, total_costs=0, initial_capital=1_000_000,
        )
        assert r["reconstructed_days"] == 0
        assert r["measured_days"] == 19
        assert r["measured_coverage"] == pytest.approx(r["snapshot_coverage"])

    def test_reconstructed_cannot_exceed_snapshot_days(self):
        r = evaluate_basket_paper_operation(
            operation_start=date(2026, 6, 1), today=date(2026, 6, 30),
            trading_days_total=10, snapshot_days=5, reconstructed_days=99,
            pending_failed_orders=0, total_costs=0, initial_capital=1_000_000,
        )
        assert r["reconstructed_days"] == 5
        assert r["measured_days"] == 0

    def test_report_text_shows_the_split(self):
        from core.basket_evaluation import format_evaluation_report

        r = evaluate_basket_paper_operation(
            operation_start=date(2026, 6, 1), today=date(2026, 6, 30),
            trading_days_total=20, snapshot_days=20, reconstructed_days=2,
            pending_failed_orders=0, total_costs=0, initial_capital=1_000_000,
        )
        text = format_evaluation_report(r, "t")
        assert "사후 복원" in text and "실측" in text

    def test_measured_row_wins_over_reconstructed_on_upsert(self):
        """나중에 실측이 들어오면 보정 표시를 걷어낸다."""
        from database.models import PortfolioSnapshot, get_session

        init_database()
        key = "basket_rebalance:upsert_mark"
        d = date(2026, 6, 15)
        _snap(key, d, 1_000, reconstructed=True)
        _snap(key, d, 1_100, reconstructed=False)

        session = get_session()
        try:
            row = session.query(PortfolioSnapshot).filter(
                PortfolioSnapshot.account_key == key,
                PortfolioSnapshot.mode == "paper",
            ).first()
            assert row.total_value == pytest.approx(1_100)
            assert not row.reconstructed, "실측이 보정 표시를 걷어내지 못했다"
        finally:
            session.close()


# ----------------------------------------------------- 거래일 달력 정합

class TestTradingCalendar:
    """달력 오류는 양방향으로 조용히 해롭다 — 분모를 부풀리거나 진짜 결측을 가린다."""

    def test_known_market_closures_are_registered(self):
        """시장 데이터로 확인된 휴장일이 달력에 있어야 한다.

        빠지면 그 날이 영업일로 집계돼 커버리지 분모가 부풀고, 없는 결측을 쫓게 된다.
        2026-07-17 하나가 두 트랙을 95% 아래로 끌어내렸다.
        """
        from config.config_loader import Config
        from core.trading_hours import TradingHours

        th = TradingHours(Config.get())
        # KOSPI 지수·개별 종목 모두 봉이 없어 휴장이 확인된 날들
        for closed in (
            date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 설 연휴
            date(2026, 5, 1),                                          # 근로자의 날
            date(2026, 6, 3),                                          # 지방선거
            date(2026, 7, 17),
        ):
            assert not th.is_trading_day(
                datetime(closed.year, closed.month, closed.day)
            ), f"{closed}는 휴장일인데 거래일로 판정된다"

    def test_open_days_are_not_marked_holiday(self):
        """실제 개장한 날이 휴장으로 등록돼 있으면 진짜 결측이 집계에서 사라진다."""
        from config.config_loader import Config
        from core.trading_hours import TradingHours

        th = TradingHours(Config.get())
        for open_day in (date(2026, 1, 27), date(2026, 1, 28), date(2026, 1, 29)):
            assert th.is_trading_day(
                datetime(open_day.year, open_day.month, open_day.day)
            ), f"{open_day}는 개장일인데 휴장으로 등록돼 있다"

    def test_verifier_reports_mismatch(self, monkeypatch):
        """검증 도구가 불일치를 실제로 잡아내는가(달력이 맞아도 로직은 살아 있어야)."""
        import tools.verify_trading_calendar as v

        # 6/8·6/10만 열렸다고 가정하면 그 사이 평일 6/9가 wrong_open으로 잡혀야 한다.
        # (6/3은 실제 휴장일로 등록돼 있어 대조 대상에서 제외 — 순수 로직만 본다)
        monkeypatch.setattr(
            v, "market_open_days",
            lambda s, e, symbol=None: {date(2026, 6, 8), date(2026, 6, 10)},
        )
        r = v.compare(date(2026, 6, 8), date(2026, 6, 12))
        assert date(2026, 6, 9) in r["wrong_open"]
        assert date(2026, 6, 8) not in r["wrong_open"]
        assert date(2026, 6, 10) not in r["wrong_open"]

    def test_verifier_ignores_days_beyond_last_known_bar(self, monkeypatch):
        """데이터가 아직 안 올라온 최근 날은 '휴장'이 아니라 '모름'이다.

        장중에 돌리면 오늘 봉이 없어서 오늘이 휴장으로 오판됐다(실제 오탐).
        """
        import tools.verify_trading_calendar as v

        monkeypatch.setattr(
            v, "market_open_days",
            lambda s, e, symbol=None: {date(2026, 6, 1), date(2026, 6, 2)},
        )
        r = v.compare(date(2026, 6, 1), date(2026, 6, 30))
        assert r["verified_through"] == date(2026, 6, 2)
        assert r["wrong_open"] == [], "데이터 없는 미래 날짜를 휴장 오류로 보고했다"

    def test_verifier_does_not_report_when_data_unavailable(self, monkeypatch):
        """데이터 소스 장애를 달력 오류로 둔갑시키지 않는다."""
        import tools.verify_trading_calendar as v

        monkeypatch.setattr(v, "market_open_days", lambda s, e, symbol=None: None)
        r = v.compare(date(2026, 6, 1), date(2026, 6, 5))
        assert r.get("error")
        assert r["wrong_open"] == [] and r["wrong_closed"] == []


# --------------------------------------------------------- 보충 소급 한도

def test_backfill_lookback_is_bounded():
    """무제한 소급이면 장기 중단도 메워져 커버리지 게이트가 목적을 잃는다."""
    import main

    assert 0 < main.BACKFILL_LOOKBACK_DAYS <= 30

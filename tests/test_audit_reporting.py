"""리포트 지표 회귀 테스트 (2026-09-23 점검).

입금을 달력 날짜로 묶으면, 그날 10:07 스냅샷 뒤에 들어온 입금이나 주말 입금이
엉뚱한 구간에 들어가 그 구간이 +25~36% 수익으로 잡힌다(#461과 같은 증상이 적립
주기마다 재발). 입금이 처음 반영된 스냅샷 구간으로 묶어야 한다.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest


def _snap(d, created, value):
    return SimpleNamespace(date=datetime.combine(d, datetime.min.time()),
                           created_at=created, total_value=value)


def _flows(monkeypatch, rows):
    monkeypatch.setattr("database.repositories.get_cash_flows",
                        lambda account_key, mode="paper": rows)


def test_weekend_deposit_is_absorbed_by_next_snapshot(monkeypatch):
    import main
    from core.performance_lens import daily_returns_from_nav

    snaps = [
        _snap(date(2026, 9, 25), datetime(2026, 9, 25, 10, 7), 300_000),
        _snap(date(2026, 9, 28), datetime(2026, 9, 28, 10, 7), 400_800),
    ]
    _flows(monkeypatch, [(datetime(2026, 9, 26, 11, 0), 100_000.0)])   # 토요일 입금

    flows = main.account_flows_by_snapshot("acct", snaps)
    assert flows == {date(2026, 9, 28): 100_000.0}
    rets = daily_returns_from_nav([(s.date, s.total_value) for s in snaps], flows=flows)
    assert rets[-1][1] == pytest.approx(0.2, abs=1e-6)   # 0.8천 원 수익만 남는다(+0.2%)


def test_deposit_after_same_day_snapshot_goes_to_next_interval(monkeypatch):
    """8/26 실제 사례: 10:07 스냅샷 뒤 17:19 입금 — 날짜로 묶으면 8/26이 -26%, 8/27이 +36%."""
    import main
    from core.performance_lens import daily_returns_from_nav

    snaps = [
        _snap(date(2026, 8, 25), datetime(2026, 8, 25, 10, 7), 280_718),
        _snap(date(2026, 8, 26), datetime(2026, 8, 26, 10, 7), 284_499),
        _snap(date(2026, 8, 27), datetime(2026, 8, 27, 10, 7), 385_460),
    ]
    _flows(monkeypatch, [(datetime(2026, 8, 26, 17, 19), 100_000.0)])

    flows = main.account_flows_by_snapshot("acct", snaps)
    assert flows == {date(2026, 8, 27): 100_000.0}
    rets = [r for _, r in daily_returns_from_nav([(s.date, s.total_value) for s in snaps], flows=flows)]
    assert all(abs(r) < 2.0 for r in rets), rets   # 가짜 ±30%대 하루가 없어야 한다


def test_flow_after_last_snapshot_is_not_yet_counted(monkeypatch):
    import main

    snaps = [_snap(date(2026, 9, 22), datetime(2026, 9, 22, 10, 7), 300_000)]
    _flows(monkeypatch, [(datetime(2026, 9, 22, 18, 0), 100_000.0)])
    assert main.account_flows_by_snapshot("acct", snaps) == {}


def test_weekly_summary_does_not_call_reconstructed_week_accident_free():
    from core.weekly_report import build_weekly_summary

    base = dict(basket_name="t", eval_result={"verdict": "WAIT", "metrics": {}},
                missing_days=0, cycle_errors=0)
    clean = build_weekly_summary(**base)
    restored = build_weekly_summary(**base, reconstructed_days=1)
    ev = {f["name"]: f["value"] for f in restored["fields"]}["🛠 주간 이벤트"]
    assert "무사고" in {f["name"]: f["value"] for f in clean["fields"]}["🛠 주간 이벤트"]
    assert "무사고" not in ev and "나중에 채운 기록 1일" in ev


def test_regime_note_is_shown():
    from core.weekly_report import build_weekly_summary

    regime = {"up": {"days": 3, "capture": 0.5, "bench_pct": 2.0, "mine_pct": 1.0},
              "down": {"days": 2, "capture": 0.4, "bench_pct": -2.0, "mine_pct": -0.8}}
    out = build_weekly_summary(basket_name="t", eval_result={"verdict": "WAIT", "metrics": {}},
                               regime=regime, regime_note="근사 — 지수 시가 기준")
    val = {f["name"]: f["value"] for f in out["fields"]}["🌗 국면 분해"]
    assert "근사" in val


def test_sharpe_label_states_risk_free_rate():
    from core.performance_lens import format_risk_line

    line = format_risk_line({"samples": 30, "vol_annual_pct": 10.0, "sharpe_annual": 0.5,
                             "down_day_ratio": 0.4, "worst_day_pct": -2.0})
    assert "샤프(금리 0% 기준)" in line


def test_daily_card_renders_risk_field(monkeypatch):
    from core.notifier import Notifier

    sent = {}
    n = Notifier.__new__(Notifier)
    monkeypatch.setattr(n, "send_embed", lambda title, desc, **kw: sent.update(kw), raising=False)
    n.send_daily_report({"total_value": 1, "risk": "연변동성 10.0% · 샤프(금리 0% 기준) +0.50"})
    names = [f["name"] for f in sent["fields"]]
    assert "📉 리스크" in names


def test_deploy_cost_estimate_exempts_etf_sell_tax():
    from core.basket_deploy import estimate_order_costs

    orders = [SimpleNamespace(action="SELL", symbol="069500", quantity=1),
              SimpleNamespace(action="SELL", symbol="005930", quantity=1)]
    prices = {"069500": 100_000, "005930": 100_000}
    with_exempt = estimate_order_costs(orders, prices, tax_exempt_symbols=["069500"])
    without = estimate_order_costs(orders, prices)
    assert with_exempt["est_tax"] == pytest.approx(200)   # 005930만 0.20%
    assert without["est_tax"] == pytest.approx(400)

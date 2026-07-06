"""tools/capital_slot_simulator.py — 자본별 슬롯 채움 계산(순수 함수) 단위 테스트."""

from tools.capital_slot_simulator import format_simulation, simulate_slot_fills


HOLDINGS = {"A": 0.5, "B": 0.5}


class TestSimulateSlotFills:
    def test_basic_fill(self):
        # 자본 10M, 주식 80% → 슬롯 4M씩. A 1주 1M → 4주(100%), B 1주 3M → 1주(75%)
        out = simulate_slot_fills(HOLDINGS, 10_000_000, 0.8, {"A": 1_000_000, "B": 3_000_000})
        by = {s["symbol"]: s for s in out["slots"]}
        assert by["A"]["shares"] == 4 and by["A"]["fill_ratio"] == 1.0
        assert by["B"]["shares"] == 1 and round(by["B"]["fill_ratio"], 2) == 0.75
        assert out["unfillable_count"] == 0
        # 배치율 = (4M + 3M) / 10M = 70%
        assert round(out["deployment_ratio"], 2) == 0.70

    def test_unfillable_when_share_exceeds_slot(self):
        # B 1주 5M > 슬롯 4M → 미체결
        out = simulate_slot_fills(HOLDINGS, 10_000_000, 0.8, {"A": 1_000_000, "B": 5_000_000})
        by = {s["symbol"]: s for s in out["slots"]}
        assert by["B"]["unfillable"] is True
        assert "1주" in by["B"]["reason"]
        assert out["unfillable_count"] == 1

    def test_unfillable_when_slot_below_min_trade(self):
        # 슬롯 4M < 최소거래 5M → 미체결 (가격은 저렴해도)
        out = simulate_slot_fills(
            HOLDINGS, 10_000_000, 0.8, {"A": 1_000, "B": 1_000}, min_trade_amount=5_000_000,
        )
        assert out["unfillable_count"] == 2
        assert all("최소거래" in s["reason"] for s in out["slots"])

    def test_missing_price_is_unknown_not_unfillable(self):
        out = simulate_slot_fills(HOLDINGS, 10_000_000, 0.8, {"A": 1_000_000})  # B 가격 없음
        by = {s["symbol"]: s for s in out["slots"]}
        assert by["B"]["unfillable"] is False
        assert by["B"]["reason"] == "가격 조회 불가"
        assert out["unknown_count"] == 1

    def test_weights_renormalized(self):
        # 비중 합 2.0 → 정규화 후 각 50%
        out = simulate_slot_fills({"A": 1.0, "B": 1.0}, 10_000_000, 1.0, {"A": 1, "B": 1})
        assert all(round(s["weight"], 2) == 0.5 for s in out["slots"])

    def test_month1_hynix_case(self):
        # 실측 재현: 자본 10M·주식 80%·10종목 → 하이닉스 슬롯 80만 < 1주 204.8만 → 미체결
        holdings = {f"S{i}": 0.1 for i in range(9)}
        holdings["000660"] = 0.1
        prices = {f"S{i}": 100_000 for i in range(9)}
        prices["000660"] = 2_048_000
        out = simulate_slot_fills(holdings, 10_000_000, 0.8, prices)
        hynix = next(s for s in out["slots"] if s["symbol"] == "000660")
        assert hynix["unfillable"] is True
        # 34M이면 슬롯 272만 ≥ 1주 → 채움
        out34 = simulate_slot_fills(holdings, 34_000_000, 0.8, prices)
        hynix34 = next(s for s in out34["slots"] if s["symbol"] == "000660")
        assert hynix34["shares"] == 1 and hynix34["unfillable"] is False


class TestFormatSimulation:
    def test_table_contains_key_lines(self):
        out = simulate_slot_fills(HOLDINGS, 10_000_000, 0.8, {"A": 1_000_000, "B": 5_000_000})
        text = format_simulation("kr_x", 10_000_000, out)
        assert "kr_x" in text and "10,000,000" in text
        assert "미체결 1개" in text
        assert "총 배치율" in text

    def test_all_fillable_message(self):
        out = simulate_slot_fills(HOLDINGS, 10_000_000, 0.8, {"A": 1_000_000, "B": 1_000_000})
        text = format_simulation("kr_x", 10_000_000, out)
        assert "전 슬롯 매수 가능" in text

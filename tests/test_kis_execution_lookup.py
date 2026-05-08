from api.kis_api import KISApi


def test_execution_from_ccld_row_extracts_price_and_quantity():
    row = {
        "ODNO": "000123",
        "avg_prvs": "60100",
        "tot_ccld_qty": "3",
        "rmn_qty": "0",
    }

    execution = KISApi._execution_from_ccld_row(row)

    assert execution == {
        "fill_price": 60100.0,
        "filled_qty": 3.0,
        "remaining_qty": 0.0,
        "order_no": "000123",
    }


def test_get_order_execution_after_order_matches_order_number():
    api = object.__new__(KISApi)

    def fake_rows(symbol, order_no, ccld_dvsn="01"):
        return [
            {
                "ODNO": "000122",
                "avg_prvs": "59000",
                "tot_ccld_qty": "9",
            },
            {
                "ODNO": "000123",
                "avg_prvs": "60100",
                "tot_ccld_qty": "3",
            },
        ]

    api._inquire_daily_ccld_rows = fake_rows

    execution = api.get_order_execution_after_order(
        "005930",
        {"odno": "000123"},
        max_attempts=1,
        delay_seconds=0,
    )

    assert execution["fill_price"] == 60100.0
    assert execution["filled_qty"] == 3.0
    assert execution["order_no"] == "000123"

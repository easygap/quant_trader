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


def test_get_order_execution_after_order_rejects_single_fallback_row_with_mismatched_order_number():
    api = object.__new__(KISApi)
    calls = []

    def fake_rows(symbol, order_no, ccld_dvsn="01"):
        calls.append(order_no)
        if order_no == "000123":
            return []
        return [{
            "ODNO": "000122",
            "avg_prvs": "59000",
            "tot_ccld_qty": "3",
        }]

    api._inquire_daily_ccld_rows = fake_rows

    execution = api.get_order_execution_after_order(
        "005930",
        {"odno": "000123"},
        max_attempts=1,
        delay_seconds=0,
    )

    assert execution is None
    assert calls == ["000123", ""]


def test_get_order_execution_after_order_rejects_single_primary_row_with_mismatched_order_number():
    api = object.__new__(KISApi)

    def fake_rows(symbol, order_no, ccld_dvsn="01"):
        if order_no == "000123":
            return [{
                "ODNO": "000122",
                "avg_prvs": "59000",
                "tot_ccld_qty": "3",
            }]
        return []

    api._inquire_daily_ccld_rows = fake_rows

    execution = api.get_order_execution_after_order(
        "005930",
        {"odno": "000123"},
        max_attempts=1,
        delay_seconds=0,
    )

    assert execution is None


def test_get_order_execution_after_order_accepts_single_row_with_unpadded_order_number():
    api = object.__new__(KISApi)

    def fake_rows(symbol, order_no, ccld_dvsn="01"):
        return [{
            "ODNO": "123",
            "avg_prvs": "60100",
            "tot_ccld_qty": "3",
        }]

    api._inquire_daily_ccld_rows = fake_rows

    execution = api.get_order_execution_after_order(
        "005930",
        {"odno": "000123"},
        max_attempts=1,
        delay_seconds=0,
    )

    assert execution["fill_price"] == 60100.0
    assert execution["filled_qty"] == 3.0
    assert execution["order_no"] == "123"


def test_unfilled_order_status_detects_uppercase_symbol_and_remaining_qty():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._is_configured = lambda: True
    api._request = lambda *a, **kw: {
        "rt_cd": "0",
        "output1": [
            {
                "PDNO": "005930",
                "NCCS_QTY": "2",
                "ORD_UNPR": "70000",
                "SLL_BUY_DVSN_CD": "02",
                "ODNO": "000001",
            }
        ],
    }

    status = api.get_unfilled_order_status("005930")

    assert status["checked"] is True
    assert status["has_unfilled"] is True
    assert status["orders"][0]["remaining_qty"] == 2
    assert api.has_unfilled_orders("005930") is True


def test_unfilled_order_status_exposes_query_failure():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._is_configured = lambda: True
    api._request = lambda *a, **kw: {"rt_cd": "1", "msg1": "temporary failure"}

    status = api.get_unfilled_order_status("005930")

    assert status["checked"] is False
    assert status["has_unfilled"] is False
    assert status["reason"] == "kis_unfilled_query_failed"
    assert api.has_unfilled_orders("005930") is False


def test_unfilled_order_status_prefers_zero_remaining_over_original_order_qty():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._is_configured = lambda: True
    api._request = lambda *a, **kw: {
        "rt_cd": "0",
        "output1": [
            {
                "PDNO": "005930",
                "RMN_QTY": "0",
                "ORD_QTY": "5",
            }
        ],
    }

    status = api.get_unfilled_order_status("005930")

    assert status["checked"] is True
    assert status["has_unfilled"] is False
    assert status["orders"] == []


def test_open_orders_status_exposes_failure_instead_of_silent_empty_list():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._is_configured = lambda: True
    api._ensure_token = lambda: None
    api._request = lambda *a, **kw: {"rt_cd": "1", "msg1": "temporary failure"}

    status = api.get_open_orders_status()

    assert status["checked"] is False
    assert status["reason"] == "kis_open_orders_query_failed"
    assert api.get_open_orders() == []


def test_get_balance_rejects_kis_error_body_instead_of_empty_balance():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._request = lambda *a, **kw: {"rt_cd": "1", "msg1": "temporary failure"}

    assert api.get_balance() is None


def test_get_balance_parses_successful_empty_account():
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._request = lambda *a, **kw: {
        "rt_cd": "0",
        "output1": [],
        "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}],
    }

    balance = api.get_balance()

    assert balance is not None
    assert balance["cash"] == 1_000_000
    assert balance["positions"] == []

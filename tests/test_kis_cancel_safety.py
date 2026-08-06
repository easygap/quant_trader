"""KIS 국내주식 취소 조회·제출 경계의 fail-closed 회귀 테스트."""

from unittest.mock import patch

import pytest

from api.kis_api import KISApi, authorized_kis_order_submission


def _bare_api(*, use_mock=False):
    api = object.__new__(KISApi)
    api.use_mock = use_mock
    api.base_url = (
        "https://openapivts.koreainvestment.com:29443"
        if use_mock
        else "https://openapi.koreainvestment.com:9443"
    )
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._is_configured = lambda: True
    return api


def _cancelable_row(**overrides):
    row = {
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "odno": "0000001234",
        "ord_gno_brno": "06010",
        "psbl_qty": "3",
        "tot_ccld_qty": "0",
        "ord_qty": "3",
        "ord_unpr": "70000",
        "ord_dvsn_cd": "00",
        "excg_id_dvsn_cd": "KRX",
        "ord_tmd": "101500",
    }
    row.update(overrides)
    return row


def test_cancelable_buy_query_preserves_required_cancel_fields():
    api = _bare_api()
    captured = {}

    def fake_request(method, path, tr_id, params=None, **kwargs):
        captured.update({
            "method": method,
            "path": path,
            "tr_id": tr_id,
            "params": params,
        })
        return {"rt_cd": "0", "output": [_cancelable_row()]}

    api._request = fake_request
    result = api.get_cancelable_order_status("005930", "BUY")

    assert result["checked"] is True
    assert result["has_cancelable"] is True
    assert result["orders"] == [
        {
            "symbol": "005930",
            "side": "BUY",
            "order_no": "0000001234",
            "order_branch": "06010",
            "cancelable_qty": 3,
            "filled_qty": 0,
            "ordered_qty": 3,
            "order_price": "70000",
            "order_type": "00",
            "exchange_id": "KRX",
            "order_time": "101500",
        }
    ]
    assert captured == {
        "method": "GET",
        "path": "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        "tr_id": "TTTC0084R",
        "params": {
            "CANO": "12345678",
            "ACNT_PRDT_CD": "01",
            "INQR_DVSN_1": "1",
            "INQR_DVSN_2": "2",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    }


@pytest.mark.parametrize(
    "bad_row, expected_reason",
    [
        (_cancelable_row(psbl_qty="NaN"), "psbl_qty_invalid"),
        (_cancelable_row(tot_ccld_qty=""), "tot_ccld_qty_invalid"),
        (_cancelable_row(ord_gno_brno=""), "cancel_fields_missing"),
        (_cancelable_row(excg_id_dvsn_cd=""), "cancel_fields_missing"),
    ],
)
def test_cancelable_query_fails_closed_on_malformed_order(bad_row, expected_reason):
    api = _bare_api()
    api._request = lambda *args, **kwargs: {"rt_cd": "0", "output": [bad_row]}

    result = api.get_cancelable_order_status("005930", "BUY")

    assert result["checked"] is False
    assert result["reason"] == "kis_cancelable_order_malformed"
    assert result["malformed"][0]["reason"] == expected_reason


def test_cancelable_query_fails_closed_at_unpageable_fifty_row_boundary():
    api = _bare_api()
    api._request = lambda *args, **kwargs: {
        "rt_cd": "0",
        "output": [_cancelable_row(odno=f"{index:010d}") for index in range(50)],
    }

    result = api.get_cancelable_order_status("005930", "BUY")

    assert result["checked"] is False
    assert result["reason"] == "kis_cancelable_pagination_required"
    assert result["returned_count"] == 50


def test_real_cancel_requires_executor_capability_and_uses_official_payload():
    api = _bare_api(use_mock=False)
    captured = {}

    def fake_request(method, path, tr_id, body=None, **kwargs):
        captured.update({
            "method": method,
            "path": path,
            "tr_id": tr_id,
            "body": body,
            "idempotent": kwargs.get("idempotent"),
        })
        return {"rt_cd": "0", "output": {"odno": "CANCEL-1"}}

    api._request = fake_request

    with pytest.raises(PermissionError):
        api.cancel_order("0000001234", "06010", 3, "00", "KRX")

    with authorized_kis_order_submission():
        result = api.cancel_order("0000001234", "06010", 3, "00", "krx")

    assert result == {"odno": "CANCEL-1"}
    assert captured == {
        "method": "POST",
        "path": "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        "tr_id": "TTTC0013U",
        "body": {
            "CANO": "12345678",
            "ACNT_PRDT_CD": "01",
            "KRX_FWDG_ORD_ORGNO": "06010",
            "ORGN_ODNO": "0000001234",
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "3",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "N",
            "EXCG_ID_DVSN_CD": "KRX",
        },
        "idempotent": False,
    }


@pytest.mark.parametrize("quantity", [0, -1, 1.5, float("nan"), float("inf")])
def test_cancel_rejects_invalid_quantity_before_request(quantity):
    api = _bare_api(use_mock=True)
    api._request = lambda *args, **kwargs: pytest.fail("invalid cancel reached API")

    with pytest.raises(ValueError):
        api.cancel_order("0000001234", "06010", quantity, "00", "KRX")


def test_cancel_response_loss_is_not_retried_by_low_level_request():
    """취소도 주문과 동일한 비멱등 경계로 제출되는지 직접 확인한다."""
    api = _bare_api(use_mock=True)
    api._requires_order_capability = lambda: False
    api._request = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("sentinel")
    )

    with patch.object(api, "_request", side_effect=RuntimeError("sentinel")) as request:
        with pytest.raises(RuntimeError, match="sentinel"):
            api.cancel_order("0000001234", "06010", 3, "00", "KRX")

    assert request.call_args.kwargs["idempotent"] is False

"""웹소켓 다건 프레임 파싱·문서 정합성 회귀 테스트 (감사: runtime-websocket-handler-unwired).

1) KIS는 체결이 몰리면 한 프레임에 여러 건(data_count)을 이어 붙여 보내는데, 파서는
   첫 레코드만 읽고 나머지를 버렸다.
2) PROJECT_GUIDE는 웹소켓 갭 처리를 완료(✅)로 표기했지만 어떤 런타임도 핸들러를
   시작하지 않는다. 문서와 실제 연결 상태가 다시 어긋나지 않게 함께 고정한다.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from api.websocket_handler import WebSocketHandler

ROOT = Path(__file__).resolve().parents[1]
H0STCNT0_FIELDS = 46  # KIS 국내주식 실시간 체결가 레코드 필드 수


def _record(symbol, price, volume, width=H0STCNT0_FIELDS):
    fields = ["0"] * width
    fields[0] = symbol
    fields[1] = "093001"
    fields[2] = str(price)
    fields[4] = "100"
    fields[5] = "0.15"
    fields[12] = str(volume)
    fields[13] = str(volume * 10)
    return "^".join(fields)


def _handler():
    handler = WebSocketHandler(
        config=SimpleNamespace(kis_api={"app_key": "", "app_secret": "", "use_mock": True})
    )
    received = []
    handler.on_price_update(received.append)
    return handler, received


def _feed(handler, frame):
    asyncio.run(handler._handle_message(frame))


def test_multi_record_frame_emits_every_record():
    handler, received = _handler()
    frame = "0|H0STCNT0|002|" + _record("005930", 70000, 10) + "^" + _record("000660", 150000, 3)

    _feed(handler, frame)

    assert [(d["symbol"], d["price"], d["volume"]) for d in received] == [
        ("005930", 70000.0, 10),
        ("000660", 150000.0, 3),
    ]
    assert set(handler._price_cache) == {"005930", "000660"}


def test_single_record_frame_is_unchanged():
    handler, received = _handler()
    _feed(handler, "0|H0STCNT0|001|" + _record("005930", 70000, 10))
    assert [(d["symbol"], d["price"]) for d in received] == [("005930", 70000.0)]


def test_trailing_separator_does_not_break_split():
    handler, received = _handler()
    frame = "0|H0STCNT0|002|" + _record("005930", 70000, 10) + "^" + _record("035420", 200000, 5) + "^"
    _feed(handler, frame)
    assert [d["symbol"] for d in received] == ["005930", "035420"]


def test_uneven_frame_falls_back_to_first_record_with_warning():
    handler, received = _handler()
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        frame = "0|H0STCNT0|002|" + _record("005930", 70000, 10) + "^extra"
        _feed(handler, frame)
    finally:
        logger.remove(sink)

    assert [d["symbol"] for d in received] == ["005930"]
    assert any("나누어떨어지지 않음" in m for m in messages)


def _runtime_sources():
    yield ROOT / "main.py"
    for folder in ("core", "monitoring", "tools"):
        yield from (ROOT / folder).rglob("*.py")


def test_project_guide_matches_websocket_wiring_state():
    """런타임이 핸들러를 시작하지 않는 한 문서는 '미연결'로 표기해야 한다(반대도 마찬가지)."""
    wired = any(
        "WebSocketHandler(" in path.read_text(encoding="utf-8", errors="ignore")
        for path in _runtime_sources()
    )
    guide = (ROOT / "docs" / "PROJECT_GUIDE.md").read_text(encoding="utf-8")

    if wired:
        pytest.fail(
            "WebSocketHandler가 런타임에 연결됐다 — docs/PROJECT_GUIDE.md의 '런타임 미연결' "
            "표기를 실제 연결 상태로 갱신하고 이 테스트를 고치세요."
        )
    assert "✅ **WebSocket 갭 상태/보충 처리**" not in guide
    assert "런타임 미연결" in guide

"""
실시간 웹소켓 데이터 핸들러
- KIS API 웹소켓을 통한 실시간 체결/호가 스트리밍
- asyncio 기반 비동기 처리
- connect() 시 KISApi.get_approval_key()로 웹소켓 전용 승인키 발급 후 구독 메시지에 사용.
  KIS 공식 문서(웹소켓 인증) 변경 시 해당 로직 재검증 필요.
- 재연결 시: 갭 시각 로그 → 3분↑ REST(현재가·일봉)로 캐시 갱신 → 5분↑ BlackSwanDetector 즉시 점검
  → 1분↑ 분봉 보충·급변(3%+) 보고 → 1분↑ Notifier(디스코드, critical=False) 갭 경고.
"""

import json
import asyncio
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any, List
import time

from loguru import logger
import websockets
import websockets.exceptions

from collections import deque

from config.config_loader import Config

# gap event ring buffer 최대 크기
_GAP_HISTORY_MAX = 50


class WebSocketHandler:
    """
    실시간 데이터 스트리밍 핸들러

    KIS 웹소켓 API를 통해 실시간 체결가, 호가를 수신합니다.
    수신된 데이터는 등록된 콜백 함수로 전달됩니다.

    사용법:
        handler = WebSocketHandler()
        handler.on_price_update(callback_fn)
        await handler.connect(["005930", "000660"])
    """

    # KIS 웹소켓 URL
    REAL_WS_URL = "ws://ops.koreainvestment.com:21000"
    MOCK_WS_URL = "ws://ops.koreainvestment.com:31000"

    # 재연결 갭 처리 임계값
    _GAP_MINUTE_BACKFILL = timedelta(seconds=60)   # 분봉 보충·스윙 검사
    _GAP_REST_REFRESH = timedelta(minutes=2)       # REST 현재가·일봉 → 캐시 (3분→2분 강화)
    _GAP_BLACKSWAN_RECHECK = timedelta(minutes=2)  # 급락 로직 즉시 1회 (5분→2분 강화: 감사 H-1 대응)
    _GAP_NOTIFY_MIN = timedelta(seconds=60)        # 디스코드 갭 경고(너무 짧은 끊김은 생략)

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        kis = self.config.kis_api

        self.app_key = kis.get("app_key", "")
        self.app_secret = kis.get("app_secret", "")
        self.use_mock = kis.get("use_mock", True)

        self.ws_url = self.MOCK_WS_URL if self.use_mock else self.REAL_WS_URL
        self.approval_key = ""

        # 콜백 함수 저장소
        self._on_price_callbacks: list[Callable] = []
        self._on_orderbook_callbacks: list[Callable] = []

        # 연결 상태 관리
        self._is_connected = False
        self._should_reconnect = True
        self._ws = None

        # 상태 추적용 변수 (Heartbeat)
        self._last_ping_time: float = 0.0
        self._last_pong_time: float = 0.0
        self._first_data_logged: bool = False

        # 재연결 갭 보충용 (연결 끊김 시각 → 재연결 성공 시 REST 보충)
        self._disconnect_time: Optional[datetime] = None
        # 웹소켓·REST 갱신 가격 스냅샷 (symbol → dict)
        self._price_cache: Dict[str, Dict[str, Any]] = {}

        # gap observability: 최근 gap event 히스토리 (ring buffer)
        self._gap_history: deque[Dict[str, Any]] = deque(maxlen=_GAP_HISTORY_MAX)
        # 현재 진행 중인 disconnect 정보 (connected 상태면 None)
        self._current_gap_start: Optional[datetime] = None

        logger.info(
            "WebSocketHandler 초기화 (모드: {})",
            "모의투자" if self.use_mock else "실전",
        )

    def on_price_update(self, callback: Callable):
        """
        실시간 체결가 수신 콜백 등록

        Args:
            callback: 체결 데이터를 받는 함수
                      callback(data: dict) 형태
                      data = {"symbol": 종목코드, "price": 현재가, "volume": 거래량, ...}
        """
        self._on_price_callbacks.append(callback)
        logger.info("체결가 콜백 등록 (총 {}개)", len(self._on_price_callbacks))

    def on_orderbook_update(self, callback: Callable):
        """
        실시간 호가 수신 콜백 등록

        Args:
            callback: 호가 데이터를 받는 함수
        """
        self._on_orderbook_callbacks.append(callback)
        logger.info("호가 콜백 등록 (총 {}개)", len(self._on_orderbook_callbacks))

    @staticmethod
    def _gap_range_swing_pct(bars: List[Dict[str, Any]]) -> Optional[float]:
        """분봉 구간 내 (고가 최대 - 저가 최소) / 저가 * 100."""
        if not bars:
            return None
        lows = [float(b["low"]) for b in bars if b.get("low") is not None and float(b["low"]) > 0]
        highs = [float(b["high"]) for b in bars if b.get("high") is not None and float(b["high"]) > 0]
        if not lows or not highs:
            return None
        lo, hi = min(lows), max(highs)
        if lo <= 0:
            return None
        return (hi - lo) / lo * 100.0

    def _emit_price_update(self, data: dict) -> None:
        """등록된 체결가 콜백에 동일 페이로드 전달."""
        for cb in self._on_price_callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.error("[WebSocket] price 콜백 오류: {}", e)

    def get_cached_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """내부 가격 캐시 조회 (웹소켓 또는 갭 복구 REST로 갱신된 값)."""
        return self._price_cache.get(symbol)

    def gap_snapshot(self) -> Dict[str, Any]:
        """
        대시보드/런타임 상태 노출용 웹소켓 갭 스냅샷.

        Returns:
            {
                "available": True,
                "is_connected": bool,
                "current_gap_since": ISO8601 | None,
                "total_gap_count": int,
                "recent_gaps": [최근 gap event list (최대 10개)],
            }
        """
        recent = list(self._gap_history)[-10:]
        return {
            "available": True,
            "is_connected": self._is_connected,
            "current_gap_since": (
                self._current_gap_start.isoformat() if self._current_gap_start else None
            ),
            "total_gap_count": len(self._gap_history),
            "recent_gaps": recent,
        }

    def _update_price_cache_from_ws(self, price_data: dict) -> None:
        sym = price_data.get("symbol")
        if not sym:
            return
        self._price_cache[sym] = {
            "quote": {
                "symbol": sym,
                "price": float(price_data.get("price") or 0),
                "prev_close": None,
                "change_rate": float(price_data.get("change_rate") or 0),
                "volume": int(price_data.get("volume") or 0),
            },
            "daily": None,
            "source_main": "websocket",
            "updated_at": datetime.now().isoformat(),
        }

    async def _rest_refresh_price_cache(self, symbols: list[str], api: Any) -> int:
        """
        KIS REST 현재가 + 일봉(최근 소량) 조회로 캐시 갱신 후 콜백 1회씩 전달.
        """
        n_ok = 0
        for symbol in symbols:
            try:
                quote = await asyncio.to_thread(api.get_current_price, symbol)
                daily = await asyncio.to_thread(api.get_daily_prices, symbol, "D", 5)
                if not quote:
                    continue
                n_ok += 1
                self._price_cache[symbol] = {
                    "quote": quote,
                    "daily": daily,
                    "source_main": "kis_rest_gap_refresh",
                    "updated_at": datetime.now().isoformat(),
                }
                prev_close = float(quote.get("prev_close") or 0)
                price = float(quote.get("price") or 0)
                chg = price - prev_close if prev_close else float(quote.get("change_rate") or 0)
                payload = {
                    "symbol": symbol,
                    "price": price,
                    "volume": int(quote.get("volume") or 0),
                    "change": chg,
                    "change_rate": float(quote.get("change_rate") or 0),
                    "time": datetime.now().strftime("%H%M%S"),
                    "cumulative_volume": int(quote.get("volume") or 0),
                    "timestamp": datetime.now().isoformat(),
                    "source": "kis_rest_gap_refresh",
                }
                self._emit_price_update(payload)
            except Exception as e:
                logger.warning("[WebSocket] REST 캐시 갱신 실패 {}: {}", symbol, e)
        logger.info("[WebSocket] 갭 복구 REST 캐시 갱신 완료: {}/{} 종목", n_ok, len(symbols))
        return n_ok

    async def _run_blackswan_gap_check(
        self,
        symbols: list[str],
        detector: Any,
        api: Any,
    ) -> None:
        """갭 5분 이상 시 개별 종목 급락 로직(check_stock) 즉시 1회 실행."""
        if api is None:
            logger.warning("[WebSocket] BlackSwan 갭 점검 생략: KIS API 없음")
            return
        for symbol in symbols:
            try:
                entry = self._price_cache.get(symbol) or {}
                q = entry.get("quote") if isinstance(entry, dict) else None
                if not q or not q.get("price") or not q.get("prev_close"):
                    q = await asyncio.to_thread(api.get_current_price, symbol)
                    if q:
                        self._price_cache[symbol] = {
                            "quote": q,
                            "daily": entry.get("daily") if isinstance(entry, dict) else None,
                            "source_main": "kis_rest_blackswan_fill",
                            "updated_at": datetime.now().isoformat(),
                        }
                if not q:
                    continue
                price = float(q.get("price") or 0)
                prev_close = float(q.get("prev_close") or 0)
                if price <= 0 or prev_close <= 0:
                    continue
                result = detector.check_stock(symbol, price, prev_close)
                if result.get("triggered"):
                    logger.warning(
                        "[WebSocket] 갭 복구 후 BlackSwan 즉시 점검 발동: {} — {}",
                        symbol,
                        result.get("reason", ""),
                    )
            except Exception as e:
                logger.warning("[WebSocket] BlackSwan 갭 점검 실패 {}: {}", symbol, e)

    def _notify_websocket_gap_discord(
        self,
        disconnect_at: datetime,
        reconnect_at: datetime,
        gap: timedelta,
    ) -> None:
        try:
            from core.notifier import Notifier

            gap_sec = gap.total_seconds()
            body = (
                f"끊김: {disconnect_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"재연결: {reconnect_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"갭: {gap_sec:.0f}초 ({gap_sec / 60.0:.2f}분)"
            )
            Notifier(self.config).send_embed(
                title="웹소켓 갭 경고",
                description=body,
                color=0xF0AD4E,
                fields=None,
                critical=False,
            )
        except Exception as e:
            logger.warning("[WebSocket] 갭 디스코드 알림 실패: {}", e)

    async def _minute_bar_gap_backfill(
        self,
        symbols: list[str],
        gap: timedelta,
        api: Any,
        detector: Any,
    ) -> int:
        """
        갭이 1분 초과 시 REST 분봉 보충 후 변동률 검사(기존 로직).
        Returns:
            보충 조회를 시도한 종목 수.
        """
        gap_sec = gap.total_seconds()
        if gap_sec <= self._GAP_MINUTE_BACKFILL.total_seconds():
            return 0
        gap_min = max(1, int(gap_sec / 60))
        minutes_fetch = gap_min + 5

        n_queried = 0
        for symbol in symbols:
            n_queried += 1
            try:
                bars = await asyncio.to_thread(api.get_price_history, symbol, minutes_fetch)
                if not bars:
                    continue
                swing = self._gap_range_swing_pct(bars)
                if swing is None:
                    continue
                if swing >= 3.0:
                    logger.warning("[WebSocket] {} 갭 중 {:.1f}% 급변 감지", symbol, swing)
                    detector.report_websocket_gap_volatility(symbol, swing)
            except Exception as e:
                logger.debug("[WebSocket] 갭 보충 조회 실패 {}: {}", symbol, e)
        return n_queried

    async def _after_websocket_reconnected(
        self,
        symbols: list[str],
        reconnect_time: datetime,
        gap_td: timedelta,
    ) -> None:
        """재연결 직후 갭 로그·REST 캐시·블랙스완·분봉 보충·알림 순 처리."""
        gap_sec = gap_td.total_seconds()
        disc = self._disconnect_time

        if disc is not None and gap_sec > 0:
            logger.info(
                "[WebSocket] 재연결 성공 | 끊김 시각={} | 재연결 시각={} | 갭={:.0f}초 ({:.2f}분)",
                disc.strftime("%Y-%m-%d %H:%M:%S"),
                reconnect_time.strftime("%Y-%m-%d %H:%M:%S"),
                gap_sec,
                gap_sec / 60.0,
            )

        # gap event 기록 초기화 (아래에서 결과 채움)
        gap_event: Dict[str, Any] = {
            "disconnect_at": disc.isoformat() if disc else None,
            "reconnect_at": reconnect_time.isoformat(),
            "gap_seconds": round(gap_sec, 1),
            "affected_symbols": list(symbols),
            "rest_backfill_performed": False,
            "rest_backfill_count": 0,
            "blackswan_checked": False,
            "blackswan_cooldown_triggered": False,
            "minute_bar_backfill_count": 0,
            "observed_volatility": {},
        }

        api = None

        async def _ensure_api():
            nonlocal api
            if api is not None:
                return api
            from api.kis_api import KISApi

            api = KISApi()
            try:
                await asyncio.to_thread(api.authenticate)
            except Exception as e:
                logger.warning("[WebSocket] 갭 처리용 KIS 인증 실패: {}", e)
                return None
            return api

        detector = None

        def _detector():
            nonlocal detector
            if detector is None:
                from core.blackswan_detector import BlackSwanDetector

                detector = BlackSwanDetector(self.config)
            return detector

        needs_kis = (
            gap_td >= self._GAP_REST_REFRESH
            or gap_td >= self._GAP_BLACKSWAN_RECHECK
            or gap_td > self._GAP_MINUTE_BACKFILL
        )
        kis = await _ensure_api() if needs_kis else None

        if gap_td >= self._GAP_REST_REFRESH and kis:
            n_ok = await self._rest_refresh_price_cache(symbols, kis)
            gap_event["rest_backfill_performed"] = True
            gap_event["rest_backfill_count"] = n_ok

        if gap_td >= self._GAP_BLACKSWAN_RECHECK and kis:
            det = _detector()
            was_on_cooldown = det.is_on_cooldown()
            await self._run_blackswan_gap_check(symbols, det, kis)
            gap_event["blackswan_checked"] = True
            gap_event["blackswan_cooldown_triggered"] = (
                not was_on_cooldown and det.is_on_cooldown()
            )

        n_backfill = 0
        if gap_td > self._GAP_MINUTE_BACKFILL and kis:
            n_backfill = await self._minute_bar_gap_backfill(
                symbols, gap_td, kis, _detector()
            )
        gap_event["minute_bar_backfill_count"] = n_backfill

        # 갭 구간 관측 변동률 수집 (price_cache에서 추출)
        for sym in symbols:
            entry = self._price_cache.get(sym)
            if entry and isinstance(entry, dict):
                q = entry.get("quote")
                if q and q.get("change_rate") is not None:
                    gap_event["observed_volatility"][sym] = round(
                        abs(float(q["change_rate"])), 2
                    )

        if disc is not None and gap_td >= self._GAP_NOTIFY_MIN:
            self._notify_websocket_gap_discord(disc, reconnect_time, gap_td)

        # ring buffer에 기록
        if disc is not None and gap_sec > 0:
            self._gap_history.append(gap_event)

        # 현재 gap 해소
        self._current_gap_start = None

        # 대시보드 런타임 상태에 gap 스냅샷 즉시 반영
        try:
            from monitoring.dashboard_runtime_state import merge_ws_gap

            merge_ws_gap(self.gap_snapshot())
        except Exception as e:
            logger.debug("[WebSocket] 갭 스냅샷 대시보드 반영 실패: {}", e)

        logger.info(
            "[WebSocket] 갭 후속 처리 완료 (분봉 보충 시도 종목 수: {})",
            n_backfill,
        )

    async def connect(self, symbols: list[str]):
        """
        웹소켓 연결 및 종목 구독 (Auto-Reconnect 무한 루프)
        """
        if not self.app_key or self.app_key == "YOUR_APP_KEY_HERE":
            logger.warning("KIS API 키 미설정 — 웹소켓 연결 불가")
            return

        from api.kis_api import KISApi
        api = KISApi()
        self.approval_key = api.get_approval_key()
        if not self.approval_key:
            logger.error("approval key 발급 실패 — 웹소켓 연결 중단")
            return

        masked_key = api._mask_key(self.approval_key) if self.approval_key else "****"
        logger.info(
            "웹소켓 연결 준비 완료 (url: {}, 종목 수: {}, approval_key: {})",
            self.ws_url, len(symbols), masked_key,
        )
        self._should_reconnect = True
        retry_count = 0

        while self._should_reconnect:
            logger.info("웹소켓 연결 시도: {} (재시도: {}, 종목: {})", self.ws_url, retry_count, symbols)

            try:
                # ping_interval=None (수동 Ping-Pong 처리를 위해)
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    reconnect_time = datetime.now()
                    if self._disconnect_time is not None:
                        gap_td = reconnect_time - self._disconnect_time
                    else:
                        gap_td = timedelta(0)

                    await self._after_websocket_reconnected(symbols, reconnect_time, gap_td)
                    self._disconnect_time = None

                    self._ws = ws
                    self._is_connected = True
                    retry_count = 0  # 성공 시 재시도 카운트 리셋
                    
                    self._last_ping_time = asyncio.get_event_loop().time()
                    self._last_pong_time = self._last_ping_time

                    logger.info(
                        "웹소켓 연결 성공 (url: {}, 구독 종목: {})",
                        self.ws_url, symbols,
                    )

                    # 종목 구독 요청
                    for symbol in symbols:
                        await self._subscribe(ws, symbol)

                    # 백그라운드 태스크: Heartbeat 감시 & 메시지 수신
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    receive_task = asyncio.create_task(self._receive_loop(ws))

                    done, pending = await asyncio.wait(
                        [heartbeat_task, receive_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # 하나라도 종료되면 나머지 태스크 취소 후 강제 재연결 유도
                    for task in pending:
                        task.cancel()

            except asyncio.CancelledError:
                logger.info("웹소켓 연결 강제 취소")
                break
            except Exception as e:
                logger.error(
                    "웹소켓 연결 오류: {} (url: {}, 예외: {})",
                    e, self.ws_url, type(e).__name__,
                )

            was_live = self._is_connected
            self._is_connected = False
            self._ws = None

            if self._should_reconnect:
                if was_live:
                    now = datetime.now()
                    self._disconnect_time = now
                    self._current_gap_start = now
                    try:
                        from monitoring.dashboard_runtime_state import merge_ws_gap

                        merge_ws_gap(self.gap_snapshot())
                    except Exception:
                        pass
                retry_count += 1
                wait_time = min(60, 2 ** min(retry_count, 6))
                logger.warning("웹소켓 끊김 — {}초 후 재연결 시도", wait_time)
                await asyncio.sleep(wait_time)

    async def _receive_loop(self, ws):
        """메시지 지속 수신 루프"""
        try:
            async for message in ws:
                # 메시지를 받을 때마다 pong 시간 업데이트 (데이터가 오고 있으면 살아있음)
                self._last_pong_time = asyncio.get_event_loop().time()
                
                # API 서버측 Ping 메시지인 경우 Pong 응답 처리 (서버 정책에 따라)
                if isinstance(message, str) and message == "PING":
                    await ws.send("PONG")
                    continue
                elif isinstance(message, bytes): # 간혹 바이트로 오는 경우 처리
                    continue
                    
                await self._handle_message(message)
        except Exception as e:
            logger.error("웹소켓 수신 루프 오류: {}", e)

    async def _heartbeat_loop(self):
        """
        좀비 커넥션 감지 쓰레드
        - 30초마다 Ping(어플리케이션 계층 혹은 프로토콜 수준) 점검
        - 45초 이상 데이터 또는 응답이 없으면 연결 취소
        """
        try:
            while self._is_connected and self._should_reconnect:
                await asyncio.sleep(15)
                
                now = asyncio.get_event_loop().time()
                # 45초 이상 아무 데이터(또는 PONG)를 받지 못했다면 좀비 커넥션으로 간주
                if now - self._last_pong_time > 45.0:
                    logger.error("Heartbeat 타임아웃! (좀비 커넥션 감지) - 강제 종료 및 재연결")
                    ws_temp = self._ws
                    if ws_temp:
                        await ws_temp.close()
                    break
                    
                # 30초 이상 응답이 조용하면 Ping 트리거
                if now - self._last_ping_time > 30.0:
                    try:
                        ws_temp = self._ws
                        if ws_temp:
                            await ws_temp.ping()
                            self._last_ping_time = now
                    except Exception as e:
                        logger.error("Ping 전송 실패: {}", e)
                        ws_temp = self._ws
                        if ws_temp:
                            await ws_temp.close()
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Heartbeat 루프 오류: {}", e)

    async def _subscribe(self, ws, symbol: str):
        """종목 실시간 체결가 구독"""
        # KIS 웹소켓 구독 메시지 포맷
        subscribe_msg = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": "1",         # 1: 등록
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNT0",  # 실시간 체결가
                    "tr_key": symbol,
                }
            }
        }
        await ws.send(json.dumps(subscribe_msg))
        logger.info("종목 {} 구독 요청 완료", symbol)

    async def _handle_message(self, message: str):
        """수신 메시지 파싱 및 콜백 호출"""
        try:
            # KIS 웹소켓 응답은 '|' 구분 또는 JSON
            if message.startswith("{"):
                data = json.loads(message)
                logger.debug("웹소켓 응답: {}", data)
                return

            # 실시간 데이터 파싱 ('|' 구분, '^' 필드 구분)
            parts = message.split("|")
            if len(parts) < 4:
                return

            tr_id = parts[1]      # 거래 ID
            data_count = parts[2]  # 데이터 건수
            raw_data = parts[3]    # 실제 데이터

            if tr_id == "H0STCNT0":
                # 체결 데이터
                price_data = self._parse_price_data(raw_data)

                # 실시간 데이터 정합성 검증
                from core.data_validator import DataValidator
                if price_data and DataValidator.validate_realtime_data(price_data):
                    if not self._first_data_logged:
                        self._first_data_logged = True
                        logger.info(
                            "웹소켓 첫 실시간 데이터 수신 (tr_id: {}, symbol: {}, price: {})",
                            tr_id, price_data.get("symbol"), price_data.get("price"),
                        )
                    self._update_price_cache_from_ws(price_data)
                    self._emit_price_update(price_data)
                elif price_data:
                    logger.warning("웹소켓 손상 데이터 드롭: {}", price_data)

        except Exception as e:
            logger.error("메시지 처리 오류: {}", e)

    @staticmethod
    def _parse_price_data(raw: str) -> Optional[Dict[str, Any]]:
        """
        실시간 체결 데이터 파싱

        Returns:
            {
                "symbol": 종목코드,
                "price": 현재가,
                "volume": 거래량,
                "change": 전일 대비,
                "change_rate": 등락률,
                "time": 체결 시간,
            }
        """
        fields = raw.split("^")
        if len(fields) < 20:
            return None

        try:
            return {
                "symbol": fields[0],                    # 종목코드
                "time": fields[1],                      # 체결시간 (HHMMSS)
                "price": float(fields[2]),               # 현재가
                "change": float(fields[4]),              # 전일 대비
                "change_rate": float(fields[5]),          # 등락률
                "volume": int(fields[12]),               # 거래량
                "cumulative_volume": int(fields[13]),     # 누적 거래량
                "timestamp": datetime.now().isoformat(),
            }
        except (ValueError, IndexError):
            return None

    async def disconnect(self):
        """웹소켓 강제 연결 종료 (종료 플래그 설정)"""
        self._should_reconnect = False
        ws_temp = self._ws
        if ws_temp and self._is_connected:
            await ws_temp.close()
            self._is_connected = False
            logger.info("웹소켓 명시적 연결 종료")

    @property
    def is_connected(self) -> bool:
        return self._is_connected

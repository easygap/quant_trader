"""
실시간 웹소켓 데이터 핸들러
- KIS API 웹소켓을 통한 실시간 체결/호가 스트리밍
- asyncio 기반 비동기 처리
"""

import json
import asyncio
from datetime import datetime
from typing import Callable, Optional, Dict, Any
import time

from loguru import logger
import websockets
import websockets.exceptions

from config.config_loader import Config


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

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        kis = self.config.kis_api

        self.app_key = kis.get("app_key", "")
        self.app_secret = kis.get("app_secret", "")
        self.use_mock = kis.get("use_mock", True)

        self.ws_url = self.MOCK_WS_URL if self.use_mock else self.REAL_WS_URL

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

    async def connect(self, symbols: list[str]):
        """
        웹소켓 연결 및 종목 구독 (Auto-Reconnect 무한 루프)
        """
        if not self.app_key or self.app_key == "YOUR_APP_KEY_HERE":
            logger.warning("KIS API 키 미설정 — 웹소켓 연결 불가")
            return

        self._should_reconnect = True
        retry_count = 0

        while self._should_reconnect:
            logger.info("웹소켓 연결 시도: {} (재시도: {}, 종목: {})", self.ws_url, retry_count, symbols)

            try:
                # ping_interval=None (수동 Ping-Pong 처리를 위해)
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    self._is_connected = True
                    retry_count = 0  # 성공 시 재시도 카운트 리셋
                    
                    self._last_ping_time = asyncio.get_event_loop().time()
                    self._last_pong_time = self._last_ping_time

                    logger.info("웹소켓 연결 성공")

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
                logger.error("웹소켓 연결 오류: {}", e)

            self._is_connected = False
            self._ws = None

            if self._should_reconnect:
                retry_count += 1
                # 재시도 지수 백오프 (최대 60초)
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
                "approval_key": self.app_key,
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
                    for callback in self._on_price_callbacks:
                        callback(price_data)
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

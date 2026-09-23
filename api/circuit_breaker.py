"""
Circuit Breaker 패턴 모듈
- 장시간 API 서버 장애 시, 불필요한 재요청을 차단하여 계정 Ban 방지
- 상태 전환: CLOSED(정상) -> OPEN(차단) -> HALF-OPEN(복구 테스트)
"""

import time
from enum import Enum
from loguru import logger
import threading


class CircuitState(Enum):
    CLOSED = "CLOSED"           # 정상 상태 (모든 요청 통과)
    OPEN = "OPEN"               # 차단 상태 (모든 요청 거절)
    HALF_OPEN = "HALF_OPEN"     # 복구 테스트 상태 (제한된 요청만 통과)


class CircuitBreaker:
    """
    API 요청에 대한 Circuit Breaker
    
    연속 실패 횟수가 임계치를 넘으면 회로를 열어(OPEN) 일정 시간 요청을 차단합니다.
    쿨다운 후 반열림(HALF_OPEN) 상태로 전환하여 요청 성공 시 다시 닫힙니다(CLOSED).
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self._half_open_probe_in_flight = False
        self._half_open_probe_started_at = 0.0
        
        # 스레드 안전성 보장
        self._lock = threading.Lock()

    def can_request(self) -> bool:
        """현재 요청 가능한 상태인지 확인"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                now = time.monotonic()
                # 쿨다운 타임아웃 지났으면 HALF_OPEN 전환
                if now - self.last_failure_time >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self._half_open_probe_in_flight = True
                    self._half_open_probe_started_at = now
                    logger.info("Circuit Breaker 상태 전환: OPEN -> HALF_OPEN (복구 테스트 진행)")
                    return True
                return False

            if self.state == CircuitState.HALF_OPEN:
                now = time.monotonic()
                if self._half_open_probe_in_flight:
                    if now - self._half_open_probe_started_at >= self.recovery_timeout:
                        logger.warning(
                            "Circuit Breaker HALF_OPEN probe 응답 타임아웃 — 새 probe 허용"
                        )
                    else:
                        return False
                self._half_open_probe_started_at = now
                self._half_open_probe_in_flight = True
                return True

        return False

    def on_success(self):
        """요청 성공 시 호출 (통계 리셋 및 CLOSED 복귀)"""
        with self._lock:
            if self.state != CircuitState.CLOSED:
                logger.info("Circuit Breaker 상태 전환: {} -> CLOSED (통신 복구)", self.state.value)
            
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self._half_open_probe_in_flight = False
            self._half_open_probe_started_at = 0.0

    def release_probe(self):
        """HALF_OPEN probe가 확정적 응답(400/401/403/429 등)을 받았을 때 호출.

        서버가 응답했으니 장애는 아니지만 요청이 성공한 것도 아니다. 상태는
        HALF_OPEN으로 두고(닫지 않는다) probe 점유만 풀어, 다음 호출이
        recovery_timeout(60초)을 기다리지 않고 곧바로 다시 probe하게 한다.
        풀지 않으면 그동안 손절 SELL을 포함한 모든 KIS 요청이 이유 없이 막힌다.
        """
        with self._lock:
            if self.state == CircuitState.HALF_OPEN and self._half_open_probe_in_flight:
                self._half_open_probe_in_flight = False
                self._half_open_probe_started_at = 0.0
                logger.info(
                    "Circuit Breaker HALF_OPEN probe 해제 — 서버가 확정 응답, 상태는 HALF_OPEN 유지"
                )

    def on_failure(self):
        """요청 실패(50x, 타임아웃 등) 시 호출"""
        alert_failure_count = None
        with self._lock:
            self.last_failure_time = time.monotonic()
            self._half_open_probe_in_flight = False
            self._half_open_probe_started_at = 0.0

            if self.state == CircuitState.HALF_OPEN:
                # HALF_OPEN 상태에서 또 실패하면 다시 OPEN으로 회귀
                self.state = CircuitState.OPEN
                logger.warning("Circuit Breaker 복구 실패: 다시 OPEN 상태로 전환")

            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.error("🚨 Circuit Breaker 발동! 상태 전환: CLOSED -> OPEN ({}회 연속 실패)", self.failure_count)
                    alert_failure_count = self.failure_count

        # 알림은 Discord·SMTP 네트워크 I/O(수 초 이상)라 락을 놓은 뒤 보낸다. 락 안에서
        # 보내면 그동안 can_request/on_success를 부르는 모든 스레드(주문 제출·손절
        # SELL 포함)가 알림 전송이 끝날 때까지 멈춘다. 발동 판정은 위 락 안에서 끝났다.
        if alert_failure_count is not None:
            self._trigger_alert(alert_failure_count)

    def _trigger_alert(self, failure_count: int | None = None):
        """서킷 브레이커 오픈 시 알림 — 치명적 이벤트이므로 모든 채널 동시 발송."""
        count = self.failure_count if failure_count is None else failure_count
        try:
            from core.notifier import Notifier
            notifier = Notifier()
            notifier.send_message(
                f"🚨 **서킷 브레이커 발동 (API 차단)**\n"
                f"연속 {count}회 API 요청 실패로 인해 모든 통신을 {self.recovery_timeout}초간 차단합니다.\n"
                f"서버 다운 또는 장애가 의심됩니다.",
                critical=True,
            )
        except Exception as e:
            logger.error("서킷 브레이커 알림 실패: {}", e)

# 싱글톤 인스턴스 제공 (kis_api 등에서 공유 사용)
_breaker_instance = CircuitBreaker()

def get_breaker() -> CircuitBreaker:
    return _breaker_instance

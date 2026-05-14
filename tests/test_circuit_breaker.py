import time


def test_half_open_allows_only_one_probe_until_result():
    """OPEN 이후 복구 probe는 한 번만 열고 응답 전 추가 요청은 차단한다."""
    from api.circuit_breaker import CircuitBreaker, CircuitState

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1.0)
    breaker.state = CircuitState.OPEN
    breaker.last_failure_time = time.monotonic() - 2.0

    assert breaker.can_request() is True
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_request() is False

    breaker.on_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.can_request() is True


def test_half_open_failure_reopens_and_blocks_until_next_timeout():
    """복구 probe 실패 시 다시 OPEN으로 돌아가고 쿨다운 전 요청을 막는다."""
    from api.circuit_breaker import CircuitBreaker, CircuitState

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0)
    breaker.state = CircuitState.HALF_OPEN
    breaker._half_open_probe_in_flight = True

    breaker.on_failure()

    assert breaker.state == CircuitState.OPEN
    assert breaker.can_request() is False


def test_stale_half_open_probe_can_be_retried_after_timeout():
    """probe 콜백이 누락돼도 timeout 이후에는 새 복구 probe를 허용한다."""
    from api.circuit_breaker import CircuitBreaker, CircuitState

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1.0)
    breaker.state = CircuitState.HALF_OPEN
    breaker._half_open_probe_in_flight = True
    breaker._half_open_probe_started_at = time.monotonic() - 2.0

    assert breaker.can_request() is True
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_request() is False

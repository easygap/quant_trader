"""
한국투자증권 (KIS) API 래퍼 모듈
- REST API를 통한 시세 조회, 주문 실행, 잔고 조회
- 토큰 발급 및 자동 갱신
- 모의투자 / 실전 도메인 전환 지원
- Rate Limiter: Token Bucket(초당) + 슬라이딩 윈도우(분당) 이중 제어
"""

import time
import json
import random
import ssl
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import threading

import requests
from loguru import logger

from config.config_loader import Config
from api.circuit_breaker import get_breaker


class KISTokenExpiredError(Exception):
    """KIS API 401 응답(토큰 만료) 시 사용. CircuitBreaker 실패로 누적하지 않음."""


class KISOrderResponseUnknown(Exception):
    """비멱등(주문 제출) 요청에서 응답을 받지 못한 네트워크 오류.

    주문 바이트가 브로커에 도달했을 수 있어 체결 여부가 불명한 상태다.
    재전송하면 이중 체결 위험이 있으므로, 상위(재시도 래퍼)가 이 예외를 받으면
    재전송하지 않고 reconcile(미체결 조회·잔고 대조) 경로로 넘겨야 한다.
    """


class KISApi:
    """
    한국투자증권 Open API 래퍼

    사용법:
        api = KISApi()
        api.authenticate()
        price = api.get_current_price("005930")
    """

    def __init__(self, account_no: str = None):
        """
        account_no: 지정 시 해당 계좌 사용 (다중 계좌/전략별 분리).
        None이면 설정의 kis_api.account_no 사용.
        """
        config = Config.get()
        kis = config.kis_api

        self.app_key = kis.get("app_key", "")
        self.app_secret = kis.get("app_secret", "")
        self.account_no = (account_no if account_no is not None else "") or kis.get("account_no", "")
        self.use_mock = kis.get("use_mock", True)

        # 도메인 설정 (모의투자 / 실전)
        if self.use_mock:
            self.base_url = kis.get("mock_url", "https://openapivts.koreainvestment.com:29443")
        else:
            self.base_url = kis.get("base_url", "https://openapi.koreainvestment.com:9443")

        # 인증 토큰
        self._access_token = None
        self._token_expires_at = None

        # 계좌번호 파싱 (XXXXXXXX-XX)
        parts = self.account_no.split("-")
        self.cano = parts[0] if len(parts) >= 1 else ""    # 종합 계좌 번호
        self.acnt_prdt_cd = parts[1] if len(parts) >= 2 else "01"  # 계좌 상품 코드

        # --- Rate Limiter (이중 제어) ---
        # 1) Token Bucket: 초당 한도 (burst 제어)
        # 2) Sliding Window: 분당 한도 (지속적 버스트 방지)
        self.max_calls_per_sec = float(kis.get("max_calls_per_sec", 10.0))
        self.max_calls_per_min = int(kis.get("max_calls_per_min", 300))
        self._tokens = self.max_calls_per_sec
        self._last_refill = time.monotonic()
        self._token_lock = threading.Lock()
        self._auth_lock = threading.Lock()

        # 분당 슬라이딩 윈도우: 최근 60초 내 요청 타임스탬프
        self._minute_window: deque[float] = deque()
        self._minute_lock = threading.Lock()

        # 모니터링 카운터 (사용량 추적)
        self._total_requests = 0
        self._total_429s = 0
        self._total_conn_errors = 0
        self._session_start = time.monotonic()

        # 토큰 에러 쿨다운: 발급 실패 시 60초간 재시도 억제
        self._token_error_until: float = 0.0

        logger.info(
            "KIS API 초기화 완료 (모드: {}, 계좌: {}, RateLimit: {}/sec, {}/min)",
            "모의투자" if self.use_mock else "실전",
            self.account_no,
            self.max_calls_per_sec,
            self.max_calls_per_min,
        )

    def _notify_auth_failure(self, message: str):
        """토큰 발급/갱신 실패 시 즉시 디스코드 등 알림. 실전 모드에서 주문이 조용히 실패하는 것을 방지."""
        text = (
            f"🚨 **KIS API 토큰 만료·갱신 실패**\n"
            f"{message}\n"
            "실전 모드에서는 이후 주문이 **조용히 실패**할 수 있으니 **즉시 확인**하세요."
        )
        try:
            from core.notifier import Notifier
            Notifier().send_message(text, critical=True)
        except Exception as exc:
            logger.error("KIS 인증 실패 알림 전송 실패: {}", exc)
        try:
            from monitoring.discord_bot import DiscordBot
            DiscordBot().send_message(text)
        except Exception as exc:
            logger.debug("KIS 인증 실패 디스코드 직접 발송 실패: {}", exc)

    def _is_configured(self) -> bool:
        """API 키가 설정되었는지 확인"""
        return (
            self.app_key != "YOUR_APP_KEY_HERE"
            and self.app_secret != "YOUR_APP_SECRET_HERE"
            and len(self.app_key) > 0
        )

    @staticmethod
    def _mask_key(key: str, head: int = 4, tail: int = 4) -> str:
        """민감 정보 마스킹 (앞뒤 일부만 노출). 빈 문자열은 그대로 반환."""
        if not key or len(key) <= head + tail:
            return "****" if key else ""
        return f"{key[:head]}...{key[-tail:]}"

    # =============================================================
    # 인증
    # =============================================================

    def authenticate(self) -> bool:
        """
        OAuth 토큰 발급 (동시 갱신 방지를 위해 Lock 사용)
        Returns:
            성공 여부
        """
        with self._auth_lock:
            return self._authenticate_impl()

    def _authenticate_impl(self) -> bool:
        """토큰 발급 실제 로직 (Lock 내부에서만 호출)."""
        if not self._is_configured():
            logger.warning("KIS API 키가 설정되지 않았습니다. settings.yaml을 확인해주세요.")
            return False

        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

        try:
            response = requests.post(url, json=body, timeout=10)
            domain = "모의투자" if self.use_mock else "실전"
            if not response.ok:
                try:
                    err_body = response.json()
                    err_msg = err_body.get("msg", err_body.get("error_description", str(err_body)))[:200]
                except Exception:
                    err_msg = response.text[:200] if response.text else ""
                logger.error(
                    "KIS API 토큰 발급 실패 [{}] {} (app_key: {})",
                    response.status_code, err_msg, self._mask_key(self.app_key),
                )
                self._notify_auth_failure(
                    f"HTTP {response.status_code} / {err_msg or '토큰 발급 실패'}"
                )
                return False
            response.raise_for_status()
            data = response.json()

            self._access_token = data.get("access_token")
            # 토큰 유효시간 (기본 24시간에서 1시간 여유)
            expires_in = int(data.get("expires_in", 86400))
            self._token_expires_at = datetime.now() + timedelta(seconds=expires_in - 3600)

            logger.info(
                "KIS API 토큰 발급 성공 (도메인: {}, 만료: {})",
                domain, self._token_expires_at,
            )
            return True
        except requests.RequestException as e:
            logger.error(
                "KIS API 토큰 발급 네트워크 오류: {} (url: {})",
                e, url.split("?")[0],
            )
            self._notify_auth_failure(str(e))
            return False
        except Exception as e:
            logger.error("KIS API 토큰 발급 실패: {}", e)
            self._notify_auth_failure(str(e))
            return False

    def _ensure_token(self):
        """토큰이 유효한지 확인하고, 만료 임박 시 갱신"""
        if self._access_token is None or (
            self._token_expires_at and datetime.now() >= self._token_expires_at
        ):
            self.authenticate()

    def _get_headers(self, tr_id: str) -> dict:
        """API 요청 헤더 생성"""
        self._ensure_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

    def _wait_for_token(self):
        """
        이중 Rate Limiter: Token Bucket(초당) + 슬라이딩 윈도우(분당).
        두 조건 모두 충족해야 요청 진행.
        """
        # 1) 분당 슬라이딩 윈도우 체크
        self._wait_for_minute_window()

        # 2) 초당 Token Bucket
        with self._token_lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill

                self._tokens = self._tokens + (elapsed * self.max_calls_per_sec)
                if self._tokens > self.max_calls_per_sec:
                    self._tokens = self.max_calls_per_sec
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens = self._tokens - 1.0
                    break
                else:
                    sleep_time = (1.0 - self._tokens) / self.max_calls_per_sec
                    time.sleep(max(0.01, sleep_time))

        # 분당 윈도우에 현재 요청 기록
        with self._minute_lock:
            self._minute_window.append(time.monotonic())
        self._total_requests += 1

    def _wait_for_minute_window(self):
        """분당 한도 초과 시 가장 오래된 요청이 윈도우를 벗어날 때까지 대기."""
        while True:
            with self._minute_lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._minute_window and self._minute_window[0] < cutoff:
                    self._minute_window.popleft()
                if len(self._minute_window) < self.max_calls_per_min:
                    return
                oldest = self._minute_window[0]
                wait = oldest - cutoff + 0.1
            logger.debug(
                "분당 한도 도달 ({}/{}), {:.1f}초 대기",
                len(self._minute_window), self.max_calls_per_min, wait,
            )
            time.sleep(wait)

    def get_rate_limit_stats(self) -> dict:
        """현재 Rate Limiter 사용량 통계 반환."""
        with self._minute_lock:
            now = time.monotonic()
            cutoff = now - 60.0
            while self._minute_window and self._minute_window[0] < cutoff:
                self._minute_window.popleft()
            recent_minute = len(self._minute_window)

        elapsed_sec = max(1, time.monotonic() - self._session_start)
        return {
            "total_requests": self._total_requests,
            "total_429s": self._total_429s,
            "total_conn_errors": self._total_conn_errors,
            "requests_last_60s": recent_minute,
            "max_per_sec": self.max_calls_per_sec,
            "max_per_min": self.max_calls_per_min,
            "avg_per_sec": round(self._total_requests / elapsed_sec, 2),
            "minute_utilization_pct": round(recent_minute / self.max_calls_per_min * 100, 1),
            "token_cooldown_active": time.monotonic() < self._token_error_until,
        }

    @staticmethod
    def _backoff_with_jitter(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
        """지수 백오프 + 랜덤 지터. 동시 요청의 thundering-herd 방지."""
        exp = min(base * (2 ** (attempt - 1)), cap)
        return exp * (0.5 + random.random() * 0.5)

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict = None,
        body: dict = None,
        max_retries: int = None,
        idempotent: bool = True,
    ) -> Dict[str, Any]:
        """
        API 요청 공통 메서드 (에러별 재시도 로직 포함)

        강화 사항:
        - SSL/연결 오류 전용 재시도 + 서킷 누적
        - 지수 백오프에 랜덤 지터 적용 (thundering-herd 방지)
        - 토큰 발급 실패 시 60초 쿨다운으로 연속 실패 루프 방지

        idempotent=False (주문 제출 등)일 때는 응답을 못 받은 네트워크 오류에서
        재시도하지 않는다. 브로커가 이미 주문을 받았는데 응답만 유실된 경우 같은 주문을
        다시 POST하면 이중 체결이 나기 때문이다. 이때는 빈 응답을 돌려 상위의
        미체결 조회/reconcile 로직이 판단하도록 한다(최대 1회 제출 원칙).
        """
        breaker = get_breaker()
        if not breaker.can_request():
            logger.warning("Circuit Breaker 동작 중! API 요청 즉시 차단: {}", path)
            return {}

        if not self._is_configured():
            logger.warning("KIS API 미설정 — 빈 응답 반환")
            return {}

        # 토큰 에러 쿨다운 중이면 즉시 반환
        if time.monotonic() < self._token_error_until:
            remaining = self._token_error_until - time.monotonic()
            logger.warning("토큰 에러 쿨다운 중 ({:.0f}초 남음) — 요청 스킵: {}", remaining, path)
            return {}

        url = f"{self.base_url}{path}"
        max_retries = max_retries if max_retries is not None else int(Config.get().kis_api.get("max_retry", 3))

        for attempt in range(1, max_retries + 1):
            # 루프 내에서 서킷이 OPEN으로 바뀌었으면 더 이상 서버를 때리지 않는다.
            if attempt > 1 and not breaker.can_request():
                logger.warning("Circuit Breaker 동작 — 재시도 중단: {}", path)
                return {}

            headers = self._get_headers(tr_id)
            self._wait_for_token()

            try:
                if method.upper() == "GET":
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                else:
                    response = requests.post(url, headers=headers, json=body, timeout=10)

                if response.status_code == 429:
                    self._total_429s += 1
                    try:
                        retry_after = int(response.headers.get("Retry-After", 5))
                    except (TypeError, ValueError):
                        # Retry-After가 HTTP-date 형식이면 정수 변환이 실패한다 — 기본값 사용.
                        retry_after = 5
                    retry_after = max(1, min(retry_after, 60))
                    logger.warning(
                        "[429 Too Many Requests] {}초 대기 후 재시도 ({}/{}) - 경로: {} (누적 429: {}회)",
                        retry_after, attempt, max_retries, path, self._total_429s,
                    )
                    time.sleep(retry_after)
                    continue

                if response.status_code in (500, 502, 503, 504):
                    breaker.on_failure()
                    wait = self._backoff_with_jitter(attempt)
                    logger.warning(
                        "[{}] 서버 오류, {:.1f}초 후 재시도 ({}/{}) - 경로: {}",
                        response.status_code, wait, attempt, max_retries, path,
                    )
                    time.sleep(wait)
                    continue

                if response.status_code == 401:
                    raise KISTokenExpiredError("KIS API 401 Unauthorized — 토큰 만료")

                if response.status_code in (400, 403):
                    logger.error("[{}] 복구 불가 오류 즉시 중단 - 경로: {}", response.status_code, path)
                    return {}

                response.raise_for_status()
                breaker.on_success()
                return response.json()

            except KISTokenExpiredError:
                logger.error("[401] 토큰 만료. 갱신 후 재시도 ({}/{})", attempt, max_retries)
                if not self.authenticate():
                    self._token_error_until = time.monotonic() + 60.0
                    self._notify_auth_failure(
                        "401 응답 후 토큰 자동 갱신 실패 (60초 쿨다운 진입). API 키·네트워크를 확인하세요."
                    )
                    return {}
                if attempt < max_retries:
                    continue
                return {}

            except (requests.exceptions.ConnectionError, ssl.SSLError, ConnectionResetError, EOFError) as e:
                self._total_conn_errors += 1
                breaker.on_failure()
                if not idempotent:
                    # 주문 제출처럼 비멱등 요청은 응답 유실 시 재전송하면 이중 체결 위험.
                    # 재시도하지 않고, 체결 여부 불명 예외를 던져 상위 재시도 래퍼가
                    # 재전송 대신 reconcile/미체결 조회 경로로 분기하게 한다.
                    logger.error(
                        "비멱등 요청 네트워크 오류 — 재전송하지 않고 중단(이중 체결 방지): {} - {}",
                        path, type(e).__name__,
                    )
                    raise KISOrderResponseUnknown(f"{path}: {type(e).__name__}") from e
                wait = self._backoff_with_jitter(attempt, base=2.0)
                logger.warning(
                    "연결/SSL 오류, {:.1f}초 후 재시도 ({}/{}) - 경로: {} - {} (누적: {}회)",
                    wait, attempt, max_retries, path, type(e).__name__, self._total_conn_errors,
                )
                time.sleep(wait)

            except requests.exceptions.Timeout:
                breaker.on_failure()
                if not idempotent:
                    logger.error(
                        "비멱등 요청 타임아웃 — 재전송하지 않고 중단(이중 체결 방지): {}", path,
                    )
                    raise KISOrderResponseUnknown(f"{path}: Timeout")
                wait = self._backoff_with_jitter(attempt)
                logger.warning("요청 타임아웃, {:.1f}초 후 재시도 ({}/{}) - 경로: {}", wait, attempt, max_retries, path)
                time.sleep(wait)

            except requests.exceptions.RequestException as e:
                breaker.on_failure()
                if not idempotent:
                    logger.error(
                        "비멱등 요청 실패 — 재전송하지 않고 중단(이중 체결 방지): {} - {}", path, e,
                    )
                    raise KISOrderResponseUnknown(f"{path}: {type(e).__name__}") from e
                logger.error("요청 실패: {} - {}", path, e)
                time.sleep(self._backoff_with_jitter(attempt, base=0.5))

        logger.error("API 요청 최종 실패 ({}회 모두 실패) - 경로: {}", max_retries, path)
        return {}

    # =============================================================
    # 시세 조회
    # =============================================================

    def get_current_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        종목 현재가 조회

        Args:
            symbol: 종목 코드 (예: "005930")

        Returns:
            현재가 정보 딕셔너리 또는 None
            {
                "price": 현재가,
                "open": 시가,
                "high": 고가,
                "low": 저가,
                "volume": 거래량,
                "change_rate": 등락률,
            }
        """
        # 모의투자 / 실전 거래 ID
        tr_id = "FHKST01010100"

        params = {
            "fid_cond_mrkt_div_code": "J",  # 주식
            "fid_input_iscd": symbol,
        }

        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id,
            params=params,
        )

        if not data or str(data.get("rt_cd", "")) != "0":
            return None

        output = data.get("output")
        if not isinstance(output, dict):
            return None

        def _to_float(key: str, default: float = 0.0) -> float:
            value = output.get(key)
            if value is None or str(value).strip() == "":
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _to_int(key: str, default: int = 0) -> int:
            value = output.get(key)
            if value is None or str(value).strip() == "":
                return default
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        price = _to_float("stck_prpr")
        if price <= 0:
            logger.warning("KIS 현재가 응답 가격 비정상: symbol={}, price={}", symbol, output.get("stck_prpr"))
            return None

        return {
            "symbol": symbol,
            "price": price,                            # 현재가
            "open": _to_float("stck_oprc"),            # 시가
            "high": _to_float("stck_hgpr"),            # 고가
            "low": _to_float("stck_lwpr"),             # 저가
            "volume": _to_int("acml_vol"),             # 누적 거래량
            "change_rate": _to_float("prdy_ctrt"),     # 전일 대비 등락률
            "prev_close": _to_float("stck_sdpr"),      # 전일 종가
        }

    def get_daily_prices(
        self,
        symbol: str,
        period: str = "D",
        count: int = 100,
    ) -> Optional[list]:
        """
        종목 일봉 데이터 조회 (최근 N개)

        Args:
            symbol: 종목 코드
            period: "D"(일), "W"(주), "M"(월)
            count: 조회 건수

        Returns:
            일봉 데이터 리스트
        """
        tr_id = "FHKST01010400"

        today = datetime.now().strftime("%Y%m%d")
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": symbol,
            "fid_input_date_1": (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d"),
            "fid_input_date_2": today,
            "fid_period_div_code": period,
            "fid_org_adj_prc": "0",  # 수정 주가 반영
        }

        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            tr_id,
            params=params,
        )

        if not data or "output" not in data:
            return None

        return data["output"]

    def get_price_history(self, symbol: str, minutes: int = 10) -> Optional[List[Dict[str, Any]]]:
        """
        당일 분봉(시간대별) 시세 조회 — 웹소켓 갭 구간 보충용.
        국내주식 API `inquire-time-itemchartprice` (1분 단위 응답 가정).

        Args:
            symbol: 종목코드
            minutes: 갭 길이 가늠용; 응답 행이 더 많으면 최근 minutes개만 사용

        Returns:
            [{"open","high","low","close","volume"}, ...] 시간순(오래된→최신) 또는 API 순서. 실패 시 None.
        """
        self._ensure_token()
        tr_id = "FHKST03010200"
        now = datetime.now()
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": symbol,
            "fid_input_hour_1": now.strftime("%H%M%S"),
            "fid_pw_data_incu_yn": "N",
        }
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id,
            params=params,
        )
        if not data or data.get("rt_cd") != "0":
            return None
        rows = data.get("output2") or data.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return None
        bars: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                cl = float(row.get("stck_prpr") or row.get("stck_clpr") or 0)
                hg_f = float(row.get("stck_hgpr") or 0)
                lw_f = float(row.get("stck_lwpr") or 0)
                op = float(row.get("stck_oprc") or 0) or cl
                vol = int(float(row.get("cntg_vol") or row.get("acml_vol") or 0))
                if cl <= 0 and op > 0:
                    cl = op
                if cl <= 0:
                    continue
                hi = hg_f if hg_f > 0 else cl
                lo = lw_f if lw_f > 0 else cl
                bars.append({
                    "open": op if op > 0 else cl,
                    "high": hi,
                    "low": lo,
                    "close": cl,
                    "volume": vol,
                })
            except (TypeError, ValueError):
                continue
        if not bars:
            return None
        if minutes > 0 and len(bars) > minutes:
            bars = bars[-minutes:]
        return bars

    # =============================================================
    # 주문 실행
    # =============================================================

    def buy_order(
        self,
        symbol: str,
        quantity: int,
        price: int = 0,
        order_type: str = "00",
    ) -> Optional[Dict[str, Any]]:
        """
        매수 주문

        Args:
            symbol: 종목 코드
            quantity: 주문 수량
            price: 주문 가격 (0: 시장가)
            order_type: "00"(지정가), "01"(시장가)

        Returns:
            주문 결과 딕셔너리
        """
        # 모의투자 vs 실전 거래 ID
        tr_id = "VTTC0802U" if self.use_mock else "TTTC0802U"

        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }

        data = self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body=body,
            idempotent=False,  # 주문 제출: 응답 유실 시 재전송 금지(이중 체결 방지)
        )

        if data and data.get("rt_cd") == "0":
            logger.info("매수 주문 성공: {} {}주 @ {:,}원", symbol, quantity, price)
            return data.get("output", {})
        else:
            msg = data.get("msg1", "알 수 없는 오류") if data else "API 응답 없음"
            logger.error("매수 주문 실패: {} - {}", symbol, msg)
            return None

    def sell_order(
        self,
        symbol: str,
        quantity: int,
        price: int = 0,
        order_type: str = "00",
    ) -> Optional[Dict[str, Any]]:
        """
        매도 주문

        Args:
            symbol: 종목 코드
            quantity: 주문 수량
            price: 주문 가격 (0: 시장가)
            order_type: "00"(지정가), "01"(시장가)

        Returns:
            주문 결과 딕셔너리
        """
        tr_id = "VTTC0801U" if self.use_mock else "TTTC0801U"

        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }

        data = self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body=body,
            idempotent=False,  # 주문 제출: 응답 유실 시 재전송 금지(이중 체결 방지)
        )

        if data and data.get("rt_cd") == "0":
            logger.info("매도 주문 성공: {} {}주 @ {:,}원", symbol, quantity, price)
            return data.get("output", {})
        else:
            msg = data.get("msg1", "알 수 없는 오류") if data else "API 응답 없음"
            logger.error("매도 주문 실패: {} - {}", symbol, msg)
            return None

    @staticmethod
    def _odno_from_order_output(order_output: Optional[Dict[str, Any]]) -> str:
        if not order_output or not isinstance(order_output, dict):
            return ""
        for k in ("ODNO", "odno", "ORD_NO", "ord_no"):
            v = order_output.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    @staticmethod
    def _avg_price_from_ccld_row(row: Dict[str, Any]) -> Optional[float]:
        """일별체결 output1 한 행에서 평균 체결가 추출."""
        if not row or not isinstance(row, dict):
            return None
        for k in (
            "avg_prvs", "AVG_PRVS", "avg_prc", "AVRG_PRC",
            "pchs_avg_pric", "PCHS_AVG_PRIC", "ccld_avg_pric", "CCLD_AVG_PRIC",
        ):
            raw = row.get(k)
            if raw is None or raw == "":
                continue
            try:
                p = float(raw)
                if p > 0:
                    return p
            except (TypeError, ValueError):
                continue
        amt_keys = (
            ("tot_ccld_amt", "tot_ccld_qty"),
            ("TOT_CCLD_AMT", "TOT_CCLD_QTY"),
            ("ccld_amt", "ccld_qty"),
            ("CCLD_AMT", "CCLD_QTY"),
        )
        for ak, qk in amt_keys:
            try:
                a = float(row.get(ak) or 0)
                q = float(row.get(qk) or 0)
                if a > 0 and q > 0:
                    return a / q
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _number_from_ccld_row(row: Dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
        if not row or not isinstance(row, dict):
            return None
        for key in keys:
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return None

    @classmethod
    def _execution_from_ccld_row(cls, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """일별체결 output1 한 행에서 체결가와 체결 수량을 함께 추출."""
        fill_price = cls._avg_price_from_ccld_row(row)
        if not fill_price or fill_price <= 0:
            return None
        filled_qty = cls._number_from_ccld_row(
            row,
            (
                "tot_ccld_qty", "TOT_CCLD_QTY",
                "ccld_qty", "CCLD_QTY",
                "exec_qty", "EXEC_QTY",
            ),
        )
        remaining_qty = cls._number_from_ccld_row(
            row,
            (
                "rmn_qty", "RMN_QTY",
                "ord_rmn_qty", "ORD_RMN_QTY",
            ),
        )
        return {
            "fill_price": float(fill_price),
            "filled_qty": filled_qty,
            "remaining_qty": remaining_qty,
            "order_no": cls._order_no_from_ccld_row(row),
        }

    @staticmethod
    def _order_no_from_ccld_row(row: Dict[str, Any]) -> str:
        if not row or not isinstance(row, dict):
            return ""
        return str(
            row.get("odno")
            or row.get("ODNO")
            or row.get("ord_no")
            or row.get("ORD_NO")
            or ""
        ).strip()

    @classmethod
    def _ccld_row_matches_order(cls, row: Dict[str, Any], order_no: str) -> bool:
        row_order_no = cls._order_no_from_ccld_row(row)
        wanted = str(order_no or "").strip()
        if not row_order_no or not wanted:
            return False
        return row_order_no == wanted or (row_order_no.lstrip("0") or row_order_no) == (
            wanted.lstrip("0") or wanted
        )

    def _inquire_daily_ccld_rows(
        self,
        symbol: str,
        order_no: str,
        ccld_dvsn: str = "01",
    ) -> List[Dict[str, Any]]:
        """
        주식일별주문체결조회 output1 목록.
        ccld_dvsn: 01 체결, 02 미체결 (증권사 스펙 기준).
        """
        if not self._is_configured() or not self.cano:
            return []
        try:
            self._ensure_token()
            tr_id = "VTTC8001R" if self.use_mock else "TTTC8001R"
            today = datetime.now().strftime("%Y%m%d")
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "CCLD_DVSN": ccld_dvsn,
                "PDNO": symbol or "",
                "ORD_NO": (order_no or "").strip(),
                "INQR_DVSN": "00",
            }
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id,
                params=params,
            )
            if not data or data.get("rt_cd") != "0":
                return []
            output = data.get("output1") or data.get("output") or []
            if isinstance(output, dict):
                output = [output] if output else []
            if not isinstance(output, list):
                return []
            return [r for r in output if isinstance(r, dict)]
        except Exception as e:
            logger.debug("일별체결조회 실패: {} — {}", symbol, e)
            return []

    def get_order_execution_after_order(
        self,
        symbol: str,
        order_output: Optional[Dict[str, Any]],
        max_attempts: int = 6,
        delay_seconds: float = 0.35,
    ) -> Optional[Dict[str, Any]]:
        """
        주문 직후 체결 평균가와 체결 수량 조회 (일별체결조회, 짧은 폴링).
        order_output: order-cash 응답의 output. ODNO 없으면 None 반환.
        """
        odno = self._odno_from_order_output(order_output)
        if not odno:
            logger.debug("주문 응답에 ODNO 없음 — 체결가 조회 생략: {}", symbol)
            return None
        def _pick_execution(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not rows:
                return None
            for row in rows:
                if not self._ccld_row_matches_order(row, odno):
                    continue
                execution = self._execution_from_ccld_row(row)
                if execution and execution.get("fill_price", 0) > 0:
                    return execution
            return None

        for attempt in range(max_attempts):
            if attempt:
                time.sleep(delay_seconds)
            execution = _pick_execution(self._inquire_daily_ccld_rows(symbol, odno, ccld_dvsn="01"))
            if execution and execution.get("fill_price", 0) > 0:
                return execution
            execution = _pick_execution(self._inquire_daily_ccld_rows(symbol, "", ccld_dvsn="01"))
            if execution and execution.get("fill_price", 0) > 0:
                return execution
        return None

    def get_filled_avg_price_after_order(
        self,
        symbol: str,
        order_output: Optional[Dict[str, Any]],
        max_attempts: int = 6,
        delay_seconds: float = 0.35,
    ) -> Optional[float]:
        """
        주문 직후 체결 평균가 조회 (일별체결조회, 짧은 폴링).
        기존 호출부 호환용. 신규 live 장부 반영은 체결 수량까지 확인한다.
        """
        execution = self.get_order_execution_after_order(
            symbol,
            order_output,
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )
        if not execution:
            return None
        fill_price = execution.get("fill_price")
        return float(fill_price) if fill_price and fill_price > 0 else None

    # =============================================================
    # 미체결 주문 조회 (주문 중복 방지용)
    # =============================================================

    @staticmethod
    def _symbol_from_order_row(row: Dict[str, Any]) -> str:
        if not row or not isinstance(row, dict):
            return ""
        return str(
            row.get("pdno")
            or row.get("PDNO")
            or row.get("shtn_pdno")
            or row.get("SHTN_PDNO")
            or ""
        ).strip()

    @staticmethod
    def _remaining_qty_from_order_row(row: Dict[str, Any]) -> int:
        if not row or not isinstance(row, dict):
            return 0
        remaining_fields = (
            ("rmn_qty", row.get("rmn_qty")),
            ("RMN_QTY", row.get("RMN_QTY")),
            ("nccs_qty", row.get("nccs_qty")),
            ("NCCS_QTY", row.get("NCCS_QTY")),
        )
        saw_remaining_field = False
        for _, value in remaining_fields:
            if value is None or value == "":
                continue
            saw_remaining_field = True
            try:
                qty = int(float(value))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                return qty
        if saw_remaining_field:
            return 0

        ordered_qty_fields = (
            ("ord_qty", row.get("ord_qty")),
            ("ORD_QTY", row.get("ORD_QTY")),
        )
        for _, value in ordered_qty_fields:
            if value is None or value == "":
                continue
            try:
                qty = int(float(value))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                return qty
        return 0

    def get_unfilled_order_status(self, symbol: str) -> Dict[str, Any]:
        """
        해당 종목 미체결 주문 조회 상태를 반환한다.
        checked=False면 API 실패/응답 이상이라 live 주문은 fail-closed로 막아야 한다.
        """
        target_symbol = str(symbol or "").strip()
        if not self._is_configured() or not self.cano:
            return {
                "checked": False,
                "has_unfilled": False,
                "reason": "kis_not_configured",
                "orders": [],
            }
        try:
            tr_id = "VTTC8001R" if self.use_mock else "TTTC8001R"
            today = datetime.now().strftime("%Y%m%d")
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",  # 전체
                "CCLD_DVSN": "02",        # 미체결
                "PDNO": target_symbol,
                "ORD_NO": "",
                "INQR_DVSN": "00",
            }
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id,
                params=params,
            )
            if not data or data.get("rt_cd") != "0":
                return {
                    "checked": False,
                    "has_unfilled": False,
                    "reason": "kis_unfilled_query_failed",
                    "message": (data or {}).get("msg1") if isinstance(data, dict) else "API 응답 없음",
                    "orders": [],
                }
            output = data.get("output1") or data.get("output") or []
            if isinstance(output, dict):
                output = [output] if output else []
            if not isinstance(output, list):
                return {
                    "checked": False,
                    "has_unfilled": False,
                    "reason": "kis_unfilled_output_invalid",
                    "orders": [],
                }

            orders = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                row_symbol = self._symbol_from_order_row(item)
                if row_symbol != target_symbol:
                    continue
                remaining_qty = self._remaining_qty_from_order_row(item)
                if remaining_qty <= 0:
                    continue
                orders.append({
                    "symbol": row_symbol,
                    "remaining_qty": remaining_qty,
                    "order_price": item.get("ord_unpr") or item.get("ORD_UNPR") or "",
                    "buy_sell": item.get("sll_buy_dvsn_cd") or item.get("SLL_BUY_DVSN_CD") or "",
                    "order_no": item.get("odno") or item.get("ODNO") or item.get("ord_no") or "",
                    "order_time": item.get("ord_tmd") or item.get("ORD_TMD") or "",
                })
            return {
                "checked": True,
                "has_unfilled": bool(orders),
                "reason": "ok",
                "orders": orders,
            }
        except Exception as e:
            logger.warning("미체결 조회 실패 — live 주문은 보류 필요: {} — {}", target_symbol, e)
            return {
                "checked": False,
                "has_unfilled": False,
                "reason": "kis_unfilled_query_exception",
                "error": str(e),
                "orders": [],
            }

    def has_unfilled_orders(self, symbol: str) -> bool:
        """
        해당 종목에 대한 미체결 주문이 있는지 조회.
        호환용 bool API. live 주문 전에는 get_unfilled_order_status()로 조회 성공 여부까지 확인한다.

        Returns:
            True: 미체결 주문이 있거나 조회 실패. False: 조회 성공 + 미체결 없음.
        """
        status = self.get_unfilled_order_status(symbol)
        if not status.get("checked"):
            logger.warning(
                "미체결 조회 상태 불명 — 호환 API는 중복 주문 방지를 위해 미체결 있음으로 간주: {} — {}",
                symbol,
                status.get("reason"),
            )
            return True
        if status.get("has_unfilled"):
            logger.info("종목 {} 미체결 주문 존재 — 중복 주문 방지를 위해 이번 주문을 보류합니다.", symbol)
            return True
        return False

    def get_open_orders_status(self) -> Dict[str, Any]:
        """
        당일 계좌 전체 미체결 주문 조회 상태 (재시작 복구·운영 점검용).
        inquire-daily-ccld + CCLD_DVSN=02. PDNO 비우면 전체(증권사 스펙에 따라 전종목 또는 오류 시 []).

        Returns:
            checked=False면 조회 실패/응답 이상. checked=True면 orders가 정상 조회 결과.
        """
        if not self._is_configured() or not self.cano:
            return {
                "checked": False,
                "reason": "kis_not_configured",
                "orders": [],
            }
        try:
            self._ensure_token()
            tr_id = "VTTC8001R" if self.use_mock else "TTTC8001R"
            today = datetime.now().strftime("%Y%m%d")
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "CCLD_DVSN": "02",
                "PDNO": "",
                "ORD_NO": "",
                "INQR_DVSN": "00",
            }
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id,
                params=params,
            )
            if not data or data.get("rt_cd") != "0":
                return {
                    "checked": False,
                    "reason": "kis_open_orders_query_failed",
                    "message": (data or {}).get("msg1") if isinstance(data, dict) else "API 응답 없음",
                    "orders": [],
                }
            output = data.get("output1") or data.get("output") or []
            if isinstance(output, dict):
                output = [output] if output else []
            if not isinstance(output, list):
                return {
                    "checked": False,
                    "reason": "kis_open_orders_output_invalid",
                    "orders": [],
                }
            normalized = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                sym = self._symbol_from_order_row(item)
                rmn = self._remaining_qty_from_order_row(item)
                if rmn <= 0:
                    continue
                normalized.append({
                    "symbol": sym,
                    "remaining_qty": rmn,
                    "order_price": item.get("ord_unpr") or item.get("ORD_UNPR") or "",
                    "buy_sell": item.get("sll_buy_dvsn_cd") or item.get("SLL_BUY_DVSN_CD") or "",
                    "order_no": item.get("odno") or item.get("ODNO") or item.get("ord_no") or "",
                    "order_time": item.get("ord_tmd") or item.get("ORD_TMD") or "",
                })
            return {
                "checked": True,
                "reason": "ok",
                "orders": normalized,
            }
        except Exception as e:
            logger.warning("미체결 전체 조회 실패: {}", e)
            return {
                "checked": False,
                "reason": "kis_open_orders_query_exception",
                "error": str(e),
                "orders": [],
            }

    def get_open_orders(self) -> list:
        """
        당일 계좌 전체 미체결 주문 조회 (재시작 복구·운영 점검용).
        호환용 list API. 실패 시 []를 반환하므로 복구 로직은 get_open_orders_status()를 우선 사용한다.
        """
        status = self.get_open_orders_status()
        return status.get("orders", []) if status.get("checked") else []

    # =============================================================
    # 잔고 조회
    # =============================================================

    def get_balance(self) -> Optional[Dict[str, Any]]:
        """
        계좌 잔고 조회

        Returns:
            잔고 정보 딕셔너리
            {
                "cash": 예수금,
                "total_value": 총 평가금액,
                "positions": [종목별 보유 현황],
            }
        """
        tr_id = "VTTC8434R" if self.use_mock else "TTTC8434R"

        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            params=params,
        )

        if not data:
            return None
        if not isinstance(data, dict) or data.get("rt_cd") != "0":
            msg = data.get("msg1", data.get("msg_cd", "잔고 조회 실패")) if isinstance(data, dict) else "잔고 조회 실패"
            logger.warning("KIS 잔고 조회 실패: {}", msg)
            return None

        output1 = data.get("output1", [])   # 종목별 보유 현황
        output2 = data.get("output2", [{}]) # 계좌 요약

        positions = []
        for item in output1:
            positions.append({
                "symbol": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "quantity": int(item.get("hldg_qty", 0)),
                "avg_price": float(item.get("pchs_avg_pric", 0)),
                "current_price": float(item.get("prpr", 0)),
                "pnl_rate": float(item.get("evlu_pfls_rt", 0)),
                "pnl_amount": float(item.get("evlu_pfls_amt", 0)),
            })

        summary = output2[0] if output2 else {}
        return {
            "cash": float(summary.get("dnca_tot_amt", 0)),        # 예수금 총액
            "total_value": float(summary.get("tot_evlu_amt", 0)),  # 총 평가금액
            "total_pnl": float(summary.get("evlu_pfls_smtl_amt", 0)),  # 총 평가손익
            "positions": positions,
        }

    def get_approval_key(self) -> str:
        """
        KIS 웹소켓 접속용 approval key 발급.

        KIS 실시간 웹소켓은 app_key 직접 사용이 아니라 접속키 발급 절차가 필요하다.
        """
        if not self._is_configured():
            logger.warning("KIS API 미설정 — approval key 발급 불가")
            return ""

        domain = "모의투자" if self.use_mock else "실전"
        url = f"{self.base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }

        try:
            response = requests.post(url, json=body, timeout=10)
            if not response.ok:
                try:
                    err_body = response.json()
                    err_msg = err_body.get("msg", err_body.get("error_description", str(err_body)))[:200]
                except Exception:
                    err_msg = response.text[:200] if response.text else ""
                logger.error(
                    "KIS approval key 발급 실패 [{}] {} (도메인: {})",
                    response.status_code, err_msg, domain,
                )
                return ""
            response.raise_for_status()
            data = response.json()
            approval_key = data.get("approval_key", "")
            if not approval_key:
                logger.error("KIS approval key 발급 실패: 응답에 approval_key 없음 (도메인: {})", domain)
                return ""
            logger.info(
                "KIS approval key 발급 성공 (도메인: {}, 키: {})",
                domain, self._mask_key(approval_key),
            )
            return approval_key
        except requests.RequestException as e:
            logger.error("KIS approval key 발급 네트워크 오류: {} (도메인: {})", e, domain)
            return ""
        except Exception as e:
            logger.error("KIS approval key 발급 실패: {} (도메인: {})", e, domain)
            return ""

    # =============================================================
    # 해외주식 (미국) — KIS Developers 해외주식 API (시세/주문/잔고)
    # 시세 EXCD·주문 OVRS_EXCG_CD 는 스펙이 다름 (예: NAS ↔ NASD).
    # 참고: koreainvestment/open-trading-api examples_llm/overseas_stock
    # =============================================================

    @staticmethod
    def map_us_market_to_kis_codes(market: str) -> tuple[str, str]:
        """
        사용자 시장 코드 → (해외시세 quotations EXCD, 주문 OVRS_EXCG_CD).

        Args:
            market: NAS / NYS / AMS (또는 NASDAQ, NYSE, AMEX)

        Returns:
            (excd, ovrs_excg_cd)
        """
        m = (market or "NAS").strip().upper()
        if m in ("NAS", "NASDAQ", "NASD"):
            return "NAS", "NASD"
        if m in ("NYS", "NYSE", "NY"):
            return "NYS", "NYSE"
        if m in ("AMS", "AMEX", "AMX"):
            return "AMS", "AMEX"
        logger.warning("미국 시장 코드 '{}' 인식 불가 — NAS/NASD 로 대체", market)
        return "NAS", "NASD"

    def get_overseas_price(self, symbol: str, market: str = "NAS") -> Optional[Dict[str, Any]]:
        """
        해외주식 현재체결가 조회.
        GET /uapi/overseas-price/v1/quotations/price (tr_id HHDFS00000300, 실전·모의 동일)
        """
        excd, _ = self.map_us_market_to_kis_codes(market)
        symb = str(symbol).strip().upper()
        tr_id = "HHDFS00000300"
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": symb,
        }
        data = self._request(
            "GET",
            "/uapi/overseas-price/v1/quotations/price",
            tr_id,
            params=params,
        )
        if not data or data.get("rt_cd") != "0":
            return None
        out = data.get("output")
        if isinstance(out, list) and out:
            out = out[0]
        if not isinstance(out, dict):
            return None
        # 응답 필드명은 증권사 스펙에 따라 다를 수 있음
        def _f(*keys, default=0.0):
            for k in keys:
                v = out.get(k)
                if v is not None and str(v).strip() != "":
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return default

        def _i(*keys, default=0):
            for k in keys:
                v = out.get(k)
                if v is not None and str(v).strip() != "":
                    try:
                        return int(float(v))
                    except (TypeError, ValueError):
                        continue
            return default

        price = _f("last", "ovrs_prpr", "prpr", "stck_prpr")
        return {
            "symbol": symb,
            "market": market,
            "excd": excd,
            "price": price,
            "open": _f("open", "ovrs_nmix_prpr", "stck_oprc"),
            "high": _f("high", "stck_hgpr"),
            "low": _f("low", "stck_lwpr"),
            "volume": _i("tvol", "acml_vol", "vol"),
            "change_rate": _f("rate", "prdy_ctrt"),
            "prev_close": _f("base", "pbase", "stck_sdpr"),
            "raw": out,
        }

    def place_overseas_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        market: str = "NAS",
    ) -> Optional[Dict[str, Any]]:
        """
        해외주식 주문 (미국 NASD/NYSE/AMEX).
        POST /uapi/overseas-stock/v1/trading/order

        Args:
            symbol: 티커 (예 AAPL)
            side: buy | sell
            qty: 주문 수량
            price: 지정가 (USD). 시장가 대체 시 0 → API 스펙상 \"0\" 문자열 전달
            market: NAS / NYS / AMS
        """
        _, ovrs = self.map_us_market_to_kis_codes(market)
        symb = str(symbol).strip().upper()
        sd = str(side).strip().lower()
        if sd not in ("buy", "sell"):
            logger.error("place_overseas_order: side는 buy/sell 만 허용: {}", side)
            return None
        if ovrs not in ("NASD", "NYSE", "AMEX"):
            logger.error("place_overseas_order: 미국 거래소만 지원 (NASD/NYSE/AMEX): {}", ovrs)
            return None

        if sd == "buy":
            tr_id = "VTTT1002U" if self.use_mock else "TTTT1002U"
            sll_type = ""
        else:
            tr_id = "VTTT1006U" if self.use_mock else "TTTT1006U"
            sll_type = "00"

        unpr = f"{float(price):.2f}" if price and float(price) > 0 else "0"

        body = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": ovrs,
            "PDNO": symb,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": unpr,
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "SLL_TYPE": sll_type,
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }

        try:
            data = self._request(
                "POST",
                "/uapi/overseas-stock/v1/trading/order",
                tr_id,
                body=body,
                idempotent=False,  # 주문 제출: 응답 유실 시 재전송 금지(이중 체결 방지)
            )
        except KISOrderResponseUnknown as exc:
            # 응답 유실(체결 여부 불명) — 재전송하지 않고 실패로 처리(이중 체결 방지).
            logger.error("해외주문 응답 유실 — 재전송 금지, 체결 여부 불명: {} {} ({})", symb, sd, exc)
            return None
        if data and data.get("rt_cd") == "0":
            logger.info("해외주문 성공 {} {} {}주 @ {}", sd, symb, qty, unpr)
            out = data.get("output")
            return out if isinstance(out, dict) else {"output": out}
        msg = data.get("msg1", data.get("msg_cd", "오류")) if data else "API 응답 없음"
        logger.error("해외주문 실패 {} {} — {}", symb, sd, msg)
        return None

    def get_overseas_balance(self) -> Optional[Dict[str, Any]]:
        """
        해외주식 체결기준 현재잔고.
        GET /uapi/overseas-stock/v1/trading/inquire-present-balance
        (실전 CTRP6504R / 모의 VTRP6504R)
        """
        tr_id = "VTRP6504R" if self.use_mock else "CTRP6504R"
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD": "840",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }
        # 공식 예시(kis_auth)는 params 전달 — GET 쿼리로 시도, 실패 시 POST body로 재시도 가능
        data = self._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            tr_id,
            params=params,
        )
        if not data or str(data.get("rt_cd", "")) != "0":
            data = self._request(
                "POST",
                "/uapi/overseas-stock/v1/trading/inquire-present-balance",
                tr_id,
                body=params,
            )
        if not data or data.get("rt_cd") != "0":
            return None

        def _rows(key: str) -> list:
            r = data.get(key) or []
            if isinstance(r, dict):
                return [r] if r else []
            return r if isinstance(r, list) else []

        rows1 = _rows("output1")
        rows2 = _rows("output2")
        rows3 = _rows("output3")

        positions = []
        pos_rows = rows2 if rows2 else rows1
        for item in pos_rows:
            if not isinstance(item, dict):
                continue
            try:
                q = int(float(item.get("ovrs_cblc_qty") or item.get("hldg_qty") or item.get("cblc_qty") or 0))
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            positions.append({
                "symbol": str(item.get("ovrs_pdno") or item.get("pdno") or "").strip(),
                "name": str(item.get("ovrs_item_name") or item.get("prdt_name") or ""),
                "quantity": q,
                "avg_price": float(item.get("pchs_avg_pric") or item.get("avg_pur_pric") or 0),
                "current_price": float(item.get("now_pric2") or item.get("prpr") or 0),
                "currency": str(item.get("tr_crcy_cd") or item.get("crcy_cd") or "USD"),
                "exchange": str(item.get("ovrs_excg_cd") or ""),
                "pnl_rate": float(item.get("evlu_pfls_rt") or 0),
                "pnl_amount": float(item.get("evlu_pfls_amt") or 0),
            })

        cash_foreign = 0.0
        for item in rows1:
            if isinstance(item, dict):
                try:
                    cash_foreign = float(item.get("frcr_dncl_amt_1") or item.get("frcr_use_amt") or 0)
                except (TypeError, ValueError):
                    pass
                break

        summary = rows3[0] if rows3 else {}
        return {
            "cash_foreign": cash_foreign,
            "total_value_foreign": float(summary.get("tot_asst_amt") or summary.get("frcr_evlu_tota") or 0),
            "positions": positions,
            "raw_output1": rows1,
            "raw_output2": rows2,
            "raw_output3": rows3,
        }

    def verify_connection(self) -> bool:
        """
        토큰 발급 후 실환경 연결 검증용: 잔고 조회 1회 수행 후 성공/실패 로깅.
        live 모드 진입 시 REST API 도달 가능 여부 확인에 사용.
        """
        domain = "모의투자" if self.use_mock else "실전"
        balance = self.get_balance()
        if balance is not None:
            logger.info(
                "KIS 실환경 연결 검증 성공 (도메인: {}, 예수금: {:.0f})",
                domain, balance.get("cash", 0),
            )
            return True
        logger.warning("KIS 실환경 연결 검증 실패: 잔고 조회 실패 (도메인: {})", domain)
        return False

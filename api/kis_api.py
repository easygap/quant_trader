"""
한국투자증권 (KIS) API 래퍼 모듈
- REST API를 통한 시세 조회, 주문 실행, 잔고 조회
- 토큰 발급 및 자동 갱신
- 모의투자 / 실전 도메인 전환 지원
"""

import time
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import threading

import requests
from loguru import logger

from config.config_loader import Config
from api.circuit_breaker import get_breaker


class KISTokenExpiredError(Exception):
    """KIS API 401 응답(토큰 만료) 시 사용. CircuitBreaker 실패로 누적하지 않음."""


class KISApi:
    """
    한국투자증권 Open API 래퍼

    사용법:
        api = KISApi()
        api.authenticate()
        price = api.get_current_price("005930")
    """

    def __init__(self):
        config = Config.get()
        kis = config.kis_api

        self.app_key = kis.get("app_key", "")
        self.app_secret = kis.get("app_secret", "")
        self.account_no = kis.get("account_no", "")
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

        # API Rate Limiter (Token Bucket)
        # 설정된 MAX_CALLS_PER_SEC에 맞춰 토큰 충전 및 소비 (기본: 4)
        self.max_calls_per_sec = float(kis.get("max_calls_per_sec", 4.0))
        self._tokens = self.max_calls_per_sec
        self._last_refill = time.monotonic()
        self._token_lock = threading.Lock()
        self._auth_lock = threading.Lock()  # 토큰 갱신 동시 호출 방지

        logger.info(
            "KIS API 초기화 완료 (모드: {}, 계좌: {}, RateLimit: {}/sec)",
            "모의투자" if self.use_mock else "실전",
            self.account_no,
            self.max_calls_per_sec
        )

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
            return False
        except Exception as e:
            logger.error("KIS API 토큰 발급 실패: {}", e)
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
        """Token Bucket Rate Limiter 알고리즘: 요청 전 토큰 확보 (동기 컨텍스트용 monotonic)"""
        with self._token_lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill

                # 시간 경과에 따른 토큰 보충
                self._tokens = self._tokens + (elapsed * self.max_calls_per_sec)
                if self._tokens > self.max_calls_per_sec:
                    self._tokens = self.max_calls_per_sec
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens = self._tokens - 1.0
                    return
                else:
                    # 토큰이 부족하면 충전될 때까지 약간 대기
                    sleep_time = (1.0 - self._tokens) / self.max_calls_per_sec
                    time.sleep(max(0.01, sleep_time))

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict = None,
        body: dict = None,
        max_retries: int = None,
    ) -> Dict[str, Any]:
        """
        API 요청 공통 메서드 (에러별 재시도 로직 포함)
        """
        breaker = get_breaker()
        if not breaker.can_request():
            logger.warning("Circuit Breaker 동작 중! API 요청 즉시 차단: {}", path)
            return {}

        if not self._is_configured():
            logger.warning("KIS API 미설정 — 빈 응답 반환")
            return {}

        url = f"{self.base_url}{path}"
        max_retries = max_retries if max_retries is not None else int(Config.get().kis_api.get("max_retry", 3))

        for attempt in range(1, max_retries + 1):
            headers = self._get_headers(tr_id)
            self._wait_for_token()

            try:
                if method.upper() == "GET":
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                else:
                    response = requests.post(url, headers=headers, json=body, timeout=10)

                # HTTP 에러 분기 처리
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    logger.warning(
                        "[429 Too Many Requests] {}초 대기 후 재시도 ({}/{}) - 경로: {}",
                        retry_after, attempt, max_retries, path
                    )
                    time.sleep(retry_after)
                    continue

                if response.status_code in (500, 502, 503, 504):
                    breaker.on_failure()  # 서킷 누적
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        "[{}] 서버 오류, {}초 후 재시도 ({}/{}) - 경로: {}",
                        response.status_code, wait, attempt, max_retries, path
                    )
                    time.sleep(wait)
                    continue

                if response.status_code == 401:
                    raise KISTokenExpiredError("KIS API 401 Unauthorized — 토큰 만료")

                if response.status_code in (400, 403):
                    logger.error("[{}] 복구 불가 오류 즉시 중단 - 경로: {}", response.status_code, path)
                    return {}  # 즉시 중단 (재시도 금지)

                response.raise_for_status()
                breaker.on_success()  # 성공 시 서킷 초기화
                return response.json()

            except KISTokenExpiredError:
                # 토큰 만료는 CircuitBreaker 실패로 누적하지 않음
                logger.error("[401] 토큰 만료. 갱신 후 재시도 ({}/{})", attempt, max_retries)
                self.authenticate()
                if attempt < max_retries:
                    continue
                return {}

            except requests.exceptions.Timeout:
                breaker.on_failure()  # 서킷 누적
                wait = 2 ** (attempt - 1)
                logger.warning("요청 타임아웃, {}초 후 재시도 ({}/{}) - 경로: {}", wait, attempt, max_retries, path)
                time.sleep(wait)
            except requests.exceptions.RequestException as e:
                breaker.on_failure()  # 서킷 누적
                logger.error("요청 실패: {} - {}", path, e)
                # Client Error 계열(4xx 중 처리안된것)이면 중단할 수 있으나 기본적으로 로그만 찍고 재시도
                time.sleep(1)

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

        if not data or "output" not in data:
            return None

        output = data["output"]
        return {
            "symbol": symbol,
            "price": float(output.get("stck_prpr", 0)),       # 현재가
            "open": float(output.get("stck_oprc", 0)),         # 시가
            "high": float(output.get("stck_hgpr", 0)),         # 고가
            "low": float(output.get("stck_lwpr", 0)),          # 저가
            "volume": int(output.get("acml_vol", 0)),          # 누적 거래량
            "change_rate": float(output.get("prdy_ctrt", 0)),  # 전일 대비 등락률
            "prev_close": float(output.get("stck_sdpr", 0)),   # 전일 종가
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
        )

        if data and data.get("rt_cd") == "0":
            logger.info("매도 주문 성공: {} {}주 @ {:,}원", symbol, quantity, price)
            return data.get("output", {})
        else:
            msg = data.get("msg1", "알 수 없는 오류") if data else "API 응답 없음"
            logger.error("매도 주문 실패: {} - {}", symbol, msg)
            return None

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

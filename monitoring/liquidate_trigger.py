"""
긴급 전체 청산 HTTP 트리거.
- 수동 개입·디스코드 봇 등에서 원격으로 전 종목 매도를 걸 수 있도록 HTTP 서버 제공.
- 환경변수 LIQUIDATE_TRIGGER_TOKEN 필수(미설정 시 서버 미가동). LIQUIDATE_TRIGGER_PORT(기본 8765).
- live 설정에서 실제 청산을 허용하려면 ENABLE_LIVE_TRADING=true 와
  LIQUIDATE_TRIGGER_CONFIRM_LIVE=true 를 둘 다 설정해야 한다.
- 청산 실행은 POST만 허용한다. 인증 토큰은 기본적으로 X-Token 또는
  Authorization: Bearer 헤더로만 받는다.

사용:
    set LIQUIDATE_TRIGGER_TOKEN=your_secret
    set LIQUIDATE_TRIGGER_CONFIRM_LIVE=true
    python -m monitoring.liquidate_trigger

    POST http://localhost:8765/liquidate
    Header: X-Token: your_secret
"""

import hmac
import json
import os
import sys
from argparse import Namespace
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import parse_qs, urlparse

# 프로젝트 루트
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_token_from_request(handler: BaseHTTPRequestHandler) -> Optional[str]:
    """Header X-Token 또는 Authorization: Bearer 토큰 추출."""
    token = handler.headers.get("X-Token")
    if token:
        return token.strip()
    auth = handler.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if bearer:
            return bearer
    if not _env_truthy("LIQUIDATE_TRIGGER_ALLOW_QUERY_TOKEN"):
        return None
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    return (qs.get("token") or [None])[0]


def _env_truthy(name: str) -> bool:
    """환경변수 truthy 값 해석."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _run_liquidate() -> tuple[bool, str]:
    """긴급 청산 실행. (success, message) 반환."""
    from database.models import init_database
    from monitoring.logger import setup_logger

    setup_logger()
    init_database()

    from main import run_emergency_liquidate

    args = Namespace(confirm_live=_env_truthy("LIQUIDATE_TRIGGER_CONFIRM_LIVE"))
    try:
        result = run_emergency_liquidate(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return (
            False,
            "긴급 청산 실행이 차단되었습니다. "
            f"종료 코드={code}. live 설정이면 ENABLE_LIVE_TRADING=true 및 "
            "LIQUIDATE_TRIGGER_CONFIRM_LIVE=true 설정을 확인하세요.",
        )
    if isinstance(result, dict):
        attempted = int(result.get("attempted") or 0)
        succeeded = int(result.get("succeeded") or 0)
        failed = int(result.get("failed") or 0)
        if failed:
            return (
                False,
                f"전 종목 청산 요청 처리 중 실패 {failed}건이 발생했습니다. "
                f"대상={attempted}, 성공={succeeded}, 실패={failed}. 로그를 확인하세요.",
            )
        return True, f"전 종목 청산 요청 처리 완료. 대상={attempted}, 성공={succeeded}, 실패={failed}."
    return True, "전 종목 청산 요청 처리 완료."


class LiquidateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") != "/liquidate":
            self._send(404, {"ok": False, "error": "Not Found"})
            return
        self._send(
            405,
            {"ok": False, "error": "Method Not Allowed. Use POST /liquidate."},
            headers={"Allow": "POST"},
        )

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") != "/liquidate":
            self._send(404, {"ok": False, "error": "Not Found"})
            return
        self._handle_liquidate()

    def _handle_liquidate(self):
        token = os.environ.get("LIQUIDATE_TRIGGER_TOKEN", "").strip()
        if not token:
            self._send(503, {"ok": False, "error": "LIQUIDATE_TRIGGER_TOKEN not configured"})
            return
        provided = _get_token_from_request(self)
        if not provided or not hmac.compare_digest(provided, token):
            self._send(403, {"ok": False, "error": "Invalid or missing token"})
            return
        try:
            ok, msg = _run_liquidate()
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

    def _send(self, code: int, body: dict, headers: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 필요 시 로깅


def main():
    token = os.environ.get("LIQUIDATE_TRIGGER_TOKEN", "").strip()
    if not token:
        print("LIQUIDATE_TRIGGER_TOKEN 환경변수가 없습니다. 서버를 시작하지 않습니다.")
        sys.exit(1)
    port = int(os.environ.get("LIQUIDATE_TRIGGER_PORT", "8765"))
    server = HTTPServer(("", port), LiquidateHandler)
    print(f"긴급 청산 HTTP 트리거: POST http://0.0.0.0:{port}/liquidate (X-Token)")
    server.serve_forever()


if __name__ == "__main__":
    main()

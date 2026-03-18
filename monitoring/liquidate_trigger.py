"""
긴급 전체 청산 HTTP 트리거.
- 수동 개입·디스코드 봇 등에서 원격으로 전 종목 매도를 걸 수 있도록 HTTP 서버 제공.
- 환경변수 LIQUIDATE_TRIGGER_TOKEN 필수(미설정 시 서버 미가동). LIQUIDATE_TRIGGER_PORT(기본 8765).

사용:
    set LIQUIDATE_TRIGGER_TOKEN=your_secret
    python -m monitoring.liquidate_trigger

    POST http://localhost:8765/liquidate
    Header: X-Token: your_secret
    또는 Query: ?token=your_secret
"""

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
    """Header X-Token 또는 query token 추출."""
    token = handler.headers.get("X-Token") or handler.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        return token.strip()
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    return (qs.get("token") or [None])[0]


def _run_liquidate() -> tuple[bool, str]:
    """긴급 청산 실행. (success, message) 반환."""
    from database.models import init_database
    from monitoring.logger import setup_logger

    setup_logger()
    init_database()

    from main import run_emergency_liquidate

    run_emergency_liquidate(Namespace())
    return True, "전 종목 청산 요청 처리 완료."


class LiquidateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") != "/liquidate":
            self._send(404, {"ok": False, "error": "Not Found"})
            return
        self._handle_liquidate()

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
        if not provided or provided != token:
            self._send(403, {"ok": False, "error": "Invalid or missing token"})
            return
        try:
            ok, msg = _run_liquidate()
            self._send(200, {"ok": ok, "message": msg})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

    def _send(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
    print(f"긴급 청산 HTTP 트리거: http://0.0.0.0:{port}/liquidate (X-Token 또는 ?token=)")
    server.serve_forever()


if __name__ == "__main__":
    main()

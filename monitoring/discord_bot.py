"""
디스코드 웹훅 알림 모듈
- 매매 알림, 일일 리포트를 디스코드 채널에 발송
- 웹훅 URL만 설정하면 별도 봇 프로세스 없이 즉시 사용 가능
"""

import json
from loguru import logger

from config.config_loader import Config

try:
    import requests as req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class DiscordBot:
    """
    디스코드 웹훅 알림

    설정: settings.yaml의 discord 섹션에 webhook_url 설정

    사용법:
        bot = DiscordBot()
        bot.send_message("테스트 메시지")
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        dc = self.config.discord

        self.enabled = dc.get("enabled", False)
        self.webhook_url = dc.get("webhook_url", "")
        self.username = dc.get("username", "퀀트 트레이더")
        self.avatar_url = dc.get("avatar_url", "")

        if self.enabled and self.webhook_url:
            logger.info("DiscordBot 활성화됨 (웹훅 연동)")
        else:
            logger.info("DiscordBot 비활성 — 콘솔 출력으로 대체")

    def send_message(self, text: str) -> bool:
        """
        디스코드 메시지 발송

        Args:
            text: 발송할 메시지

        Returns:
            성공 여부
        """
        if not self.enabled or not self.webhook_url or not HAS_REQUESTS:
            logger.info("[알림] {}", text)
            return True

        try:
            payload = {
                "content": text,
                "username": self.username,
            }
            if self.avatar_url:
                payload["avatar_url"] = self.avatar_url

            response = req.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            return response.status_code in (200, 204)
        except Exception as e:
            logger.error("[ALERT_FAILED] 디스코드 발송 실패: {}", e)
            return False

    def send_embed(self, title: str, description: str, color: int = 0x4F9EF8, fields: list = None) -> bool:  # noqa
        """
        디스코드 Embed 메시지 발송 (리치 포맷)

        Args:
            title: 임베드 제목
            description: 임베드 설명
            color: 임베드 색상 (hex)
            fields: [{"name": "필드명", "value": "값", "inline": True/False}]

        Returns:
            성공 여부
        """
        if not self.enabled or not self.webhook_url or not HAS_REQUESTS:
            logger.info("[알림] {} — {}", title, description)
            return True

        try:
            embed = {
                "title": title,
                "description": description,
                "color": color,
            }

            if fields:
                embed["fields"] = fields

            payload = {
                "username": self.username,
                "embeds": [embed],
            }
            if self.avatar_url:
                payload["avatar_url"] = self.avatar_url

            response = req.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            return response.status_code in (200, 204)
        except Exception as e:
            logger.error("디스코드 Embed 발송 실패: {}", e)
            return False

    def send_trade_alert(self, trade_info: dict):
        """매매 알림 발송"""
        action = trade_info.get("action", "")
        symbol = trade_info.get("symbol", "")
        price = trade_info.get("price", 0)
        quantity = trade_info.get("quantity", 0)
        pnl = trade_info.get("pnl", 0)
        pnl_rate = trade_info.get("pnl_rate", 0)

        is_buy = action == "BUY"
        color = 0x27AE60 if is_buy else 0xE74C3C  # 녹색 / 빨간색

        fields = [
            {"name": "종목", "value": symbol, "inline": True},
            {"name": "가격", "value": f"{price:,.0f}원", "inline": True},
            {"name": "수량", "value": f"{quantity}주", "inline": True},
            {"name": "금액", "value": f"{price * quantity:,.0f}원", "inline": True},
        ]

        if action != "BUY":
            emoji = "📈" if pnl >= 0 else "📉"
            fields.append({
                "name": f"{emoji} 수익",
                "value": f"{pnl:,.0f}원 ({pnl_rate:.2f}%)",
                "inline": True,
            })

        title = f"{'🟢 매수' if is_buy else '🔴 매도'} — {action}"
        self.send_embed(title, "", color=color, fields=fields)

    def send_daily_report(self, report: dict):
        """일일 리포트 발송"""
        fields = [
            {"name": "💰 총 평가금", "value": f"{report.get('total_value', 0):,.0f}원", "inline": True},
            {"name": "💵 현금", "value": f"{report.get('cash', 0):,.0f}원", "inline": True},
            {"name": "📈 일일 수익률", "value": f"{report.get('daily_return', 0):.2f}%", "inline": True},
            {"name": "📊 누적 수익률", "value": f"{report.get('cumulative_return', 0):.2f}%", "inline": True},
            {"name": "📉 MDD", "value": f"{report.get('mdd', 0):.2f}%", "inline": True},
            {"name": "📋 보유 종목", "value": f"{report.get('position_count', 0)}개", "inline": True},
            {"name": "🔄 당일 매매", "value": f"{report.get('total_trades', 0)}건", "inline": True},
        ]

        diag = report.get("strategy_diagnosis")
        if diag:
            if isinstance(diag, (list, tuple)):
                diag_text = "\n".join(str(x) for x in diag)
            else:
                diag_text = str(diag)
            diag_text = diag_text.strip()
            if len(diag_text) > 1000:
                diag_text = diag_text[:997] + "..."
            fields.append({
                "name": "📋 전략 진단 (장마감)",
                "value": diag_text or "—",
                "inline": False,
            })

        cum_return = report.get("cumulative_return", 0)
        color = 0x27AE60 if cum_return >= 0 else 0xE74C3C

        self.send_embed("📊 일일 리포트", "", color=color, fields=fields)

    def send_signal_alert(self, symbol: str, signal_info: dict):
        """매매 신호 감지 알림"""
        signal = signal_info.get("signal", "HOLD")
        score = signal_info.get("score", 0)
        close = signal_info.get("close", 0)
        details = signal_info.get("details", {})

        if signal == "HOLD":
            return

        is_buy = signal == "BUY"
        color = 0x27AE60 if is_buy else 0xE74C3C

        fields = [
            {"name": "종목", "value": symbol, "inline": True},
            {"name": "종가", "value": f"{close:,.0f}원", "inline": True},
            {"name": "점수", "value": f"{score}", "inline": True},
        ]

        for key, val in details.items():
            fields.append({"name": key, "value": str(val), "inline": True})

        emoji = "🟢" if is_buy else "🔴"
        self.send_embed(f"{emoji} {signal} 신호 감지", "", color=color, fields=fields)

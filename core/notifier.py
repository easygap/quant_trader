"""
통합 알림 모듈 (이중화 처리)

Fallback 우선순위:
  1차: 디스코드(DiscordBot) 웹훅
  2차: 텔레그램 Bot API
  3차: 이메일 (SMTP)

critical=True 이벤트(블랙스완, 서킷브레이커 등)는 가용한 모든 채널로 동시 발송.
일반 이벤트는 1차 실패 시 2차→3차 순으로 시도.

사용법:
    notifier = Notifier(config)       # Config 기반
    notifier.send_message("일반 알림")
    notifier.send_message("긴급!", critical=True)

DiscordBot과 동일한 인터페이스(send_trade_alert, send_daily_report, send_signal_alert,
send_embed)를 제공하므로, scheduler 등에서 drop-in 교체 가능.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from loguru import logger

from config.config_loader import Config
from monitoring.discord_bot import DiscordBot

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class Notifier:
    """
    통합 알림 시스템 — 디스코드 + 텔레그램 + 이메일 이중화.

    - 일반 알림: 디스코드 전송 → 실패 시 텔레그램 → 실패 시 이메일
    - 치명적 알림(critical=True): 가용한 모든 채널에 동시 발송
    - 알림 실패 누적 시 시스템 점검 알림
    """

    _alert_failure_count = 0
    _alert_failure_threshold = 5
    _alert_health_notified = False

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.discord = DiscordBot(self.config)

        # 텔레그램 설정 (settings.yaml → telegram 섹션 또는 환경변수)
        tg = self.config._settings.get("telegram", {})
        self.tg_enabled = tg.get("enabled", False)
        self.tg_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
        self.tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", str(tg.get("chat_id", "")))

        if self.tg_enabled and self.tg_bot_token and self.tg_chat_id:
            logger.info("Notifier: 텔레그램 fallback 활성화")
        else:
            logger.debug("Notifier: 텔레그램 미설정 — 비활성")

        # SMTP 설정 (환경변수)
        self.smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.alert_email = os.environ.get("ALERT_EMAIL_TO", self.smtp_user)

        if self.smtp_user and self.smtp_password:
            logger.info("Notifier: 이메일 fallback 활성화 (→ {})", self.alert_email)
        else:
            logger.debug("Notifier: 이메일(SMTP) 미설정 — 비활성")

    # ------------------------------------------------------------------
    # 텔레그램
    # ------------------------------------------------------------------
    def _send_telegram(self, text: str) -> bool:
        if not self.tg_enabled or not self.tg_bot_token or not self.tg_chat_id:
            return False
        if not _HAS_REQUESTS:
            logger.debug("requests 미설치 — 텔레그램 발송 스킵")
            return False
        try:
            url = f"https://api.telegram.org/bot{self.tg_bot_token}/sendMessage"
            resp = _requests.post(
                url,
                json={"chat_id": self.tg_chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("📨 텔레그램 발송 성공")
                return True
            logger.warning("텔레그램 발송 실패: HTTP {} — {}", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            logger.error("[ALERT_FAILED] 텔레그램 발송 예외: {}", e)
            return False

    # ------------------------------------------------------------------
    # 이메일
    # ------------------------------------------------------------------
    def _send_email(self, subject: str, body: str) -> bool:
        if not self.smtp_user or not self.smtp_password:
            return False
        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user
            msg["To"] = self.alert_email
            msg["Subject"] = f"[퀀트 트레이더] {subject}"
            msg.attach(MIMEText(body, "plain"))

            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port)
                server.starttls()

            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)
            server.quit()
            logger.info("📧 이메일 발송 성공: {}", subject)
            return True
        except Exception as e:
            logger.error("[ALERT_FAILED] 이메일 발송 실패: {}", e)
            return False

    # ------------------------------------------------------------------
    # fallback 체인
    # ------------------------------------------------------------------
    def _record_alert_failure(self):
        Notifier._alert_failure_count += 1
        if (
            Notifier._alert_failure_count >= Notifier._alert_failure_threshold
            and not Notifier._alert_health_notified
        ):
            Notifier._alert_health_notified = True
            msg = (
                f"[ALERT_FAILED] 알림 실패 {Notifier._alert_failure_count}회 누적. "
                "디스코드·텔레그램·이메일(SMTP) 설정을 점검하세요."
            )
            logger.warning(msg)
            self._send_email(subject="알림 경로 점검 필요", body=msg)

    def _fallback_text(self, text: str, subject: str = "알림") -> bool:
        """텔레그램 → 이메일 순서로 fallback 시도. 하나라도 성공하면 True."""
        if self._send_telegram(text):
            return True
        if self._send_email(subject=subject, body=text):
            return True
        self._record_alert_failure()
        return False

    def _broadcast_text(self, text: str, subject: str = "긴급 알림"):
        """가용한 모든 채널에 동시 발송 (critical 이벤트용)."""
        self._send_telegram(text)
        self._send_email(subject=subject, body=text)

    # ------------------------------------------------------------------
    # 공개 API — DiscordBot과 동일 인터페이스
    # ------------------------------------------------------------------
    def send_message(self, text: str, critical: bool = False):
        """
        메시지 전송.
        - critical=False: 디스코드 → (실패 시) 텔레그램 → 이메일 fallback
        - critical=True: 디스코드 + 텔레그램 + 이메일 모두 동시 발송
        """
        try:
            discord_ok = self.discord.send_message(text)
            if critical:
                self._broadcast_text(text, subject="긴급 알림")
            elif not discord_ok:
                self._fallback_text(text)
        except Exception as e:
            logger.error("[ALERT_FAILED] 알림 전송 실패: {}", e)
            self._fallback_text(text)

    def send_embed(
        self, title: str, description: str,
        color: int = 0x4F9EF8, fields: list = None,
        critical: bool = False,
    ):
        """Embed(리치) 메시지. 텔레그램·이메일은 평문 변환."""
        try:
            discord_ok = self.discord.send_embed(title, description, color, fields)
            plain = self._embed_to_plain(title, description, fields)
            if critical:
                self._broadcast_text(plain, subject=title)
            elif not discord_ok:
                self._fallback_text(plain, subject=title)
        except Exception as e:
            logger.error("[ALERT_FAILED] 임베드 알림 전송 실패: {}", e)
            plain = self._embed_to_plain(title, description, fields)
            self._fallback_text(plain, subject=title)

    def send_trade_alert(self, trade_info: dict):
        """매매 알림. 큰 손실(-5% 이하 손절)은 fallback 동시 발송."""
        self.discord.send_trade_alert(trade_info)

        action = trade_info.get("action", "")
        pnl_rate = trade_info.get("pnl_rate", 0)
        if action != "BUY" and pnl_rate <= -5.0:
            symbol = trade_info.get("symbol", "")
            price = trade_info.get("price", 0)
            msg = f"[매도/손절] {symbol} @ {price:,.0f}원 | 손익률 {pnl_rate:.2f}%"
            self._broadcast_text(msg, subject="매도 손절 주의")

    def send_daily_report(self, report: dict):
        """일일 리포트."""
        self.discord.send_daily_report(report)

    def send_signal_alert(self, symbol: str, signal_info: dict):
        """신호 감지 알림."""
        self.discord.send_signal_alert(symbol, signal_info)

    # ------------------------------------------------------------------
    # 유틸
    # ------------------------------------------------------------------
    @staticmethod
    def _embed_to_plain(title: str, description: str, fields: list = None) -> str:
        lines = [title]
        if description:
            lines.append(description)
        if fields:
            for f in fields:
                lines.append(f"- {f.get('name', '')}: {f.get('value', '')}")
        return "\n".join(lines)

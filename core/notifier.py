"""
통합 알림 모듈 (이중화 처리)
- 1차: Дискорд(DiscordBot) 발송
- 2차(Fallback): 이메일 (SMTP) 전송 (디스코드 전송 실패 또는 치명적 이벤트 시 동시 발송)
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger

from monitoring.discord_bot import DiscordBot


class Notifier:
    """
    통합 알림 시스템
    디스코드 알림을 기본으로 하되, 실패 시 이메일로 Fallback.
    치명적인 알림(블랙스완, 서킷브레이커 등)은 양쪽 모두 발송.
    """

    def __init__(self):
        self.discord = DiscordBot()
        
        # SMTP Fallback 설정
        self.smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT", 465))
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.alert_email = os.environ.get("ALERT_EMAIL_BCC", self.smtp_user)

    def _send_email(self, subject: str, body: str) -> bool:
        """이메일 발송 내부 메서드"""
        if not self.smtp_user or not self.smtp_password:
            logger.debug("SMTP 설정 미비 — 이메일 발송 스킵")
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user
            msg["To"] = self.alert_email
            msg["Subject"] = f"[퀀트 트레이더 알림] {subject}"
            
            msg.attach(MIMEText(body, "plain"))

            # SSL/TLS 연결
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port)
                server.starttls()
                
            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)
            server.quit()
            
            logger.info("📧 Fallback 이메일 발송 성공: {}", subject)
            return True
        except Exception as e:
            logger.error("이메일 발송 실패: {}", e)
            return False

    def send_message(self, text: str, critical: bool = False):
        """
        일반 메시지 전송
        Args:
            text: 내용
            critical: 치명적 오류/이벤트 여부 (True면 이메일 무조건 동시 발송)
        """
        success = self.discord.send_message(text)
        
        # 디스코드 실패 시 이메일 폴백, 또는 치명적 메시지일 경우 무조건 이메일
        if not success or critical:
            self._send_email(subject="긴급 알림", body=text)

    def send_embed(self, title: str, description: str, color: int = 0x4F9EF8, fields: list = None, critical: bool = False):
        """
        임베드/리치 메시지 전송 (포맷 변경 후 디스코드 / 이메일 발송)
        """
        success = self.discord.send_embed(title, description, color, fields)
        
        if not success or critical:
            # Embed 필드를 Plain Text로 변환
            body = f"{title}\n{description}\n"
            if fields:
                for f in fields:
                    body += f"- {f.get('name')}: {f.get('value')}\n"
            self._send_email(subject=title, body=body)

    def send_trade_alert(self, trade_info: dict):
        """매매 알림 발송 - 디스코드 래핑"""
        # 디스코드 내부에서 자신의 send_embed를 호출하므로, Notifier 차원에서 직접 파싱하여 2중 처리
        action = trade_info.get("action", "")
        symbol = trade_info.get("symbol", "")
        price = trade_info.get("price", 0)
        pnl_rate = trade_info.get("pnl_rate", 0)

        # 1차 디스코드
        self.discord.send_trade_alert(trade_info)

        # 매매 알림은 손실이 클 경우(예: -5% 이하 손절) 이메일 폴백 발송
        if action == "SELL" and pnl_rate <= -5.0:
            msg = f"{action} 기회: {symbol} @ {price:,.0f}원 | 손익률: {pnl_rate:.2f}%"
            self._send_email(subject="[매도/손절] 주의 알림", body=msg)

    def send_daily_report(self, report: dict):
        """일일 리포트 발송"""
        # 리포트는 기본 디스코드 발송
        self.discord.send_daily_report(report)
        
    def send_signal_alert(self, symbol: str, signal_info: dict):
        """신호 감지 알림"""
        self.discord.send_signal_alert(symbol, signal_info)

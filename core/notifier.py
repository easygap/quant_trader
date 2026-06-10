"""
통합 알림 모듈 (디스코드 + 이메일)

Fallback:
  일반: 디스코드 → 실패 시 이메일(SMTP)
  critical=True: 디스코드 + 이메일 동시 발송

사용법:
    notifier = Notifier(config)
    notifier.send_message("일반 알림")
    notifier.send_message("긴급!", critical=True)

DiscordBot과 동일한 인터페이스(send_trade_alert, send_daily_report, send_signal_alert,
send_embed)를 제공한다.
"""

import html
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from loguru import logger

from config.config_loader import Config
from monitoring.discord_bot import DiscordBot


class Notifier:
    """
    통합 알림 — 디스코드 → 이메일(폴백 또는 critical 시 병행).

    - 일반: 디스코드 시도 후 실패 시 이메일
    - critical: 디스코드 + 이메일 동시
    - 채널별 실패 카운트(discord_fail_count, email_fail_count)가 각각 5회 이상이면 logger.critical 1회
    """

    _discord_fail_count = 0
    _email_fail_count = 0
    _failure_threshold = 5
    _dual_critical_logged = False

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.discord = DiscordBot(self.config)

        em = (self.config._settings.get("email") or {}) if getattr(self.config, "_settings", None) else {}
        self._email_enabled = em.get("enabled", True)
        self._smtp_server_def = em.get("smtp_server", "") or "smtp.gmail.com"
        self._smtp_port_def = int(em.get("smtp_port", 587))
        self._smtp_user_def = em.get("smtp_user", "")
        self._alert_to_def = em.get("alert_to", "")

        logger.debug(
            "Notifier 초기화 (이메일 enabled={}, smtp_server={})",
            self._email_enabled,
            self._smtp_server_def,
        )

    # ------------------------------------------------------------------
    # 실패 카운트
    # ------------------------------------------------------------------
    @classmethod
    def _maybe_dual_critical(cls) -> None:
        if cls._discord_fail_count < cls._failure_threshold or cls._email_fail_count < cls._failure_threshold:
            cls._dual_critical_logged = False
            return
        if cls._dual_critical_logged:
            return
        cls._dual_critical_logged = True
        logger.critical(
            "[ALERT_HEALTH] 디스코드 실패 {}회·이메일 실패 {}회 누적 — 웹훅·SMTP(환경변수) 설정을 점검하세요.",
            cls._discord_fail_count,
            cls._email_fail_count,
        )

    def _mark_discord_ok(self) -> None:
        Notifier._discord_fail_count = 0
        self._maybe_dual_critical()

    def _mark_discord_fail(self) -> None:
        Notifier._discord_fail_count += 1
        self._maybe_dual_critical()

    def _mark_email_ok(self) -> None:
        Notifier._email_fail_count = 0
        self._maybe_dual_critical()

    def _mark_email_fail(self) -> None:
        Notifier._email_fail_count += 1
        self._maybe_dual_critical()

    # ------------------------------------------------------------------
    # 이메일 (HTML 본문, 테이블: 종목·신호·가격·시간)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_email_html(body_text: str, table_rows: Optional[List[Dict[str, Any]]] = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = list(table_rows) if table_rows else []
        if not rows:
            rows = [{"symbol": "—", "signal": "—", "price": "—", "time": now}]
        thead = (
            "<thead><tr>"
            "<th>종목</th><th>신호</th><th>가격</th><th>시간</th>"
            "</tr></thead>"
        )
        tbody_parts = ["<tbody>"]
        for r in rows:
            sym = html.escape(str(r.get("symbol", "—")))
            sig = html.escape(str(r.get("signal", "—")))
            prc = html.escape(str(r.get("price", "—")))
            tim = html.escape(str(r.get("time", now)))
            tbody_parts.append(f"<tr><td>{sym}</td><td>{sig}</td><td>{prc}</td><td>{tim}</td></tr>")
        tbody_parts.append("</tbody>")
        table = (
            '<table border="1" cellpadding="8" cellspacing="0" '
            'style="border-collapse:collapse;font-family:sans-serif;font-size:14px">'
            f"{thead}{''.join(tbody_parts)}</table>"
        )
        extra = ""
        if body_text and body_text.strip():
            extra = f'<p style="margin-top:16px;white-space:pre-wrap">{html.escape(body_text)}</p>'
        return f"<html><body>{table}{extra}</body></html>"

    def _send_email(
        self,
        title: str,
        alert_level: str,
        body_text: str = "",
        table_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        환경변수: SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO
        제목: [Quant Trader] {알림 레벨} - {제목}
        465 → SSL, 그 외(예: 587) → STARTTLS. 타임아웃 10초.
        """
        if not self._email_enabled:
            return False

        server = os.environ.get("SMTP_SERVER", self._smtp_server_def).strip()
        port = int(os.environ.get("SMTP_PORT", str(self._smtp_port_def)))
        user = os.environ.get("SMTP_USER", self._smtp_user_def).strip()
        password = os.environ.get("SMTP_PASSWORD", "").strip()
        alert_to = os.environ.get("ALERT_EMAIL_TO", self._alert_to_def or user).strip()

        if not user or not password or not alert_to:
            logger.debug("이메일(SMTP) 미설정 — USER/PASSWORD/ALERT_EMAIL_TO 확인")
            return False

        subject = f"[Quant Trader] {alert_level} - {title}"
        html_body = self._build_email_html(body_text, table_rows)

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = user
            msg["To"] = alert_to
            msg["Subject"] = subject
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            if port == 465:
                with smtplib.SMTP_SSL(server, port, timeout=10) as smtp:
                    smtp.login(user, password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(server, port, timeout=10) as smtp:
                    smtp.starttls()
                    smtp.login(user, password)
                    smtp.send_message(msg)

            logger.info("📧 이메일 발송 성공: {}", subject)
            return True
        except Exception as e:
            logger.error("[ALERT_FAILED] 이메일 발송 실패: {}", e)
            return False

    def _send_email_tracked(
        self,
        title: str,
        alert_level: str,
        body_text: str = "",
        table_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        ok = self._send_email(title, alert_level, body_text, table_rows)
        if ok:
            self._mark_email_ok()
        else:
            self._mark_email_fail()
        return ok

    # ------------------------------------------------------------------
    # 디스코드 결과 추적
    # ------------------------------------------------------------------
    def _discord_deliverable(self) -> bool:
        """디스코드가 실제 채널로 전달 가능한 상태인지.

        DiscordBot은 비활성(웹훅 미설정) 시 콘솔에 출력하고 True를 반환한다 —
        그 True를 '발송 성공'으로 믿으면 이메일 폴백이 영영 트리거되지 않아,
        웹훅 미설정 환경에서 일반 알림이 콘솔에만 남는다(무인 운영에서는 아무도
        못 본다). 콘솔 폴백은 채널 전달이 아니므로 구분한다.
        """
        return bool(
            getattr(self.discord, "enabled", False)
            and getattr(self.discord, "webhook_url", "")
        )

    def _discord_send_message(self, text: str) -> bool:
        if not self._discord_deliverable():
            # 콘솔 기록은 유지하되 '전달 안 됨'으로 취급해 이메일 폴백을 트리거한다.
            # 설정상 비활성은 장애가 아니므로 실패 카운트는 올리지 않는다(경보 오탐 방지).
            try:
                self.discord.send_message(text)
            except Exception:
                pass
            return False
        try:
            ok = self.discord.send_message(text)
            if ok:
                self._mark_discord_ok()
            else:
                self._mark_discord_fail()
            return ok
        except Exception as e:
            logger.error("[ALERT_FAILED] 디스코드 발송 예외: {}", e)
            self._mark_discord_fail()
            return False

    def _discord_send_embed(
        self, title: str, description: str, color: int = 0x4F9EF8, fields: list = None,
    ) -> bool:
        if not self._discord_deliverable():
            try:
                self.discord.send_embed(title, description, color, fields)
            except Exception:
                pass
            return False
        try:
            ok = self.discord.send_embed(title, description, color, fields)
            if ok:
                self._mark_discord_ok()
            else:
                self._mark_discord_fail()
            return ok
        except Exception as e:
            logger.error("[ALERT_FAILED] 디스코드 Embed 예외: {}", e)
            self._mark_discord_fail()
            return False

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    def send_message(self, text: str, critical: bool = False) -> None:
        level = "CRITICAL" if critical else "INFO"
        discord_ok = self._discord_send_message(text)
        if critical:
            self._send_email_tracked("알림", level, body_text=text)
        elif not discord_ok:
            self._send_email_tracked("알림", "WARNING", body_text=text)

    send = send_message

    def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x4F9EF8,
        fields: list = None,
        critical: bool = False,
    ) -> None:
        level = "CRITICAL" if critical else "INFO"
        plain = self._embed_to_plain(title, description, fields)
        rows = self._embed_to_table_rows(title, description, fields)
        discord_ok = self._discord_send_embed(title, description, color, fields)
        if critical:
            self._send_email_tracked(title, level, body_text=plain, table_rows=rows)
        elif not discord_ok:
            self._send_email_tracked(title, "WARNING", body_text=plain, table_rows=rows)

    def send_trade_alert(self, trade_info: dict) -> None:
        action = trade_info.get("action", "")
        symbol = trade_info.get("symbol", "")
        price = trade_info.get("price", 0)
        quantity = trade_info.get("quantity", 0)
        pnl = trade_info.get("pnl", 0)
        pnl_rate = trade_info.get("pnl_rate", 0)

        is_buy = action == "BUY"
        color = 0x27AE60 if is_buy else 0xE74C3C
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
        self.send_embed(title, "", color=color, fields=fields, critical=action != "BUY" and pnl_rate <= -5.0)

    def send_daily_report(self, report: dict) -> None:
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

    def send_signal_alert(self, symbol: str, signal_info: dict) -> None:
        signal = signal_info.get("signal", "HOLD")
        if signal == "HOLD":
            return

        score = signal_info.get("score", 0)
        close = signal_info.get("close", 0)
        details = signal_info.get("details", {})
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

    @staticmethod
    def _embed_to_table_rows(
        title: str, description: str, fields: list = None,
    ) -> List[Dict[str, Any]]:
        """Embed fields에서 종목/신호/가격/시간 형태로 1행 구성 (없으면 제목·설명으로 대체)."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        symbol = "—"
        signal = "—"
        price = "—"
        if fields:
            name_to_val = {str(f.get("name", "")).strip(): f.get("value", "") for f in fields}
            for key in ("종목", "Symbol", "symbol"):
                if key in name_to_val:
                    symbol = str(name_to_val[key])
                    break
            for key in ("신호", "Signal", "점수"):
                if key in name_to_val:
                    signal = str(name_to_val[key])
                    break
            for key in ("가격", "종가", "가격(원)"):
                if key in name_to_val:
                    price = str(name_to_val[key])
                    break
        if symbol == "—" and title:
            signal = title[:200]
        if description and price == "—":
            price = description[:120]
        return [{"symbol": symbol, "signal": signal, "price": price, "time": now}]

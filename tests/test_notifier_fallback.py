"""Notifier 폴백 체인 회귀 테스트.

핵심: DiscordBot은 비활성(웹훅 미설정) 시 콘솔 폴백하며 True를 반환한다 —
Notifier가 그 True를 '발송 성공'으로 믿으면 이메일 폴백이 영영 트리거되지 않아,
웹훅 미설정 + SMTP 설정 환경에서 일반 알림이 콘솔에만 남는다(무인 운영 깜깜이).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.notifier import Notifier


def _notifier(discord_enabled, webhook="https://discord/webhook", send_ok=True):
    n = Notifier.__new__(Notifier)  # __init__ 우회(설정 로드 없이 구성)
    n.config = SimpleNamespace()
    n.discord = SimpleNamespace(
        enabled=discord_enabled,
        webhook_url=webhook if discord_enabled else "",
        send_message=MagicMock(return_value=True if not discord_enabled else send_ok),
        send_embed=MagicMock(return_value=True if not discord_enabled else send_ok),
    )
    n._email_enabled = True
    n._email_calls = []
    n._send_email_tracked = lambda *a, **kw: n._email_calls.append((a, kw)) or True
    return n


def test_disabled_discord_triggers_email_fallback():
    """웹훅 미설정(콘솔 폴백 True)이어도 일반 알림은 이메일 폴백으로 가야 한다."""
    n = _notifier(discord_enabled=False)
    n.send_message("일반 알림")
    assert len(n._email_calls) == 1
    # 콘솔 기록은 유지(디스코드 객체 호출은 함)
    assert n.discord.send_message.called


def test_disabled_discord_does_not_count_as_failure():
    """설정상 비활성은 장애가 아니다 — 실패 카운트 비증가(양채널 사망 경보 오탐 방지)."""
    Notifier._discord_fail_count = 0
    n = _notifier(discord_enabled=False)
    n.send_message("일반 알림")
    assert Notifier._discord_fail_count == 0


def test_enabled_discord_success_skips_email():
    """디스코드 실제 발송 성공이면 일반 알림은 이메일을 보내지 않는다(기존 동작)."""
    n = _notifier(discord_enabled=True, send_ok=True)
    n.send_message("일반 알림")
    assert n._email_calls == []


def test_enabled_discord_failure_falls_back_to_email():
    n = _notifier(discord_enabled=True, send_ok=False)
    n.send_message("일반 알림")
    assert len(n._email_calls) == 1


def test_critical_always_attempts_email():
    """critical은 디스코드 성공 여부와 무관하게 이메일도 발송한다."""
    n = _notifier(discord_enabled=True, send_ok=True)
    n.send_message("긴급", critical=True)
    assert len(n._email_calls) == 1


def test_embed_disabled_discord_triggers_email_fallback():
    n = _notifier(discord_enabled=False)
    n.send_embed("제목", "설명", fields=[{"name": "종목", "value": "005930"}])
    assert len(n._email_calls) == 1

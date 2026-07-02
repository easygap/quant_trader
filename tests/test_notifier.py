from core.notifier import Notifier


class _MockConfig:
    discord = {
        "enabled": True,
        "webhook_url": "https://example.invalid/webhook",
        "username": "테스트",
        "avatar_url": "",
    }
    _settings = {
        "email": {
            "enabled": True,
            "smtp_server": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "sender@example.com",
            "alert_to": "ops@example.com",
        }
    }


class _FailingDiscord:
    # 실제 DiscordBot 인터페이스 반영: 활성+웹훅 설정 상태(전달 가능)에서의 '발송 실패'.
    # (비활성 콘솔 폴백과 구분 — 비활성은 실패 카운트가 오르지 않는다)
    enabled = True
    webhook_url = "https://example.invalid/webhook"

    def __init__(self):
        self.embeds = []

    def send_embed(self, title, description, color=0x4F9EF8, fields=None):
        self.embeds.append({
            "title": title,
            "description": description,
            "color": color,
            "fields": fields or [],
        })
        return False

    def send_message(self, text):
        raise AssertionError("send_message should not be used by embed helpers")

    def send_trade_alert(self, trade_info):
        raise AssertionError("send_trade_alert must go through Notifier fallback")

    def send_daily_report(self, report):
        raise AssertionError("send_daily_report must go through Notifier fallback")

    def send_signal_alert(self, symbol, signal_info):
        raise AssertionError("send_signal_alert must go through Notifier fallback")


def _notifier_with_failing_discord(monkeypatch):
    notifier = Notifier(_MockConfig())
    notifier.discord = _FailingDiscord()
    emails = []
    monkeypatch.setattr(
        notifier,
        "_send_email",
        lambda title, alert_level, body_text="", table_rows=None: emails.append({
            "title": title,
            "alert_level": alert_level,
            "body_text": body_text,
            "table_rows": table_rows or [],
        }) or True,
    )
    Notifier._discord_fail_count = 0
    Notifier._email_fail_count = 0
    Notifier._dual_critical_logged = False
    return notifier, emails


def test_trade_alert_uses_fallback_when_discord_embed_fails(monkeypatch):
    notifier, emails = _notifier_with_failing_discord(monkeypatch)

    notifier.send_trade_alert({
        "action": "BUY",
        "symbol": "005930",
        "price": 70000,
        "quantity": 3,
    })

    assert len(notifier.discord.embeds) == 1
    assert emails[0]["alert_level"] == "WARNING"
    assert Notifier._discord_fail_count == 1
    assert Notifier._email_fail_count == 0


def test_stop_loss_trade_alert_sends_critical_email(monkeypatch):
    notifier, emails = _notifier_with_failing_discord(monkeypatch)

    notifier.send_trade_alert({
        "action": "SELL",
        "symbol": "005930",
        "price": 65000,
        "quantity": 2,
        "pnl": -800000,
        "pnl_rate": -5.2,
    })

    assert notifier.discord.embeds[0]["title"] == "🔴 매도 — SELL"
    assert emails[0]["alert_level"] == "CRITICAL"
    assert Notifier._discord_fail_count == 1


def test_daily_report_uses_fallback_when_discord_embed_fails(monkeypatch):
    notifier, emails = _notifier_with_failing_discord(monkeypatch)

    notifier.send_daily_report({
        "total_value": 10_000_000,
        "cash": 2_000_000,
        "daily_return": -0.5,
        "cumulative_return": 1.2,
        "mdd": -3.0,
        "position_count": 2,
        "total_trades": 1,
    })

    assert notifier.discord.embeds[0]["title"] == "📊 일일 리포트"
    assert emails[0]["alert_level"] == "WARNING"
    assert Notifier._discord_fail_count == 1


def test_daily_report_v2_extra_fields_rendered(monkeypatch):
    """v2 부가 필드(벤치마크 격차·배치율·진행률·비용·미체결)가 있으면 embed 필드로 렌더링."""
    notifier, _ = _notifier_with_failing_discord(monkeypatch)
    notifier.send_daily_report({
        "total_value": 9_452_986, "cash": 3_887_986,
        "daily_return": -0.47, "cumulative_return": -5.47,
        "mdd": -10.69, "position_count": 9, "total_trades": 0,
        "benchmark_gap": "NAV -5.47% vs KS11 +0.71% (격차 -6.18%p)",
        "deployment": "주식 59% / 설계 80% (-21.0%p)",
        "progress": "17/60일 (28%) · 커버리지 94% · 결측예산 2일",
        "cost": "누적 0.096% · 연환산 1.43%(참고)",
        "slot_warning": "미체결 1개: 000660 1주 2,424,000원 > 슬롯 756,239원 — 자본 결정 대기(#422)",
    })
    names = [f["name"] for f in notifier.discord.embeds[0]["fields"]]
    assert "📊 vs KS11" in names
    assert "🎯 주식 배치율" in names
    assert "📅 진행률" in names
    assert "💸 누적 비용" in names
    assert "⚠️ 미체결 슬롯" in names


def test_daily_report_omits_absent_v2_fields(monkeypatch):
    """v2 키가 없으면 기존 카드 그대로 — 부가 필드 미표시(하위 호환)."""
    notifier, _ = _notifier_with_failing_discord(monkeypatch)
    notifier.send_daily_report({
        "total_value": 10_000_000, "cash": 2_000_000,
        "daily_return": 0.1, "cumulative_return": 1.2, "mdd": -3.0,
        "position_count": 2, "total_trades": 1,
    })
    names = [f["name"] for f in notifier.discord.embeds[0]["fields"]]
    assert "📊 vs KS11" not in names
    assert "⚠️ 미체결 슬롯" not in names


def test_signal_alert_uses_fallback_and_hold_is_silent(monkeypatch):
    notifier, emails = _notifier_with_failing_discord(monkeypatch)

    notifier.send_signal_alert("005930", {"signal": "HOLD", "score": 0, "close": 70000})
    assert notifier.discord.embeds == []
    assert emails == []

    notifier.send_signal_alert("005930", {"signal": "SELL", "score": -3, "close": 69000})

    assert notifier.discord.embeds[0]["title"] == "🔴 SELL 신호 감지"
    assert emails[0]["alert_level"] == "WARNING"
    assert Notifier._discord_fail_count == 1

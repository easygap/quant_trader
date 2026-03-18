"""DiscordBot fallback 반환값 테스트."""

from monitoring.discord_bot import DiscordBot


class _MockConfig:
    discord = {
        "enabled": False,
        "webhook_url": "",
        "username": "테스트",
        "avatar_url": "",
    }


def test_send_message_returns_true_on_console_fallback():
    bot = DiscordBot(_MockConfig())
    assert bot.send_message("fallback ok") is True


def test_send_embed_returns_true_on_console_fallback():
    bot = DiscordBot(_MockConfig())
    assert bot.send_embed("title", "desc") is True

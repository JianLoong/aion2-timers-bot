"""Entrypoint behaviour: the intents it asks for, and the first-run failures."""

from __future__ import annotations

import sqlite3

import discord

import bot as bot_module
from aion2_timerbot.db import Store


async def test_bot_requests_the_guilds_intent(bot):
    """Without it every guild is "unavailable", so role pings could never resolve."""
    assert bot.intents.guilds is True
    assert bot.intents.members is False
    assert bot.intents.message_content is False


def _prepare(monkeypatch, tmp_path, token: str | None) -> None:
    # A developer's own .env must not decide the outcome of these tests.
    monkeypatch.setattr(bot_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "timers.db"))
    if token is None:
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DISCORD_TOKEN", token)


def test_a_missing_token_is_reported_without_a_traceback(monkeypatch, tmp_path, capsys):
    _prepare(monkeypatch, tmp_path, None)

    assert bot_module.main() == 2

    stderr = capsys.readouterr().err
    assert "DISCORD_TOKEN" in stderr
    assert "Traceback" not in stderr


def test_a_rejected_token_is_reported_without_a_traceback(monkeypatch, tmp_path, capsys):
    async def rejected(self, token, **kwargs):
        raise discord.LoginFailure("Improper token has been passed.")

    _prepare(monkeypatch, tmp_path, "not-a-real-token")
    monkeypatch.setattr(bot_module.Aion2TimerBot, "start", rejected)

    assert bot_module.main() == 3

    stderr = capsys.readouterr().err
    assert "DISCORD_TOKEN" in stderr
    assert "Traceback" not in stderr


def test_an_unwritable_database_path_is_reported(monkeypatch, tmp_path, capsys):
    """A bad DB_PATH, or a volume the bot's uid cannot write, must not look like a crash."""

    async def denied(self):
        raise sqlite3.OperationalError("unable to open database file")

    _prepare(monkeypatch, tmp_path, "not-a-real-token")
    monkeypatch.setattr(Store, "open", denied)

    assert bot_module.main() == 2

    stderr = capsys.readouterr().err
    assert "database" in stderr
    assert "timers.db" in stderr
    assert "Traceback" not in stderr
    assert "Event loop is closed" not in stderr


def test_a_folder_that_cannot_be_made_is_reported(monkeypatch, tmp_path, capsys):
    """The other half: mkdir fails, which arrives as an OSError instead."""

    async def denied(self):
        raise PermissionError(13, "Permission denied")

    _prepare(monkeypatch, tmp_path, "not-a-real-token")
    monkeypatch.setattr(Store, "open", denied)

    assert bot_module.main() == 2

    stderr = capsys.readouterr().err
    assert "database" in stderr
    assert "Traceback" not in stderr

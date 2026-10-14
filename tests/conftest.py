"""Shared test doubles: a real Bot, a real Store, and fake Discord objects."""

from __future__ import annotations

from typing import Any

import discord
import pytest_asyncio

from bot import Aion2TimerBot
from aion2_timerbot.cogs.timers import TimerCog
from aion2_timerbot.config import Config
from aion2_timerbot.db import Store
from aion2_timerbot.presets import BUILTIN_PATH, load_library


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, ephemeral=False, **kwargs):
        self._done = True
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    @property
    def last(self) -> dict[str, Any]:
        return self.messages[-1]


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class FakeUser:
    def __init__(self, user_id: int = 1, *, permissions: discord.Permissions | None = None) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.guild_permissions = permissions or discord.Permissions.none()


class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    def __init__(self, guild_id: int = 99, roles: list[FakeRole] | None = None) -> None:
        self.id = guild_id
        self.roles = list(roles or [])

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((role for role in self.roles if role.id == role_id), None)


class FakeChannel:
    def __init__(self, channel_id: int = 555) -> None:
        self.id = channel_id
        self.sent: list[dict[str, Any]] = []

    async def send(self, content=None, *, embed=None, allowed_mentions=None, **kwargs):
        self.sent.append({"content": content, "embed": embed, "allowed_mentions": allowed_mentions})


class FakeHTTPResponse:
    """Enough of an aiohttp response for discord.py's HTTPException to be built."""

    def __init__(self, status: int = 404, reason: str = "Not Found") -> None:
        self.status = status
        self.reason = reason


_NO_GUILD = object()


class FakeInteraction:
    def __init__(self, *, user=None, guild=_NO_GUILD, channel=None) -> None:
        self.user = user or FakeUser()
        # pass guild=None explicitly for the "used in a DM" case
        self.guild = FakeGuild() if guild is _NO_GUILD else guild
        self.channel = channel or FakeChannel()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = None
        self.guild_id = self.guild.id if self.guild is not None else None


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = Store(tmp_path / "timers.db")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


@pytest_asyncio.fixture
async def bot(tmp_path):
    """A real bot with the cog loaded but no gateway connection.

    The cog is added directly rather than through ``load_extension``: discord.py's
    extension loader re-executes the module from its spec, which would leave two
    copies of every class in it and break identity checks in the tests. The loader
    path has its own test.
    """
    config = Config(token="test-token", db_path=tmp_path / "bot.db", tick_seconds=3600.0)
    instance = Store(config.db_path)
    await instance.open()
    player = Aion2TimerBot(config, instance, load_library(BUILTIN_PATH))
    await player.add_cog(TimerCog(player))
    try:
        yield player
    finally:
        cog = player.get_cog("TimerCog")
        if cog is not None:
            await cog.cog_unload()
        await instance.close()

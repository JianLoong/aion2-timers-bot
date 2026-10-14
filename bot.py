"""Entry point for the Aion 2 timer bot: ``python bot.py``."""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from aion2_timerbot.config import Config, ConfigError
from aion2_timerbot.db import Store
from aion2_timerbot.presets import BUILTIN_PATH, PresetLibrary, load_library

log = logging.getLogger("aion2_timerbot")


def build_intents() -> discord.Intents:
    """Only the non-privileged GUILDS intent.

    Nothing to tick in the developer portal, but it keeps guild roles and channels
    cached, which role pings and ``bot.get_channel()`` rely on. With ``Intents.none()``
    every guild is "unavailable", so a role name or ID could never be resolved.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class Aion2TimerBot(commands.Bot):
    """The bot instance: owns the store and the loaded presets."""

    def __init__(self, config: Config, store: Store, presets: PresetLibrary) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            help_command=None,
        )
        self.config = config
        self.store = store
        self.presets = presets

    async def setup_hook(self) -> None:
        await self.load_extension("aion2_timerbot.cogs.timers")
        if self.config.dev_guild_id is not None:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to the dev guild %s", len(synced), self.config.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %d commands globally (this can take up to an hour)", len(synced))

    async def on_ready(self) -> None:
        me = self.user
        log.info("logged in as %s (%s) across %d guilds", me, me.id if me else "?", len(self.guilds))


async def run() -> None:
    load_dotenv()
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    presets = load_library(config.presets_file or BUILTIN_PATH)
    log.info("loaded %d built-in presets", len(presets.presets))

    store = Store(config.db_path)
    try:
        await store.open()
    except OSError as exc:
        raise ConfigError(
            f"Could not open the timer database at {store.path}: {exc}. Check DB_PATH and that "
            "the folder it points at exists and is writable."
        ) from exc
    log.info("timers are stored in %s", store.path)

    bot = Aion2TimerBot(config, store, presets)
    try:
        await bot.start(config.token)
    finally:
        # closes the HTTP session too, so a failed login exits quietly
        await bot.close()
        await store.close()


def main() -> int:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except discord.LoginFailure:
        print(
            "Discord rejected the token in DISCORD_TOKEN. Copy it again from "
            "https://discord.com/developers/applications -> your app -> Bot -> Reset Token, "
            "and check there is no surrounding whitespace or quotes in .env.",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

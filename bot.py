"""Entry point for the Aion 2 timer bot: ``python bot.py``."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from aion2_timerbot.config import Config, ConfigError
from aion2_timerbot.db import Store
from aion2_timerbot.presets import BUILTIN_PATH, PresetLibrary, load_library
from aion2_timerbot.schedule_source import ScheduleSource

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
    """The bot instance: owns the store, the presets and the schedule source."""

    def __init__(
        self,
        config: Config,
        store: Store,
        presets: PresetLibrary,
        schedules: ScheduleSource,
    ) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            help_command=None,
        )
        self.config = config
        self.store = store
        self.presets = presets
        self.schedules = schedules

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

    schedules = ScheduleSource(
        region=config.schedule_region,
        url=config.schedule_url,
        cache_path=config.schedule_cache_path,
        override_path=config.schedules_path,
        ttl_seconds=config.schedule_ttl_hours * 3600.0,
    )

    store = Store(config.db_path)
    try:
        await store.open()
    except (OSError, sqlite3.Error) as exc:
        # sqlite3 reports an unwritable folder as OperationalError ("unable to open database
        # file"), which is not an OSError, so both have to be caught to explain it properly.
        raise ConfigError(
            f"Could not open the timer database at {store.path}: {exc}. Make sure that folder "
            "exists and is writable by the user running the bot — in Docker that is uid 1000, "
            "so a root-owned ./data needs `chown -R 1000:1000 data` on the host."
        ) from exc
    log.info("timers are stored in %s", store.path)

    # Load the event schedule now, so a bad URL or override file shows up in the log at
    # startup rather than the first time somebody asks for the board.
    schedule = await schedules.get()
    log.info(
        "loaded %d scheduled events for %s (%s)",
        len(schedule.events), schedule.region.label, schedule.region.time_zone,
    )
    if schedules.problem:
        log.warning("schedule problem: %s", schedules.problem)

    bot = Aion2TimerBot(config, store, presets, schedules)
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

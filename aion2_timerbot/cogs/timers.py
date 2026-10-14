"""Slash commands for setting, showing and cancelling Aion 2 timers."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..db import DuplicateTimerError, Store
from ..duration import DurationError, format_timedelta, parse_duration
from ..models import Timer
from ..presets import Preset, PresetLibrary
from ..scheduler import TimerScheduler, next_occurrence

if TYPE_CHECKING:
    from ..bot import Aion2TimerBot

log = logging.getLogger(__name__)

MAX_NAME = 60
MAX_NOTE = 200
MAX_ALIASES = 120
MAX_LINES = 40
TIMER_COLOUR = discord.Colour(0x4C7EFF)
DONE_COLOUR = discord.Colour(0xF2B33D)

_USAGE_NAME = "Use `/timer set`, or start a preset with `/timer preset start`."


class UserError(app_commands.AppCommandError):
    """An error worth showing to the person who ran the command, verbatim."""


class TimerCog(commands.Cog):
    """Set named countdowns and get pinged when they end."""

    timer_group = app_commands.Group(
        name="timer",
        description="Set and show Aion 2 timers.",
        guild_only=True,
    )
    preset_group = app_commands.Group(
        name="preset",
        description="Ready-made timers for recurring Aion 2 content.",
        parent=timer_group,
    )

    def __init__(self, bot: "Aion2TimerBot") -> None:
        self.bot = bot
        self.config = bot.config
        self.store: Store = bot.store
        self.builtin: PresetLibrary = bot.presets
        self.scheduler = TimerScheduler(
            self.store, self._announce, tick_seconds=bot.config.tick_seconds
        )

    async def cog_load(self) -> None:
        await self.scheduler.start()

    async def cog_unload(self) -> None:
        await self.scheduler.stop()

    # ------------------------------------------------------------ autocomplete

    async def timer_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        now = time.time()
        needle = current.casefold()
        choices: list[app_commands.Choice[str]] = []
        for timer in await self.store.list_timers(guild.id):
            if needle and needle not in timer.name.casefold():
                continue
            choices.append(
                app_commands.Choice(name=_clamp(_timer_label(timer, now)), value=timer.name)
            )
        return choices[:25]

    async def preset_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        library = await self._library(interaction)
        needle = current.casefold()
        choices: list[app_commands.Choice[str]] = []
        for preset in library.sorted():
            haystack = (preset.display_name, *preset.aliases)
            if needle and not any(needle in part.casefold() for part in haystack):
                continue
            mark = " (server)" if preset.source == "guild" else ""
            label = f"{preset.display_name}{mark} every {format_timedelta(_seconds(preset.interval_seconds))}"
            choices.append(app_commands.Choice(name=_clamp(label), value=preset.display_name))
        return choices[:25]

    async def guild_preset_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        needle = current.casefold()
        choices = [
            app_commands.Choice(
                name=_clamp(f"{p.display_name} every {format_timedelta(_seconds(p.interval_seconds))}"),
                value=p.display_name,
            )
            for p in await self.store.list_guild_presets(guild.id)
            if not needle or needle in p.display_name.casefold()
        ]
        return choices[:25]

    # ---------------------------------------------------------------- commands

    @timer_group.command(name="set", description="Start a countdown and get pinged when it ends.")
    @app_commands.describe(
        name="What the timer is for, e.g. World Boss.",
        duration="How long until it fires, e.g. 90m, 2h30m, 1d.",
        repeat="Keep firing every `duration` after the first alert.",
        note="Extra text shown when it fires.",
        ping="Who to ping: here, everyone, or a role name or @mention.",
        channel="Where to post the alert. Defaults to this channel.",
        replace="Overwrite a timer that already has this name.",
    )
    async def timer_set(
        self,
        interaction: discord.Interaction,
        name: str,
        duration: str,
        repeat: bool = False,
        note: str | None = None,
        ping: str | None = None,
        channel: discord.TextChannel | None = None,
        replace: bool = False,
    ) -> None:
        timer = await self._create_timer(
            interaction,
            name=name,
            duration=duration,
            repeat=repeat,
            note=note,
            ping=ping,
            channel=channel,
            replace=replace,
        )
        await interaction.response.send_message(embed=self._set_embed(timer), ephemeral=True)

    @timer_group.command(name="list", description="Show every timer running in this server.")
    async def timer_list(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        timers = await self.store.list_timers(guild.id)
        await interaction.response.send_message(embed=self._list_embed(timers), ephemeral=True)

    @timer_group.command(name="cancel", description="Stop a timer before it fires.")
    @app_commands.describe(name="The timer to stop.")
    @app_commands.autocomplete(name=timer_name_autocomplete)
    async def timer_cancel(self, interaction: discord.Interaction, name: str) -> None:
        guild = self._require_guild(interaction)
        timer = await self.store.get_timer_by_name(guild.id, name)
        if timer is None:
            raise UserError(f"No timer called **{name}** is running here. `/timer list` shows what is.")
        if timer.created_by != interaction.user.id and not self._can_manage_channels(interaction):
            raise UserError(
                "Only the person who set this timer, or someone who can manage channels, can cancel it."
            )
        await self.store.delete_timer(timer.id)
        await interaction.response.send_message(f"Stopped the **{timer.name}** timer.", ephemeral=True)

    @preset_group.command(name="list", description="Show the timers you can start in one command.")
    @app_commands.describe(category="Only show presets in this category.")
    async def preset_list(
        self, interaction: discord.Interaction, category: str | None = None
    ) -> None:
        library = await self._library(interaction)
        wanted = category.strip().casefold() if category else None
        presets = [p for p in library.sorted() if not wanted or p.category.casefold() == wanted]
        if wanted and not presets:
            raise UserError(
                f"No presets in **{category}**. Categories: "
                + ", ".join(f"`{c}`" for c in library.categories())
            )
        await interaction.response.send_message(embed=self._preset_embed(presets), ephemeral=True)

    @preset_group.command(name="start", description="Start a timer from a preset.")
    @app_commands.describe(
        preset="Which preset to start.",
        delay="How long until it first fires. Defaults to one preset interval.",
        repeat="Keep repeating at the preset interval.",
        note="Extra text shown when it fires.",
        ping="Who to ping: here, everyone, or a role name or @mention.",
        channel="Where to post the alert. Defaults to this channel.",
        replace="Overwrite a timer that already has this name.",
    )
    @app_commands.autocomplete(preset=preset_name_autocomplete)
    async def preset_start(
        self,
        interaction: discord.Interaction,
        preset: str,
        delay: str | None = None,
        repeat: bool = True,
        note: str | None = None,
        ping: str | None = None,
        channel: discord.TextChannel | None = None,
        replace: bool = False,
    ) -> None:
        timer = await self._start_preset(
            interaction,
            preset_name=preset,
            delay=delay,
            repeat=repeat,
            note=note,
            ping=ping,
            channel=channel,
            replace=replace,
        )
        await interaction.response.send_message(
            embed=self._set_embed(timer, title="⏳ Preset timer set"), ephemeral=True
        )

    @preset_group.command(name="add", description="Save a preset of your own for this server.")
    @app_commands.describe(
        name="Preset name, e.g. Ancient Boss.",
        interval="How often it repeats, e.g. 3h30m.",
        category="Grouping used by /timer preset list.",
        aliases="Short names to accept too, separated by commas.",
        note="Text saved alongside the preset.",
    )
    async def preset_add(
        self,
        interaction: discord.Interaction,
        name: str,
        interval: str,
        category: str | None = None,
        aliases: str | None = None,
        note: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        if not self._can_manage_guild(interaction):
            raise UserError("You need the **Manage Server** permission to change this server's presets.")

        clean_name = name.strip()
        if not clean_name or len(clean_name) > MAX_NAME:
            raise UserError(f"Give the preset a name of {MAX_NAME} characters or fewer.")
        if aliases and len(aliases) > MAX_ALIASES:
            raise UserError(f"Keep aliases to {MAX_ALIASES} characters or fewer.")
        if note and len(note) > MAX_NOTE:
            raise UserError(f"Keep the note to {MAX_NOTE} characters or fewer.")
        try:
            every = parse_duration(interval)
        except DurationError as exc:
            raise UserError(str(exc)) from exc

        preset = await self.store.add_guild_preset(
            guild_id=guild.id,
            display_name=clean_name,
            interval_seconds=every.total_seconds(),
            category=(category or "Custom").strip() or "Custom",
            aliases=aliases or "",
            note=note.strip() if note else None,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Saved the **{preset.display_name}** preset, repeating every "
            f"{format_timedelta(_seconds(preset.interval_seconds))}. Start it with "
            f"`/timer preset start preset:{preset.display_name}`.",
            ephemeral=True,
        )

    @preset_group.command(name="remove", description="Delete one of this server's presets.")
    @app_commands.describe(name="The preset to delete.")
    @app_commands.autocomplete(name=guild_preset_autocomplete)
    async def preset_remove(self, interaction: discord.Interaction, name: str) -> None:
        guild = self._require_guild(interaction)
        if not self._can_manage_guild(interaction):
            raise UserError("You need the **Manage Server** permission to change this server's presets.")
        existing = await self.store.get_guild_preset(guild.id, name)
        if existing is None:
            raise UserError(
                f"This server has no preset called **{name}**. Built-in presets live in "
                "`data/presets.json` and are edited there."
            )
        await self.store.delete_guild_preset(guild.id, existing.display_name)
        await interaction.response.send_message(
            f"Deleted the **{existing.display_name}** preset.", ephemeral=True
        )

    # ------------------------------------------------------------- timer setup

    async def _create_timer(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        duration: str,
        repeat: bool,
        note: str | None,
        ping: str | None,
        channel: discord.abc.GuildChannel | None,
        replace: bool,
    ) -> Timer:
        guild = self._require_guild(interaction)
        clean_name = name.strip()
        if not clean_name:
            raise UserError("Give the timer a name.")
        if len(clean_name) > MAX_NAME:
            raise UserError(f"Keep the timer name to {MAX_NAME} characters or fewer.")
        if note and len(note) > MAX_NOTE:
            raise UserError(f"Keep the note to {MAX_NOTE} characters or fewer.")
        try:
            delta = parse_duration(duration)
        except DurationError as exc:
            raise UserError(str(exc)) from exc

        target = channel or interaction.channel
        if target is None:
            raise UserError("I could not tell which channel to post in — pass the `channel` option.")
        mention = self._resolve_mention(guild, ping)
        await self._guard_limit(guild.id, clean_name, replace)

        now = time.time()
        return await self._insert(
            guild_id=guild.id,
            channel_id=target.id,
            name=clean_name,
            due_at=now + delta.total_seconds(),
            created_by=interaction.user.id,
            interval_seconds=delta.total_seconds() if repeat else None,
            note=note.strip() if note else None,
            mention=mention,
            replace=replace,
        )

    async def _start_preset(
        self,
        interaction: discord.Interaction,
        *,
        preset_name: str,
        delay: str | None,
        repeat: bool,
        note: str | None,
        ping: str | None,
        channel: discord.abc.GuildChannel | None,
        replace: bool,
    ) -> Timer:
        guild = self._require_guild(interaction)
        library = await self._library(interaction)
        found: Preset | None = library.get(preset_name)
        if found is None:
            raise UserError(
                f"I don't know a preset called **{preset_name}**. `/timer preset list` shows what exists."
            )
        if note and len(note) > MAX_NOTE:
            raise UserError(f"Keep the note to {MAX_NOTE} characters or fewer.")

        first_fire = found.interval_seconds
        if delay:
            try:
                first_fire = parse_duration(delay).total_seconds()
            except DurationError as exc:
                raise UserError(str(exc)) from exc

        target = channel or interaction.channel
        if target is None:
            raise UserError("I could not tell which channel to post in — pass the `channel` option.")
        mention = self._resolve_mention(guild, ping)
        await self._guard_limit(guild.id, found.display_name, replace)

        now = time.time()
        return await self._insert(
            guild_id=guild.id,
            channel_id=target.id,
            name=found.display_name,
            due_at=now + first_fire,
            created_by=interaction.user.id,
            interval_seconds=found.interval_seconds if repeat else None,
            note=(note.strip() if note else found.note),
            mention=mention,
            replace=replace,
        )

    async def _insert(self, **kwargs: object) -> Timer:
        try:
            return await self.store.add_timer(**kwargs)  # type: ignore[arg-type]
        except DuplicateTimerError as exc:
            raise UserError(
                f"A timer called **{kwargs.get('name')}** is already running. Cancel it with "
                "`/timer cancel`, or re-run with `replace: True`."
            ) from exc

    async def _guard_limit(self, guild_id: int, name: str, replace: bool) -> None:
        if replace:
            return
        existing = await self.store.get_timer_by_name(guild_id, name)
        if existing is not None:
            raise UserError(
                f"A timer called **{existing.name}** is already running. Cancel it with "
                "`/timer cancel`, or re-run with `replace: True`."
            )
        count = await self.store.count_timers(guild_id)
        if count >= self.config.max_timers_per_guild:
            raise UserError(
                f"This server already has {self.config.max_timers_per_guild} timers, the most I keep. "
                "Cancel one first."
            )

    # ------------------------------------------------------------ announcements

    async def _announce(self, timer: Timer, lateness: float) -> None:
        channel = self.bot.get_channel(timer.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(timer.channel_id)
            except discord.HTTPException:
                log.warning(
                    "timer #%s (%s) points at channel %s, which I cannot reach",
                    timer.id, timer.name, timer.channel_id,
                )
                return

        send = getattr(channel, "send", None)
        if send is None:
            log.warning(
                "timer #%s (%s) points at channel %s, which cannot receive messages",
                timer.id, timer.name, timer.channel_id,
            )
            return

        # Only the validated mention reaches the message text: the timer name, which
        # anyone can set, goes in the embed, where Discord never turns it into a ping.
        content = f"{self._mention_prefix(timer.mention)}Timer finished".strip()
        allowed = discord.AllowedMentions(
            everyone=timer.mention == "everyone",
            roles=bool(timer.mention and timer.mention.startswith("<@&")),
            users=False,
        )
        try:
            await send(
                content=content,
                embed=self._finished_embed(timer, lateness),
                allowed_mentions=allowed,
            )
        except discord.Forbidden:
            log.warning(
                "no permission to post timer #%s in channel %s", timer.id, timer.channel_id
            )
        except discord.HTTPException:
            log.warning("could not post timer #%s", timer.id, exc_info=True)

    # ------------------------------------------------------------------ embeds

    def _set_embed(self, timer: Timer, *, title: str = "⏳ Timer set") -> discord.Embed:
        embed = discord.Embed(title=title, colour=TIMER_COLOUR, description=f"**{timer.name}**")
        fires = _dt(timer.due_at)
        embed.add_field(
            name="Fires",
            value=f"{discord.utils.format_dt(fires, 'R')} ({discord.utils.format_dt(fires, 'f')})",
        )
        if timer.is_repeating:
            embed.add_field(name="Repeats", value=f"every {format_timedelta(_seconds(timer.interval_seconds or 0))}")
        if timer.note:
            embed.add_field(name="Note", value=timer.note, inline=False)
        if timer.mention:
            embed.add_field(name="Ping", value=_mention_label(timer.mention))
        return embed

    def _list_embed(self, timers: list[Timer]) -> discord.Embed:
        embed = discord.Embed(
            title=f"⏳ Timers in this server ({len(timers)})", colour=TIMER_COLOUR
        )
        if not timers:
            embed.description = f"Nothing is running. {_USAGE_NAME}"
            return embed

        now = time.time()
        lines = [_timer_label(timer, now, channel_mention=True) for timer in timers[:MAX_LINES]]
        embed.description = "\n".join(lines)
        if len(timers) > MAX_LINES:
            embed.set_footer(text=f"Showing the next {MAX_LINES} of {len(timers)} timers.")
        return embed

    def _preset_embed(self, presets: list[Preset]) -> discord.Embed:
        embed = discord.Embed(
            title="⏳ Presets", colour=TIMER_COLOUR, description="Start one with `/timer preset start`."
        )
        if not presets:
            embed.description = "This server has no presets yet — add one with `/timer preset add`."
            return embed
        for category in dict.fromkeys(p.category for p in presets):
            entries = [p for p in presets if p.category == category]
            lines = []
            for preset in entries:
                aliases = f" (also {', '.join(preset.aliases)})" if preset.aliases else ""
                source = " · server" if preset.source == "guild" else ""
                lines.append(
                    f"**{preset.display_name}** — every "
                    f"{format_timedelta(_seconds(preset.interval_seconds))}{aliases}{source}"
                )
            embed.add_field(name=category, value="\n".join(lines)[:1024], inline=False)
        return embed

    def _finished_embed(self, timer: Timer, lateness: float) -> discord.Embed:
        embed = discord.Embed(
            title=f"⏰ {timer.name} is up!",
            colour=DONE_COLOUR,
            description=f"<@{timer.created_by}>" if timer.created_by else None,
        )
        if timer.note:
            embed.add_field(name="Note", value=timer.note, inline=False)
        if timer.is_repeating and timer.interval_seconds:
            nxt = next_occurrence(timer.due_at, timer.interval_seconds, time.time())
            embed.add_field(
                name="Next", value=discord.utils.format_dt(_dt(nxt), "R"), inline=True
            )
        if lateness > self.config.missed_after_seconds:
            embed.add_field(
                name="Late",
                value=(
                    "I was offline or busy, so this fired "
                    f"{format_timedelta(_seconds(lateness))} late."
                ),
                inline=False,
            )
        return embed

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("Timers only work inside a server.")
        return interaction.guild

    @staticmethod
    def _can_manage_channels(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and (permissions.manage_channels or permissions.administrator))

    @staticmethod
    def _can_manage_guild(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and (permissions.manage_guild or permissions.administrator))

    @staticmethod
    def _resolve_mention(guild: discord.Guild, raw: str | None) -> str | None:
        """Turn user input into "here", "everyone" or a role mention, or fail loudly."""
        if raw is None:
            return None
        text = raw.strip()
        if not text or text.casefold() in {"none", "no", "off", "-"}:
            return None

        lowered = text.casefold().lstrip("@")
        if lowered in {"here", "everyone"}:
            return lowered
        if text.startswith("<@&") and text.endswith(">"):
            role_id = text[3:-1]
            if role_id.isdigit() and guild.get_role(int(role_id)) is not None:
                return f"<@&{role_id}>"
            raise UserError("That role mention is not a role in this server.")
        if text.isdigit():
            if guild.get_role(int(text)) is not None:
                return f"<@&{text}>"
            raise UserError(f"No role in this server has the ID `{text}`.")
        role = discord.utils.find(lambda r: r.name.casefold() == lowered, guild.roles)
        if role is not None:
            return f"<@&{role.id}>"
        raise UserError(
            f"I could not find a role called **{raw}**. Use `here`, `everyone`, or the exact "
            "name or @mention of a role."
        )

    @staticmethod
    def _mention_prefix(mention: str | None) -> str:
        if not mention:
            return ""
        return f"@{mention} " if mention in {"here", "everyone"} else f"{mention} "

    async def _library(self, interaction: discord.Interaction) -> PresetLibrary:
        guild = interaction.guild
        guild_presets = await self.store.list_guild_presets(guild.id) if guild else []
        return self.builtin.merged(guild_presets)

    # ------------------------------------------------------------- error paths

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, UserError):
            message = str(error)
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to do that."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "Timers only work inside a server."
        else:
            command = getattr(interaction.command, "qualified_name", "?")
            log.error("command /%s failed", command, exc_info=error)
            message = "Something went wrong on my side — the details are in the bot log."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("could not report a command error to the user")


async def setup(bot: "Aion2TimerBot") -> None:
    await bot.add_cog(TimerCog(bot))


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


def _clamp(text: str) -> str:
    return text[:100]


def _timer_label(timer: Timer, now: float, *, channel_mention: bool = False) -> str:
    parts = [f"**{timer.name}**"]
    if timer.remaining(now) <= 0:
        parts.append("due now")
    else:
        parts.append(discord.utils.format_dt(_dt(timer.due_at), "R"))
        parts.append(f"({discord.utils.format_dt(_dt(timer.due_at), 'f')})")
    if timer.is_repeating and timer.interval_seconds:
        parts.append(f"· every {format_timedelta(_seconds(timer.interval_seconds))}")
    if timer.note:
        parts.append(f"— {timer.note}")
    if channel_mention:
        parts.append(f"· <#{timer.channel_id}>")
    return " ".join(parts)


def _mention_label(mention: str) -> str:
    return f"@{mention}" if mention in {"here", "everyone"} else mention

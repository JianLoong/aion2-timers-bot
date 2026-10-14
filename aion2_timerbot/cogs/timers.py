"""Slash commands for setting, showing and cancelling Aion 2 timers."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ..db import DuplicateTimerError, Store
from ..duration import DurationError, format_timedelta, parse_duration
from ..models import EventBoard, EventReminder, Timer
from ..presets import Preset, PresetLibrary
from ..schedule import EventOccurrences, Occurrence, Schedule
from ..schedule_source import ScheduleSource
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

# Any guild channel that can hold a message. Discord's default "#general" is often a VOICE
# channel, so demanding a text channel rejects the obvious place to put a board. Announcement
# channels are `TextChannel` in discord.py, so they are already covered.
MessageableChannel = (
    discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread
)

# A reminder is still worth sending this long after its ideal moment (restart, slow tick).
REMINDER_GRACE = 120.0
# A board refreshes when its earliest listed event starts, but never less often than this...
BOARD_REFRESH_FLOOR = 900.0
# ...and never sits stale for longer than this.
BOARD_REFRESH_CAP = 3600.0


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
    reminder_group = app_commands.Group(
        name="reminder",
        description="Warn me before a scheduled event starts.",
        parent=timer_group,
    )

    def __init__(self, bot: "Aion2TimerBot") -> None:
        self.bot = bot
        self.config = bot.config
        self.store: Store = bot.store
        self.builtin: PresetLibrary = bot.presets
        self.source: ScheduleSource = bot.schedules
        # Guards board rendering: the periodic tick and a /timer board command can otherwise
        # both see "no message yet" and post the board twice.
        self._board_lock = asyncio.Lock()
        # One background tick drives everything: countdown timers first, then the event
        # board refresh and any reminders that have come due.
        self.scheduler = TimerScheduler(
            self.store,
            self._announce,
            tick_seconds=bot.config.tick_seconds,
            on_tick=self._tick_events,
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

    async def schedule_event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        schedule = await self.source.get()
        needle = current.casefold()
        now = time.time()
        choices: list[app_commands.Choice[str]] = []
        for event in schedule.sorted_events():
            if needle and needle not in event.name.casefold():
                continue
            upcoming = schedule.next_occurrence(event, now=now)
            label = event.name
            if upcoming is not None:
                label += f" · {discord.utils.format_dt(_dt(upcoming.start), 'R')}"
            choices.append(app_commands.Choice(name=_clamp(label), value=event.name))
        return choices[:25]

    async def reminder_event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        schedule = await self.source.get()
        needle = current.casefold()
        choices: list[app_commands.Choice[str]] = []
        for reminder in await self.store.list_event_reminders(guild.id):
            event = schedule.find(reminder.event_id)
            name = event.name if event else reminder.event_id
            if needle and needle not in name.casefold():
                continue
            label = f"{name} · {_minutes_phrase(reminder.minutes_before)}"
            choices.append(app_commands.Choice(name=_clamp(label), value=reminder.event_id))
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
        channel: MessageableChannel | None = None,
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
        channel: MessageableChannel | None = None,
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

    # -------------------------------------------------- scheduled events (board)

    @timer_group.command(
        name="board", description="Post a live board of the upcoming scheduled events."
    )
    @app_commands.describe(
        channel="Where the board goes. Defaults to this channel.",
        hours="How far ahead to list. Defaults to 24.",
        remove="Remove the board in that channel instead of posting it.",
    )
    async def timer_board(
        self,
        interaction: discord.Interaction,
        channel: MessageableChannel | None = None,
        hours: Literal[12, 24, 48] = 24,
        remove: bool = False,
    ) -> None:
        guild = self._require_guild(interaction)
        if not self._can_manage_channels(interaction):
            raise UserError(
                "You need the **Manage Channels** permission to place or remove a board."
            )
        target = channel or interaction.channel
        if target is None:
            raise UserError("I could not tell which channel to use — pass the `channel` option.")

        if remove:
            existing = await self.store.get_event_board(guild.id, target.id)
            if existing is None:
                raise UserError(f"There is no board in <#{target.id}> to remove.")
            await self.store.delete_event_board(guild.id, target.id)
            await self._delete_board_message(target, existing.message_id)
            await interaction.response.send_message(
                f"Removed the board in <#{target.id}>.", ephemeral=True
            )
            return

        board = await self.store.upsert_event_board(
            guild_id=guild.id,
            channel_id=target.id,
            hours=float(hours),
            created_by=interaction.user.id,
        )
        problem = await self._render_board_into(board, force=True)
        if problem is not None:
            raise UserError(problem)
        await interaction.response.send_message(
            f"The board is live in <#{target.id}>. I re-render it as each event passes, so the "
            "countdowns stay honest. `/timer board remove:True` takes it away again.",
            ephemeral=True,
        )

    @reminder_group.command(name="set", description="Get warned before a scheduled event starts.")
    @app_commands.describe(
        event="Which scheduled event.",
        minutes="How long before it starts. 0 warns as it starts.",
        channel="Where the warning goes. Defaults to this channel.",
        ping="Who to ping: here, everyone, or a role name or @mention.",
    )
    @app_commands.autocomplete(event=schedule_event_autocomplete)
    async def reminder_set(
        self,
        interaction: discord.Interaction,
        event: str,
        minutes: Literal[0, 2, 5, 10, 15] = 5,
        channel: MessageableChannel | None = None,
        ping: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        if not self._can_manage_channels(interaction):
            raise UserError("You need the **Manage Channels** permission to change reminders.")

        schedule = await self.source.get()
        found = schedule.find(event)
        if found is None:
            raise UserError(self._unknown_event(schedule, event))

        target = channel or interaction.channel
        if target is None:
            raise UserError("I could not tell which channel to use — pass the `channel` option.")
        mention = self._resolve_mention(guild, ping)

        reminder = await self.store.upsert_event_reminder(
            guild_id=guild.id,
            event_id=found.id,
            minutes_before=int(minutes),
            channel_id=target.id,
            mention=mention,
            created_by=interaction.user.id,
        )
        upcoming = schedule.next_occurrence(found, now=time.time())
        when = (
            f"next one {discord.utils.format_dt(_dt(upcoming.start), 'R')}"
            if upcoming is not None
            else "no upcoming time is known for it"
        )
        await interaction.response.send_message(
            f"Will warn you {_minutes_phrase(reminder.minutes_before)} **{found.name}**, in "
            f"<#{target.id}> — {when}.",
            ephemeral=True,
        )

    @reminder_group.command(name="off", description="Stop warning about an event.")
    @app_commands.describe(event="The event to stop warning about.")
    @app_commands.autocomplete(event=reminder_event_autocomplete)
    async def reminder_off(self, interaction: discord.Interaction, event: str) -> None:
        guild = self._require_guild(interaction)
        if not self._can_manage_channels(interaction):
            raise UserError("You need the **Manage Channels** permission to change reminders.")

        reminder = await self.store.get_event_reminder(guild.id, event)
        if reminder is None:
            schedule = await self.source.get()
            found = schedule.find(event)
            if found is not None:
                reminder = await self.store.get_event_reminder(guild.id, found.id)
        if reminder is None:
            raise UserError(f"No reminder is set for **{event}** in this server.")

        await self.store.delete_event_reminder(guild.id, reminder.event_id)
        name = await self._event_name(reminder.event_id)
        await interaction.response.send_message(
            f"Reminders for **{name}** are off.", ephemeral=True
        )

    @reminder_group.command(name="list", description="Show the event warnings set up here.")
    async def reminder_list(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        reminders = await self.store.list_event_reminders(guild.id)
        schedule = await self.source.get()

        embed = discord.Embed(title="🔔 Event reminders", colour=DONE_COLOUR)
        if not reminders:
            embed.description = "None yet. `/timer reminder set` adds one."
        else:
            for reminder in reminders:
                event = schedule.find(reminder.event_id)
                name = event.name if event else reminder.event_id
                icon = f"{event.icon} " if event and event.icon else ""
                details = f"{_minutes_phrase(reminder.minutes_before)} · in <#{reminder.channel_id}>"
                if reminder.mention:
                    details += f" · pings {_mention_label(reminder.mention)}"
                embed.add_field(name=f"{icon}{name}", value=details, inline=False)
        if self.source.problem:
            embed.set_footer(text=f"⚠️ {self.source.problem}"[:2048])
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
        channel = await self._channel(timer.channel_id)
        if channel is None:
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

    # ------------------------------------------------- scheduled event engine

    async def _tick_events(self, now: float | None = None) -> None:
        """Runs after every timer batch: refresh due boards, then fire due reminders."""
        moment = time.time() if now is None else now
        await self._refresh_boards(moment)
        await self._fire_reminders(moment)

    async def _refresh_boards(self, now: float) -> None:
        for board in await self.store.due_event_boards(now):
            problem = await self._render_board_into(board)
            if problem is not None:
                log.warning("board in channel %s: %s", board.channel_id, problem)

    async def _render_board_into(self, board: EventBoard, *, force: bool = False) -> str | None:
        """Post or edit one board's message. Returns something to show the user, or None.

        Serialised, and the row is re-read under the lock: a tick and a command racing here
        would both see "no message yet" and post the board twice.
        """
        moment = time.time()
        async with self._board_lock:
            current = await self.store.get_event_board(board.guild_id, board.channel_id)
            if current is None:
                return None  # removed while we were waiting for the lock
            if not force and current.refresh_at > moment:
                return None  # another render already covered this window
            board = current

            channel = await self._channel(board.channel_id)
            if channel is None:
                await self._defer_board(board)
                return f"I cannot reach the channel for this board (id {board.channel_id})."

            schedule = await self.source.get()
            embed, refresh_at = self._board_embed(schedule, board.hours, now=moment)

            message = await self._fetch_message(channel, board.message_id)
            if message is None:
                posted = await self._send_embed(channel, embed)
                message_id = getattr(posted, "id", None)
                if message_id is None:
                    await self._defer_board(board)
                    return f"I could not post in <#{board.channel_id}> — check my permissions there."
                await self.store.set_event_board_message(
                    board.guild_id,
                    board.channel_id,
                    message_id=int(message_id),
                    refresh_at=refresh_at,
                )
                return None

            if not await self._edit_embed(message, embed):
                await self._defer_board(board)
                return None
            await self.store.set_event_board_message(
                board.guild_id, board.channel_id, message_id=board.message_id, refresh_at=refresh_at
            )
            return None

    async def _defer_board(self, board: EventBoard) -> None:
        """Push the next refresh out, so a broken board is not retried every tick."""
        await self.store.upsert_event_board(
            guild_id=board.guild_id,
            channel_id=board.channel_id,
            hours=board.hours,
            created_by=board.created_by,
            refresh_at=time.time() + BOARD_REFRESH_FLOOR,
        )

    def _board_embed(
        self, schedule: Schedule, hours: float, *, now: float
    ) -> tuple[discord.Embed, float]:
        """The board contents, plus when it should next be re-rendered."""
        rows = schedule.board(now=now, hours=hours, per_event=3)
        embed = discord.Embed(title=f"⏳ Next up — next {int(hours)}h", colour=TIMER_COLOUR)

        lines: list[str] = []
        if self.source.problem:
            lines.append(f"⚠️ {self.source.problem}")
        if not rows:
            lines.append(
                "No schedule data yet. Set `SCHEDULE_URL`, or write your own events into "
                f"`{self.config.schedules_path}`."
            )
        lines.extend(_board_line(row) for row in rows)
        embed.description = "\n".join(lines)[:4000]

        footer = (
            f"Times are in your own timezone · {schedule.region.label} clock "
            f"({schedule.region.time_zone})"
        )
        if schedule.updated_at:
            footer += f" · schedule data {schedule.updated_at}"
        embed.set_footer(text=footer[:2048])

        starts = [occurrence.start for row in rows for occurrence in row.occurrences]
        refresh_at = min(starts) + 5.0 if starts else now + BOARD_REFRESH_FLOOR
        return embed, min(refresh_at, now + BOARD_REFRESH_CAP)

    async def _fire_reminders(self, now: float) -> None:
        reminders = await self.store.list_event_reminders()
        if not reminders:
            return
        schedule = await self.source.get()
        for reminder in reminders:
            event = schedule.find(reminder.event_id)
            if event is None:
                log.warning("reminder references an unknown event %r", reminder.event_id)
                continue
            await self._fire_reminder(reminder, event, schedule, now)

    async def _fire_reminder(
        self, reminder: EventReminder, event, schedule: Schedule, now: float
    ) -> None:
        lead = reminder.minutes_before * 60
        # Look slightly into the past as well, so a "when it starts" reminder still lands
        # on a 5-second tick instead of being skipped for pointing at last minute.
        candidates = schedule.next_occurrences(
            event, now=now - lead - REMINDER_GRACE, count=2
        )
        for occurrence in candidates:
            fire_at = occurrence.start - lead
            if fire_at > now or (now - fire_at) > REMINDER_GRACE:
                continue
            if reminder.last_fired_at == occurrence.start:
                continue  # already announced this occurrence (also what stops a restart repeating it)
            await self._announce_event(reminder, occurrence, event, schedule)
            await self.store.mark_event_reminder_fired(
                reminder.guild_id, reminder.event_id, started_at=occurrence.start
            )
            return

    async def _announce_event(
        self, reminder: EventReminder, occurrence: Occurrence, event, schedule: Schedule
    ) -> None:
        channel = await self._channel(reminder.channel_id)
        if channel is None:
            log.warning(
                "reminder for %s points at channel %s, which I cannot reach",
                event.name, reminder.channel_id,
            )
            return
        send = getattr(channel, "send", None)
        if send is None:
            return

        # Only the configured mention reaches the message text; the event's own name (which
        # can come from any override file) stays in the embed, where it cannot ping anyone.
        content = f"{self._mention_prefix(reminder.mention)}Event starting".strip()
        allowed = discord.AllowedMentions(
            everyone=reminder.mention == "everyone",
            roles=bool(reminder.mention and reminder.mention.startswith("<@&")),
            users=False,
        )
        try:
            await send(
                content=content,
                embed=self._reminder_embed(event, occurrence, schedule),
                allowed_mentions=allowed,
            )
        except discord.Forbidden:
            log.warning("no permission to post a reminder in channel %s", reminder.channel_id)
        except discord.HTTPException:
            log.warning("could not post a reminder for %s", event.name, exc_info=True)

    def _reminder_embed(self, event, occurrence: Occurrence, schedule: Schedule) -> discord.Embed:
        icon = f"{event.icon} " if event.icon else ""
        starts = _dt(occurrence.start)
        embed = discord.Embed(
            title=f"{icon}{event.name}",
            colour=DONE_COLOUR,
            description=(
                f"Starts {discord.utils.format_dt(starts, 'R')} "
                f"({discord.utils.format_dt(starts, 't')})."
            ),
        )
        if event.duration_minutes:
            length = f"about {format_timedelta(_seconds(event.duration_minutes * 60))}"
            if event.portal_minutes:
                length += f" · enter within the first {event.portal_minutes} min"
            embed.add_field(name="Length", value=length, inline=False)
        if event.description:
            embed.add_field(name="About", value=event.description[:300], inline=False)
        if self.source.problem:
            embed.set_footer(text=f"⚠️ {self.source.problem}"[:2048])
        return embed

    def _explain_channel_value(self, error: app_commands.TransformerError) -> str:
        """Explain a channel value Discord would not hand over.

        The picker offers every channel, but resolving one needs it in my cache, and a channel
        I have no View Channel permission for is not there — that is the usual cause.
        """
        value = error.value
        channel_id = getattr(value, "id", None)
        name = getattr(value, "name", None)
        label = f"**#{name}**" if name else f"`{channel_id if channel_id is not None else value}`"
        known = self.bot.get_channel(channel_id) if isinstance(channel_id, int) else None

        if known is None:
            return (
                f"I cannot see {label} at all, so I cannot post there. Give me "
                "**View Channel** and **Send Messages** in that channel (or in its category), "
                "then try again."
            )
        return (
            f"I cannot post in {label} — it is {_channel_kind(known)}. Pick a text, "
            "announcement, voice or stage channel."
        )

    @staticmethod
    def _unknown_event(schedule: Schedule, name: str) -> str:
        known = schedule.names()
        if not known:
            return (
                "I have no schedule data yet, so I don't know any events. Check the log — "
                "`SCHEDULE_URL` may be unreachable and no cached copy exists."
            )
        sample = ", ".join(f"`{event}`" for event in known[:8])
        return f"I don't know an event called **{name}**. Known events: {sample}."

    async def _event_name(self, event_id: str) -> str:
        schedule = await self.source.get()
        event = schedule.find(event_id)
        return event.name if event is not None else event_id

    # --------------------------------------------------------- channel helpers

    async def _channel(self, channel_id: int):
        """The channel object to post in, or None if it is gone or unreachable."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("channel %s is unreachable", channel_id)
                return None
        return channel if hasattr(channel, "send") else None

    async def _fetch_message(self, channel, message_id: int | None):
        if message_id is None:
            return None
        fetch = getattr(channel, "fetch_message", None)
        if fetch is None:
            return None
        try:
            return await fetch(message_id)
        except discord.HTTPException:
            return None

    async def _send_embed(self, channel, embed: discord.Embed):
        send = getattr(channel, "send", None)
        if send is None:
            return None
        try:
            return await send(embed=embed)
        except discord.HTTPException:
            log.warning("could not post a message in channel %s", getattr(channel, "id", "?"))
            return None

    async def _edit_embed(self, message, embed: discord.Embed) -> bool:
        edit = getattr(message, "edit", None)
        if edit is None:
            return False
        try:
            await edit(embed=embed)
        except discord.HTTPException:
            log.warning("could not edit a board message", exc_info=True)
            return False
        return True

    async def _delete_board_message(self, channel, message_id: int | None) -> None:
        message = await self._fetch_message(channel, message_id)
        delete = getattr(message, "delete", None)
        if delete is None:
            return
        try:
            await delete()
        except discord.HTTPException:
            log.warning("could not delete a board message", exc_info=True)

    # ------------------------------------------------------------- error paths

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, UserError):
            message = str(error)
        elif isinstance(error, app_commands.TransformerError):
            message = self._explain_channel_value(error)
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


def _minutes_phrase(minutes: int) -> str:
    return "when it starts" if not minutes else f"{minutes} min before"


def _channel_kind(channel) -> str:
    """Describe a channel that cannot hold a board, in words worth reading."""
    if isinstance(channel, discord.CategoryChannel):
        return "a category, which groups channels rather than holding messages"
    if isinstance(channel, discord.ForumChannel):
        return "a forum, which wants posts rather than one message"
    return "a kind of channel I cannot post in"


def _board_line(row: EventOccurrences) -> str:
    """One event on the board: the next start, its window, then any further ones."""
    event = row.event
    icon = f"{event.icon} " if event.icon else ""
    label = f" `{row.occurrences[0].label}`" if row.occurrences[0].label else ""
    first = row.occurrences[0]

    if row.later:
        # Nothing in the window: give the date as well, so a quiet board still shows it.
        when = (
            f"{discord.utils.format_dt(_dt(first.start), 'f')} "
            f"({discord.utils.format_dt(_dt(first.start), 'R')})"
        )
    else:
        when = (
            f"{discord.utils.format_dt(_dt(first.start), 't')}"
            f"–{discord.utils.format_dt(_dt(first.end), 't')} "
            f"({discord.utils.format_dt(_dt(first.start), 'R')})"
        )

    rest = " · ".join(
        discord.utils.format_dt(_dt(occurrence.start), "R") for occurrence in row.occurrences[1:]
    )
    tail = f" · then {rest}" if rest else ""
    return f"{icon}**{event.name}**{label} — {when}{tail}"

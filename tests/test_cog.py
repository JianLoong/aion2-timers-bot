"""The command layer, driven through fake Discord objects (no gateway needed)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import get_args, get_type_hints
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

from aion2_timerbot.cogs.timers import TimerCog, UserError
from aion2_timerbot.config import Config
from aion2_timerbot.db import Store
from aion2_timerbot.presets import BUILTIN_PATH, load_library
from aion2_timerbot.schedule_source import ScheduleSource
from bot import Aion2TimerBot
from tests.conftest import (
    FakeChannel,
    FakeGuild,
    FakeHTTPResponse,
    FakeInteraction,
    FakeRole,
    FakeUser,
)

MANAGE_CHANNELS = discord.Permissions(manage_channels=True)
MANAGE_GUILD = discord.Permissions(manage_guild=True)


def _cog(bot) -> TimerCog:
    return bot.get_cog("TimerCog")


def _command(tree, *path: str):
    """Tree.get_command only takes one name, so walk groups by hand."""
    current = tree.get_command(path[0])
    for name in path[1:]:
        if current is None:
            return None
        current = current.get_command(name)
    return current


async def _invoke(cog, command_name: str, interaction, **kwargs):
    """Call a command the way discord.py would for a cog-bound command."""
    command = getattr(cog, command_name)
    return await command.callback(cog, interaction, **kwargs)


async def _expect_user_error(cog, interaction, command_name: str, **kwargs) -> str:
    """Run a command, push its failure through the error handler, return the text."""
    try:
        await _invoke(cog, command_name, interaction, **kwargs)
    except UserError as error:
        await cog.cog_app_command_error(interaction, error)
        return str(error)
    raise AssertionError(f"{command_name} was expected to fail")


# --------------------------------------------------------------- registration


async def test_commands_are_registered_under_timer(bot):
    tree = bot.tree
    for path in [
        ("timer",),
        ("timer", "set"),
        ("timer", "list"),
        ("timer", "cancel"),
        ("timer", "preset"),
        ("timer", "preset", "list"),
        ("timer", "preset", "start"),
        ("timer", "preset", "add"),
        ("timer", "preset", "remove"),
    ]:
        assert _command(tree, *path) is not None, f"/{' '.join(path)} is missing"

    group = _command(tree, "timer")
    assert group.guild_only is True
    assert _command(tree, "timer", "set").description


async def test_autocomplete_is_wired_to_the_right_options(bot):
    set_command = _command(bot.tree, "timer", "set")
    cancel = _command(bot.tree, "timer", "cancel")
    start = _command(bot.tree, "timer", "preset", "start")

    assert cancel._params["name"].autocomplete is not None
    assert start._params["preset"].autocomplete is not None
    assert set_command._params["name"].autocomplete is None


async def test_the_setup_entry_point_registers_the_cog(tmp_path):
    """The real loader path a deployed bot takes at startup."""
    store = Store(tmp_path / "extension.db")
    await store.open()
    player = Aion2TimerBot(
        Config(token="test-token", db_path=store.path),
        store,
        load_library(BUILTIN_PATH),
        ScheduleSource(region="global-as", url=""),
    )
    await player.load_extension("aion2_timerbot.cogs.timers")
    try:
        assert player.get_cog("TimerCog") is not None
        assert _command(player.tree, "timer", "set") is not None
        assert _command(player.tree, "timer", "preset", "start") is not None
    finally:
        cog = player.get_cog("TimerCog")
        if cog is not None:
            await cog.cog_unload()
        await store.close()


# -------------------------------------------------------------- /timer set


async def test_set_creates_a_one_shot_timer(bot):
    cog = _cog(bot)
    channel = FakeChannel(777)
    interaction = FakeInteraction(channel=channel)

    await _invoke(cog, "timer_set", interaction, name="World Boss", duration="2h30m")

    timers = await cog.store.list_timers(interaction.guild.id)
    assert len(timers) == 1
    timer = timers[0]
    assert timer.name == "World Boss"
    assert timer.channel_id == 777
    assert timer.interval_seconds is None
    assert timer.created_by == interaction.user.id
    assert timer.remaining() == pytest.approx(9000, abs=2)
    assert interaction.response.messages[0]["ephemeral"] is True
    assert "World Boss" in interaction.response.messages[0]["embed"].description


async def test_set_with_repeat_stores_the_interval(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    await _invoke(cog, "timer_set", interaction, name="Rift", duration="45m", repeat=True)

    timer = (await cog.store.list_timers(interaction.guild.id))[0]
    assert timer.is_repeating
    assert timer.interval_seconds == 2700


async def test_set_posts_to_the_chosen_channel_and_ping(bot):
    cog = _cog(bot)
    guild = FakeGuild(roles=[FakeRole(4242, "Officers")])
    interaction = FakeInteraction(guild=guild)
    elsewhere = FakeChannel(999)

    await _invoke(
        cog,
        "timer_set",
        interaction,
        name="Siege",
        duration="1d",
        ping="Officers",
        note="be online",
        channel=elsewhere,
    )

    timer = (await cog.store.list_timers(guild.id))[0]
    assert timer.channel_id == 999
    assert timer.mention == "<@&4242>"
    assert timer.note == "be online"


@pytest.mark.parametrize(("ping", "expected"), [("here", "here"), ("@everyone", "everyone"), ("off", None), ("", None)])
async def test_set_normalises_the_ping(bot, ping, expected):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name=f"T{ping}", duration="5m", ping=ping)
    timer = await cog.store.get_timer_by_name(interaction.guild.id, f"T{ping}")
    assert timer is not None
    assert timer.mention == expected


async def test_set_rejects_an_unknown_role(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(guild=FakeGuild(roles=[FakeRole(1, "Officers")]))

    message = await _expect_user_error(
        cog, interaction, "timer_set", name="Boss", duration="1h", ping="Raiders"
    )
    assert "Raiders" in message
    assert await cog.store.list_timers(interaction.guild.id) == []
    assert interaction.response.last["ephemeral"] is True


async def test_set_reports_a_bad_duration(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    message = await _expect_user_error(
        cog, interaction, "timer_set", name="Boss", duration="whenever"
    )
    assert "duration" in message.lower()
    assert await cog.store.list_timers(interaction.guild.id) == []


async def test_set_refuses_a_duplicate_unless_replacing(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="Boss", duration="1h")

    message = await _expect_user_error(cog, interaction, "timer_set", name="boss", duration="2h")
    assert "replace" in message
    assert len(await cog.store.list_timers(interaction.guild.id)) == 1

    await _invoke(cog, "timer_set", interaction, name="boss", duration="2h", replace=True)
    timers = await cog.store.list_timers(interaction.guild.id)
    assert len(timers) == 1
    assert timers[0].remaining() == pytest.approx(7200, abs=2)


async def test_set_honours_the_per_guild_cap(bot):
    cog = _cog(bot)
    cog.config.max_timers_per_guild = 1
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="First", duration="1h")

    message = await _expect_user_error(cog, interaction, "timer_set", name="Second", duration="1h")
    assert "1" in message


async def test_set_needs_a_name(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _expect_user_error(cog, interaction, "timer_set", name="   ", duration="1h")


async def test_set_outside_a_guild_is_refused(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(guild=None)

    message = await _expect_user_error(cog, interaction, "timer_set", name="Boss", duration="1h")
    assert "server" in message


# ------------------------------------------------------ /timer list, cancel


async def test_list_shows_what_is_running(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="World Boss", duration="4h", repeat=True, note="wb")

    await _invoke(cog, "timer_list", interaction)
    embed = interaction.response.messages[-1]["embed"]
    assert "(1)" in embed.title
    assert "World Boss" in embed.description
    assert "every 4h" in embed.description


async def test_empty_list_explains_how_to_start(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_list", interaction)
    embed = interaction.response.messages[-1]["embed"]
    assert "/timer set" in embed.description


async def test_the_creator_can_cancel(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="Boss", duration="1h")

    await _invoke(cog, "timer_cancel", interaction, name="boss")
    assert await cog.store.list_timers(interaction.guild.id) == []
    assert "Boss" in interaction.response.messages[-1]["content"]


async def test_other_members_cannot_cancel_someone_elses_timer(bot):
    cog = _cog(bot)
    owner = FakeInteraction()
    await _invoke(cog, "timer_set", owner, name="Boss", duration="1h")

    stranger = FakeInteraction(user=FakeUser(2))
    message = await _expect_user_error(cog, stranger, "timer_cancel", name="Boss")
    assert "manage channels" in message
    assert len(await cog.store.list_timers(owner.guild.id)) == 1


async def test_a_moderator_can_cancel_anyone_timer(bot):
    cog = _cog(bot)
    owner = FakeInteraction()
    await _invoke(cog, "timer_set", owner, name="Boss", duration="1h")

    moderator = FakeInteraction(user=FakeUser(3, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "timer_cancel", moderator, name="Boss")
    assert await cog.store.list_timers(owner.guild.id) == []


async def test_cancelling_something_that_is_not_running_is_a_clear_error(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    message = await _expect_user_error(cog, interaction, "timer_cancel", name="Ghost")
    assert "Ghost" in message


async def test_cancel_autocomplete_lists_running_timers(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="World Boss", duration="4h")

    choices = await cog.timer_name_autocomplete(interaction, "")
    assert [c.value for c in choices] == ["World Boss"]
    assert "World Boss" in choices[0].name
    assert await cog.timer_name_autocomplete(interaction, "zzz") == []
    assert await cog.timer_name_autocomplete(FakeInteraction(guild=None), "") == []


# ------------------------------------------------------------- /timer preset


async def test_preset_list_groups_by_category(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "preset_list", interaction)

    embed = interaction.response.messages[-1]["embed"]
    names = [field.name for field in embed.fields]
    assert "Reset" in names
    assert any("World Boss" in field.value for field in embed.fields)


async def test_preset_list_filters_by_category(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "preset_list", interaction, category="reset")
    embed = interaction.response.messages[-1]["embed"]
    assert [field.name for field in embed.fields] == ["Reset"]

    await _expect_user_error(cog, interaction, "preset_list", category="nope")


async def test_preset_start_creates_a_repeating_timer(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    await _invoke(cog, "preset_start", interaction, preset="World Boss")

    timer = (await cog.store.list_timers(interaction.guild.id))[0]
    assert timer.name == "World Boss"
    assert timer.interval_seconds == 4 * 3600
    assert timer.remaining() == pytest.approx(4 * 3600, abs=2)


async def test_preset_start_accepts_an_alias_and_a_delay(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    await _invoke(cog, "preset_start", interaction, preset="wb", delay="20m", repeat=False)

    timer = (await cog.store.list_timers(interaction.guild.id))[0]
    assert timer.name == "World Boss"
    assert timer.interval_seconds is None
    assert timer.remaining() == pytest.approx(1200, abs=2)


async def test_preset_start_rejects_an_unknown_preset(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    message = await _expect_user_error(cog, interaction, "preset_start", preset="Ancient Dragon")
    assert "Ancient Dragon" in message
    assert await cog.store.list_timers(interaction.guild.id) == []


async def test_preset_add_needs_manage_server(bot):
    cog = _cog(bot)
    member = FakeInteraction(user=FakeUser(2))
    message = await _expect_user_error(
        cog, member, "preset_add", name="Ancient Boss", interval="3h"
    )
    assert "Manage Server" in message


async def test_preset_add_then_use_then_remove(bot):
    cog = _cog(bot)
    admin = FakeInteraction(user=FakeUser(9, permissions=MANAGE_GUILD))

    await _invoke(
        cog,
        "preset_add",
        admin,
        name="Ancient Boss",
        interval="3h30m",
        category="Open world",
        aliases="ab",
        note="spawns in the rift",
    )
    assert "Ancient Boss" in admin.response.messages[-1]["content"]

    library = await cog._library(admin)
    assert library.get("ab").interval_seconds == 3 * 3600 + 30 * 60
    assert library.get("ab").source == "guild"

    await _invoke(cog, "preset_start", admin, preset="Ancient Boss")
    timer = (await cog.store.list_timers(admin.guild.id))[0]
    assert timer.interval_seconds == 3 * 3600 + 30 * 60

    await _invoke(cog, "preset_remove", admin, name="Ancient Boss")
    assert await cog.store.list_guild_presets(admin.guild.id) == []
    assert (await cog._library(admin)).get("Ancient Boss") is None
    # the built-in presets are still there
    assert (await cog._library(admin)).get("World Boss").source == "builtin"


async def test_preset_add_validates_its_input(bot):
    cog = _cog(bot)
    admin = FakeInteraction(user=FakeUser(9, permissions=MANAGE_GUILD))
    await _expect_user_error(cog, admin, "preset_add", name="Boss", interval="sometimes")
    await _expect_user_error(cog, admin, "preset_add", name="", interval="1h")


async def test_preset_remove_leaves_builtins_alone(bot):
    cog = _cog(bot)
    admin = FakeInteraction(user=FakeUser(9, permissions=MANAGE_GUILD))
    message = await _expect_user_error(cog, admin, "preset_remove", name="World Boss")
    assert "built-in" in message or "presets.json" in message
    assert (await cog._library(admin)).get("World Boss") is not None


async def test_preset_autocomplete_marks_server_presets(bot):
    cog = _cog(bot)
    admin = FakeInteraction(user=FakeUser(9, permissions=MANAGE_GUILD))
    await _invoke(cog, "preset_add", admin, name="Ancient Boss", interval="1h")

    choices = await cog.preset_name_autocomplete(admin, "ancient")
    assert [c.value for c in choices] == ["Ancient Boss"]
    assert "(server)" in choices[0].name

    assert [c.value for c in await cog.guild_preset_autocomplete(admin, "")] == ["Ancient Boss"]
    assert await cog.guild_preset_autocomplete(admin, "world") == []


# ------------------------------------------------------------ announcements


async def test_announcement_shows_the_name_and_pings_the_channel(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(321)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel if channel_id == 321 else None)

    interaction = FakeInteraction(channel=FakeChannel(321))
    await _invoke(
        cog,
        "timer_set",
        interaction,
        name="World Boss",
        duration="1h",
        repeat=True,
        note="hit it",
        ping="here",
    )
    timer = (await cog.store.list_timers(interaction.guild.id))[0]
    timer.due_at = time.time() - 5

    await cog._announce(timer, 5.0)

    sent = channel.sent[-1]
    assert sent["content"] == "@here Timer finished"
    assert "World Boss is up!" in sent["embed"].title
    assert sent["allowed_mentions"].everyone is False
    assert sent["allowed_mentions"].roles is False


async def test_a_timer_name_cannot_smuggle_a_ping_into_the_message(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(321)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    interaction = FakeInteraction(channel=FakeChannel(321))
    await _invoke(cog, "timer_set", interaction, name="@everyone NOW", duration="1h", ping="here")
    timer = (await cog.store.list_timers(interaction.guild.id))[0]

    await cog._announce(timer, 0.0)

    sent = channel.sent[-1]
    assert "@everyone" not in sent["content"]
    assert sent["content"] == "@here Timer finished"
    assert sent["allowed_mentions"].everyone is False
    assert "@everyone NOW" in sent["embed"].title


async def test_everyone_ping_is_allowed_only_when_asked_for(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(321)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    interaction = FakeInteraction(channel=FakeChannel(321))
    await _invoke(cog, "timer_set", interaction, name="Siege", duration="1h", ping="everyone")
    timer = (await cog.store.list_timers(interaction.guild.id))[0]

    await cog._announce(timer, 0.0)

    sent = channel.sent[-1]
    assert sent["content"] == "@everyone Timer finished"
    assert sent["allowed_mentions"].everyone is True


async def test_a_late_timer_says_so(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(321)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    interaction = FakeInteraction(channel=FakeChannel(321))
    await _invoke(cog, "timer_set", interaction, name="Boss", duration="1h")
    timer = (await cog.store.list_timers(interaction.guild.id))[0]

    await cog._announce(timer, 3600.0)
    fields = {field.name for field in channel.sent[-1]["embed"].fields}
    assert "Late" in fields


async def test_announcing_into_a_vanished_channel_is_survivable(bot, monkeypatch):
    cog = _cog(bot)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: None)

    async def no_fetch(channel_id):
        raise discord.NotFound(FakeHTTPResponse(404, "Not Found"), "unknown channel")

    monkeypatch.setattr(bot, "fetch_channel", no_fetch)

    interaction = FakeInteraction()
    await _invoke(cog, "timer_set", interaction, name="Boss", duration="1h")
    timer = (await cog.store.list_timers(interaction.guild.id))[0]

    await cog._announce(timer, 0.0)  # must not raise


async def test_the_running_bot_fires_a_due_timer(bot, monkeypatch):
    """End to end: a due timer is announced and then gone."""
    cog = _cog(bot)
    channel = FakeChannel(321)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)

    interaction = FakeInteraction(channel=FakeChannel(321))
    await _invoke(cog, "timer_set", interaction, name="World Boss", duration="1h")
    timer = (await cog.store.list_timers(interaction.guild.id))[0]

    fired = await cog.scheduler.run_once(now=timer.due_at + 0.5)

    assert [t.name for t in fired] == ["World Boss"]
    assert "World Boss is up!" in channel.sent[-1]["embed"].title
    assert await cog.store.list_timers(interaction.guild.id) == []


# ------------------------------------------------------------- error handler


async def test_unexpected_errors_do_not_leak_internals(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    await cog.cog_app_command_error(interaction, RuntimeError("secret stack detail"))

    message = interaction.response.last["content"]
    assert "secret stack detail" not in message
    assert interaction.response.last["ephemeral"] is True


async def test_error_after_a_response_uses_followup(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    await _invoke(cog, "timer_list", interaction)

    await cog.cog_app_command_error(interaction, UserError("too late"))

    assert interaction.followup.messages[-1]["content"] == "too late"
    assert interaction.followup.messages[-1]["ephemeral"] is True


async def test_missing_permission_and_dm_errors_are_explained(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    await cog.cog_app_command_error(interaction, app_commands.NoPrivateMessage())
    assert "server" in interaction.response.last["content"]


async def test_missing_permissions_are_explained(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()
    missing = discord.Permissions(manage_guild=True)
    assert missing.manage_guild  # the fixture provides the permission the error denies
    await cog.cog_app_command_error(interaction, app_commands.MissingPermissions(["manage_guild"]))
    assert "permission" in interaction.response.last["content"].lower()


# ------------------------------------------------------------- /timer board

def test_channel_options_accept_any_channel_that_can_hold_a_message():
    """Regression: Discord's default "#general" is often a VOICE channel.

    A TextChannel-only option made Discord reject it before our code ever ran:
    "Failed to convert general to TextChannel".
    """
    for name in ("timer_set", "timer_board", "preset_start", "reminder_set"):
        command = TimerCog.__dict__[name]
        allowed = get_args(get_type_hints(command.callback)["channel"])
        for expected in (
            discord.TextChannel,
            discord.VoiceChannel,
            discord.StageChannel,
            discord.Thread,
        ):
            assert expected in allowed, f"/timer {name} would refuse a {expected.__name__}"


KST = ZoneInfo("Asia/Seoul")


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=KST).timestamp()


def _staff(bot) -> FakeInteraction:
    """An interaction from someone who may manage channels, in a channel we can post to."""
    return FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))


def _board_channel(bot, monkeypatch, channel_id: int = 700):
    """One channel object, used both as the interaction's channel and by the bot's lookup.

    Two fakes with the same id would hide the fact that posting and fetching messages need
    to hit the same object.
    """
    channel = FakeChannel(channel_id)
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel if cid == channel_id else None)
    interaction = FakeInteraction(
        channel=channel, user=FakeUser(1, permissions=MANAGE_CHANNELS)
    )
    return channel, interaction


async def _next_start(cog, event_id: str, now: float):
    schedule = await cog.source.get()
    event = schedule.find(event_id)
    return schedule.next_occurrence(event, now=now)


async def test_board_posts_a_live_embed(bot, monkeypatch):
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)

    await _invoke(cog, "timer_board", interaction)

    board = await cog.store.get_event_board(interaction.guild.id, 700)
    assert board is not None
    assert board.message_id is not None
    assert board.refresh_at > time.time()  # scheduled, not left due

    embed = channel.messages[board.message_id].embed
    assert "Hourly Boss" in embed.description
    assert "Weekly Siege" in embed.description
    assert embed.description.count("<t:") >= 3  # live countdowns Discord renders per viewer
    assert "Asia/Seoul" in embed.footer.text
    assert "next 24h" in embed.title
    assert interaction.response.last["ephemeral"] is True


async def test_the_board_option_offers_voice_channels_to_discord(bot):
    """What Discord is told to offer in the picker — the payload, not just the annotation."""
    options = _command(bot.tree, "timer", "board").to_dict(bot.tree)["options"]
    option = next(entry for entry in options if entry["name"] == "channel")

    assert option["type"] == discord.AppCommandOptionType.channel.value
    offered = set(option["channel_types"])
    assert {
        discord.ChannelType.text.value,
        discord.ChannelType.voice.value,
        discord.ChannelType.stage_voice.value,
    } <= offered


async def test_board_needs_manage_channels(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    message = await _expect_user_error(cog, interaction, "timer_board")

    assert "Manage Channels" in message
    assert await cog.store.list_event_boards(interaction.guild.id) == []


async def test_a_channel_discord_cannot_hand_over_is_explained(bot):
    """If Discord refuses a value, say so in words instead of "something went wrong"."""
    cog = _cog(bot)
    interaction = FakeInteraction()
    param = _command(bot.tree, "timer", "board")._params["channel"]

    await cog.cog_app_command_error(
        interaction,
        app_commands.TransformerError(
            "general", discord.AppCommandOptionType.channel, param._annotation
        ),
    )

    message = interaction.response.last["content"]
    assert "channel" in message.lower()
    assert "Something went wrong" not in message


async def test_board_reports_a_channel_it_cannot_post_in(bot, monkeypatch):
    cog = _cog(bot)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: None)

    async def gone(channel_id):
        raise discord.NotFound(FakeHTTPResponse(404, "Not Found"), "unknown channel")

    monkeypatch.setattr(bot, "fetch_channel", gone)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))

    message = await _expect_user_error(cog, interaction, "timer_board")

    assert "reach" in message or "post" in message


async def test_board_shows_a_schedule_problem_to_the_reader(bot, monkeypatch):
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)
    cog.source.last_error = "could not fetch https://shugo.gg/timers.json: dns is down"

    await _invoke(cog, "timer_board", interaction)

    board = await cog.store.get_event_board(interaction.guild.id, 700)
    embed = channel.messages[board.message_id].embed
    assert "⚠️" in embed.description
    assert "dns is down" in embed.description


async def test_re_running_board_edits_the_same_message(bot, monkeypatch):
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)

    await _invoke(cog, "timer_board", interaction)
    first = await cog.store.get_event_board(interaction.guild.id, 700)

    await _invoke(cog, "timer_board", interaction, hours=12)

    board = await cog.store.get_event_board(interaction.guild.id, 700)
    assert board.message_id == first.message_id
    assert board.hours == 12
    assert len(channel.messages) == 1  # one board message, never a second
    assert len(channel.messages[board.message_id].edits) >= 1
    assert "next 12h" in channel.messages[board.message_id].embed.title


async def test_board_remove_deletes_the_message(bot, monkeypatch):
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)
    await _invoke(cog, "timer_board", interaction)
    board = await cog.store.get_event_board(interaction.guild.id, 700)

    await _invoke(cog, "timer_board", interaction, remove=True)

    assert await cog.store.get_event_board(interaction.guild.id, 700) is None
    assert channel.messages[board.message_id].deleted is True


async def test_removing_a_board_that_is_not_there_is_explained(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))

    message = await _expect_user_error(cog, interaction, "timer_board", remove=True)

    assert "no board" in message.lower()


async def test_the_tick_refreshes_a_due_board(bot, monkeypatch):
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)
    await _invoke(cog, "timer_board", interaction)
    board = await cog.store.get_event_board(interaction.guild.id, 700)
    edits_before = len(channel.messages[board.message_id].edits)

    # make it due, as it would be once its earliest event has started
    await cog.store.set_event_board_message(
        interaction.guild.id, 700, message_id=board.message_id, refresh_at=0.0
    )
    await cog._tick_events(now=time.time())

    assert len(channel.messages[board.message_id].edits) > edits_before
    refreshed = await cog.store.get_event_board(interaction.guild.id, 700)
    assert refreshed.refresh_at > time.time()


async def test_a_board_is_not_posted_twice_when_the_tick_and_a_command_race(bot, monkeypatch):
    """The tick and /timer board both render; only one message may come out of it."""
    cog = _cog(bot)
    channel, interaction = _board_channel(bot, monkeypatch)

    await _invoke(cog, "timer_board", interaction)
    await cog._tick_events(now=time.time())  # the background tick, right after

    assert len(channel.messages) == 1
    board = await cog.store.get_event_board(interaction.guild.id, 700)
    assert board.message_id in channel.messages


async def test_a_board_whose_channel_vanished_is_backed_off(bot, monkeypatch):
    cog = _cog(bot)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: None)

    async def gone(channel_id):
        raise discord.NotFound(FakeHTTPResponse(404, "Not Found"), "unknown channel")

    monkeypatch.setattr(bot, "fetch_channel", gone)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await cog.store.upsert_event_board(
        guild_id=interaction.guild.id, channel_id=999, hours=24, created_by=1, refresh_at=0.0
    )

    await cog._tick_events(now=time.time())  # must not raise

    board = await cog.store.get_event_board(interaction.guild.id, 999)
    assert board.refresh_at > time.time()  # retried later, not every tick


# ---------------------------------------------------------- /timer reminder


async def test_reminder_set_and_list(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))

    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=5)

    reminder = await cog.store.get_event_reminder(interaction.guild.id, "hourly-boss")
    assert reminder.minutes_before == 5
    assert reminder.channel_id == interaction.channel.id
    assert "5 min before" in interaction.response.last["content"]

    await _invoke(cog, "reminder_list", interaction)
    embed = interaction.response.messages[-1]["embed"]
    assert "Hourly Boss" in embed.fields[0].name
    assert "5 min before" in embed.fields[0].value


async def test_reminder_set_needs_manage_channels(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    message = await _expect_user_error(cog, interaction, "reminder_set", event="Hourly Boss")

    assert "Manage Channels" in message
    assert await cog.store.list_event_reminders(interaction.guild.id) == []


async def test_reminder_set_names_the_events_it_knows(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))

    message = await _expect_user_error(cog, interaction, "reminder_set", event="Ancient Dragon")

    assert "Ancient Dragon" in message
    assert "Hourly Boss" in message and "Weekly Siege" in message


async def test_reminder_fires_at_the_lead_time_and_only_once(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    interaction = FakeInteraction(channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=5, ping="here")

    now = _at(2026, 10, 14, 12, 0)
    occurrence = await _next_start(cog, "hourly-boss", now)
    lead = 5 * 60

    await cog._fire_reminders(now=occurrence.start - lead - 60)  # an hour before: nothing
    assert channel.sent == []

    await cog._fire_reminders(now=occurrence.start - lead + 1)
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent["content"] == "@here Event starting"
    assert "Hourly Boss" in sent["embed"].title
    assert "Starts" in sent["embed"].description
    assert sent["allowed_mentions"].everyone is False  # "here" is not everyone

    await cog._fire_reminders(now=occurrence.start - lead + 6)  # the next tick
    assert len(channel.sent) == 1  # not sent twice

    reminder = await cog.store.get_event_reminder(interaction.guild.id, "hourly-boss")
    assert reminder.last_fired_at == occurrence.start


async def test_a_reminder_survives_a_restart_without_repeating(bot, monkeypatch):
    """The dedupe lives in the database, not in the cog's memory."""
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    interaction = FakeInteraction(channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=5)

    now = _at(2026, 10, 14, 12, 0)
    occurrence = await _next_start(cog, "hourly-boss", now)
    fire_time = occurrence.start - 5 * 60 + 1

    await cog._fire_reminders(now=fire_time)
    assert len(channel.sent) == 1

    restarted = TimerCog(bot)  # a fresh cog over the same database
    await restarted._fire_reminders(now=fire_time + 2)

    assert len(channel.sent) == 1


async def test_a_reminder_for_the_next_occurrence_fires_again(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    interaction = FakeInteraction(channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=0)

    now = _at(2026, 10, 14, 12, 0)
    first = await _next_start(cog, "hourly-boss", now)

    await cog._fire_reminders(now=first.start + 2)
    assert len(channel.sent) == 1

    # the following one, an hour later
    await cog._fire_reminders(now=first.start + 3600 + 2)
    assert len(channel.sent) == 2


async def test_a_when_it_starts_reminder_is_not_skipped(bot, monkeypatch):
    """Regression: with a 0-minute lead the occurrence lookup used to miss it entirely."""
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    interaction = FakeInteraction(channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=0)

    now = _at(2026, 10, 14, 12, 0)
    occurrence = await _next_start(cog, "hourly-boss", now)

    await cog._fire_reminders(now=occurrence.start + 3)

    assert len(channel.sent) == 1


async def test_a_reminder_missed_by_a_long_outage_is_dropped(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    interaction = FakeInteraction(channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=5)

    now = _at(2026, 10, 14, 12, 0)
    occurrence = await _next_start(cog, "hourly-boss", now)

    await cog._fire_reminders(now=occurrence.start + 600)  # ten minutes after it began

    assert channel.sent == []


async def test_a_reminder_only_pings_what_it_was_told_to(bot, monkeypatch):
    cog = _cog(bot)
    channel = FakeChannel(800)
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    guild = FakeGuild(roles=[FakeRole(4242, "Officers")])
    interaction = FakeInteraction(guild=guild, channel=FakeChannel(800), user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=0, ping="Officers")

    now = _at(2026, 10, 14, 12, 0)
    occurrence = await _next_start(cog, "hourly-boss", now)
    await cog._fire_reminders(now=occurrence.start + 2)

    sent = channel.sent[-1]
    assert sent["content"] == "<@&4242> Event starting"
    assert sent["allowed_mentions"].roles is True
    assert sent["allowed_mentions"].everyone is False


async def test_reminder_off(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Hourly Boss", minutes=5)

    await _invoke(cog, "reminder_off", interaction, event="hourly-boss")

    assert await cog.store.list_event_reminders(interaction.guild.id) == []
    assert "Hourly Boss" in interaction.response.last["content"]


async def test_reminder_off_for_something_not_set_is_explained(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))

    message = await _expect_user_error(cog, interaction, "reminder_off", event="Hourly Boss")

    assert "No reminder" in message


async def test_event_autocomplete_offers_the_schedule(bot):
    cog = _cog(bot)
    interaction = FakeInteraction()

    choices = await cog.schedule_event_autocomplete(interaction, "hourly")

    assert [choice.value for choice in choices] == ["Hourly Boss"]
    assert await cog.schedule_event_autocomplete(interaction, "zzz") == []


async def test_reminder_autocomplete_shows_what_is_on(bot):
    cog = _cog(bot)
    interaction = FakeInteraction(user=FakeUser(1, permissions=MANAGE_CHANNELS))
    await _invoke(cog, "reminder_set", interaction, event="Weekly Siege", minutes=15)

    choices = await cog.reminder_event_autocomplete(interaction, "")

    assert [choice.value for choice in choices] == ["weekly-siege"]
    assert "Weekly Siege" in choices[0].name
    assert "15 min before" in choices[0].name

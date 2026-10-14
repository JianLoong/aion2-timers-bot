import time

import pytest

from aion2_timerbot.db import DuplicateTimerError, Store


async def test_add_get_and_list_in_due_order(store):
    later = await store.add_timer(
        guild_id=1, channel_id=10, name="Siege", due_at=5000.0, created_by=7
    )
    sooner = await store.add_timer(
        guild_id=1, channel_id=11, name="World Boss", due_at=1000.0, created_by=7, interval_seconds=3600
    )
    assert later.id != sooner.id

    fetched = await store.get_timer(sooner.id)
    assert fetched is not None
    assert fetched.name == "World Boss"
    assert fetched.interval_seconds == 3600
    assert fetched.is_repeating

    assert [t.name for t in await store.list_timers(1)] == ["World Boss", "Siege"]
    assert await store.count_timers(1) == 2
    assert await store.get_timer(9999) is None
    assert await store.list_timers(2) == []


async def test_names_are_unique_per_guild_but_not_across_guilds(store):
    await store.add_timer(guild_id=1, channel_id=10, name="World Boss", due_at=1.0, created_by=7)

    with pytest.raises(DuplicateTimerError):
        await store.add_timer(guild_id=1, channel_id=10, name="  world   boss ", due_at=2.0, created_by=7)

    # a different guild may reuse the name
    other = await store.add_timer(guild_id=2, channel_id=20, name="World Boss", due_at=3.0, created_by=8)
    assert other.guild_id == 2


async def test_replace_overwrites_the_existing_timer(store):
    first = await store.add_timer(guild_id=1, channel_id=10, name="Rift", due_at=100.0, created_by=7)
    second = await store.add_timer(
        guild_id=1, channel_id=12, name="Rift", due_at=900.0, created_by=8, replace=True
    )

    assert second.id != first.id
    assert await store.get_timer(first.id) is None
    assert await store.count_timers(1) == 1
    assert (await store.get_timer_by_name(1, "RIft")).due_at == 900.0


async def test_lookup_by_name_is_case_insensitive(store):
    await store.add_timer(guild_id=1, channel_id=10, name="World Boss", due_at=100.0, created_by=7)
    assert (await store.get_timer_by_name(1, "world boss")) is not None
    assert (await store.get_timer_by_name(1, "missing")) is None


async def test_due_timers_returns_only_past_timers(store):
    await store.add_timer(guild_id=1, channel_id=10, name="Past", due_at=100.0, created_by=7)
    await store.add_timer(guild_id=1, channel_id=10, name="Now", due_at=200.0, created_by=7)
    await store.add_timer(guild_id=1, channel_id=10, name="Future", due_at=300.0, created_by=7)

    due = await store.due_timers(200.0)
    assert [t.name for t in due] == ["Past", "Now"]
    assert await store.due_timers(200.0, limit=1) != due
    assert [t.name for t in await store.due_timers(200.0, limit=1)] == ["Past"]


async def test_reschedule_and_delete(store):
    timer = await store.add_timer(
        guild_id=1, channel_id=10, name="Boss", due_at=100.0, created_by=7, interval_seconds=60.0
    )
    await store.reschedule(timer.id, 160.0, fired_count=3)

    updated = await store.get_timer(timer.id)
    assert updated.due_at == 160.0
    assert updated.fired_count == 3

    assert await store.delete_timer_by_name(1, "boss") is True
    assert await store.delete_timer_by_name(1, "boss") is False
    assert await store.delete_timer(timer.id) is False


async def test_clear_timers_leaves_other_guilds_alone(store):
    await store.add_timer(guild_id=1, channel_id=10, name="A", due_at=1.0, created_by=7)
    await store.add_timer(guild_id=1, channel_id=10, name="B", due_at=1.0, created_by=7)
    await store.add_timer(guild_id=2, channel_id=10, name="C", due_at=1.0, created_by=7)

    assert await store.clear_timers(1) == 2
    assert await store.count_timers(1) == 0
    assert await store.count_timers(2) == 1


async def test_timer_survives_reopening_the_database(tmp_path):
    path = tmp_path / "persist.db"

    async with Store(path) as first:
        await first.add_timer(
            guild_id=1, channel_id=10, name="World Boss", due_at=time.time() + 60, created_by=7
        )

    async with Store(path) as second:
        timers = await second.list_timers(1)
        assert [t.name for t in timers] == ["World Boss"]


async def test_guild_presets_round_trip(store):
    created = await store.add_guild_preset(
        guild_id=1,
        display_name="Ancient Boss",
        interval_seconds=5400.0,
        category="Open world",
        aliases="ab, ancient",
        note="from the raid rift",
        created_by=7,
    )
    assert created.key == "ancient boss"
    assert created.aliases == ("ab", "ancient")

    assert (await store.get_guild_preset(1, "Ancient Boss")).interval_seconds == 5400.0
    assert (await store.get_guild_preset(1, "ab")).display_name == "Ancient Boss"
    assert (await store.get_guild_preset(2, "Ancient Boss")) is None
    assert [p.display_name for p in await store.list_guild_presets(1)] == ["Ancient Boss"]


async def test_adding_the_same_guild_preset_updates_it(store):
    await store.add_guild_preset(guild_id=1, display_name="Boss", interval_seconds=60.0)
    await store.add_guild_preset(
        guild_id=1, display_name="boss", interval_seconds=600.0, category="Custom"
    )

    presets = await store.list_guild_presets(1)
    assert len(presets) == 1
    assert presets[0].interval_seconds == 600.0


async def test_guild_preset_deletion(store):
    await store.add_guild_preset(guild_id=1, display_name="Boss", interval_seconds=60.0)
    assert await store.delete_guild_preset(1, "BOSS") is True
    assert await store.delete_guild_preset(1, "BOSS") is False
    assert await store.list_guild_presets(1) == []


# -------------------------------------------------------------- event boards


async def test_event_board_round_trip(store):
    created = await store.upsert_event_board(
        guild_id=1, channel_id=10, hours=24, created_by=7, refresh_at=100.0, created_at=50.0
    )
    assert (created.guild_id, created.channel_id, created.hours) == (1, 10, 24)
    assert created.message_id is None
    assert (created.refresh_at, created.created_at) == (100.0, 50.0)

    await store.set_event_board_message(1, 10, message_id=555, refresh_at=200.0)
    stored = await store.get_event_board(1, 10)
    assert stored.message_id == 555
    assert stored.refresh_at == 200.0
    assert stored.hours == 24  # untouched by the message update

    assert await store.get_event_board(1, 11) is None


async def test_one_board_per_channel_and_the_posted_message_survives(store):
    await store.upsert_event_board(guild_id=1, channel_id=10, hours=24, created_by=7, created_at=50.0)
    await store.set_event_board_message(1, 10, message_id=999, refresh_at=10.0)

    again = await store.upsert_event_board(guild_id=1, channel_id=10, hours=48, created_by=8)

    assert again.hours == 48
    assert again.message_id == 999  # re-running /timer board keeps editing the same message
    assert again.created_at == 50.0  # the original creation time is kept
    assert len(await store.list_event_boards(1)) == 1


async def test_due_event_boards_are_selected_by_refresh_time(store):
    await store.upsert_event_board(guild_id=1, channel_id=10, hours=24, created_by=7, refresh_at=100.0)
    await store.upsert_event_board(guild_id=1, channel_id=11, hours=24, created_by=7, refresh_at=500.0)

    assert await store.due_event_boards(50.0) == []
    assert [b.channel_id for b in await store.due_event_boards(200.0)] == [10]
    assert [b.channel_id for b in await store.due_event_boards(1000.0)] == [10, 11]


async def test_listing_and_deleting_event_boards(store):
    await store.upsert_event_board(guild_id=1, channel_id=10, hours=24, created_by=7)
    await store.upsert_event_board(guild_id=2, channel_id=20, hours=24, created_by=7)

    assert [(b.guild_id, b.channel_id) for b in await store.list_event_boards()] == [(1, 10), (2, 20)]
    assert [b.channel_id for b in await store.list_event_boards(2)] == [20]
    assert await store.delete_event_board(1, 10) is True
    assert await store.delete_event_board(1, 10) is False
    assert len(await store.list_event_boards()) == 1


# ----------------------------------------------------------- event reminders


async def test_event_reminder_round_trip(store):
    created = await store.upsert_event_reminder(
        guild_id=1, event_id="rift", minutes_before=5, channel_id=10, created_by=7, mention="here"
    )
    assert (created.minutes_before, created.channel_id, created.mention) == (5, 10, "here")
    assert created.last_fired_at is None

    assert (await store.get_event_reminder(1, "rift")).event_id == "rift"
    assert await store.get_event_reminder(2, "rift") is None
    assert [r.event_id for r in await store.list_event_reminders(1)] == ["rift"]
    assert [r.event_id for r in await store.list_event_reminders()] == ["rift"]


async def test_a_reminder_is_one_per_event_and_changing_it_rearms_it(store):
    await store.upsert_event_reminder(
        guild_id=1, event_id="rift", minutes_before=0, channel_id=10, created_by=7
    )
    await store.mark_event_reminder_fired(1, "rift", started_at=1234.0)
    assert (await store.get_event_reminder(1, "rift")).last_fired_at == 1234.0

    updated = await store.upsert_event_reminder(
        guild_id=1, event_id="rift", minutes_before=10, channel_id=11, created_by=8, mention="everyone"
    )

    assert updated.minutes_before == 10
    assert updated.channel_id == 11
    assert updated.mention == "everyone"
    assert updated.last_fired_at is None  # otherwise the new setting never fires
    assert len(await store.list_event_reminders(1)) == 1


async def test_deleting_an_event_reminder(store):
    await store.upsert_event_reminder(
        guild_id=1, event_id="rift", minutes_before=5, channel_id=10, created_by=7
    )
    assert await store.delete_event_reminder(1, "rift") is True
    assert await store.delete_event_reminder(1, "rift") is False
    assert await store.list_event_reminders(1) == []


async def test_event_tables_survive_reopening_the_database(tmp_path):
    path = tmp_path / "persist-events.db"

    async with Store(path) as first:
        await first.upsert_event_board(guild_id=1, channel_id=10, hours=12, created_by=7)
        await first.set_event_board_message(1, 10, message_id=42, refresh_at=99.0)
        await first.upsert_event_reminder(
            guild_id=1, event_id="rift", minutes_before=2, channel_id=10, created_by=7
        )

    async with Store(path) as second:
        board = await second.get_event_board(1, 10)
        assert (board.message_id, board.hours, board.refresh_at) == (42, 12, 99.0)
        assert (await second.get_event_reminder(1, "rift")).minutes_before == 2

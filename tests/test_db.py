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

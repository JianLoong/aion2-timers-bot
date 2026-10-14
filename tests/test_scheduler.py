import pytest

from aion2_timerbot.scheduler import TimerScheduler, next_occurrence


@pytest.mark.parametrize(
    ("due_at", "interval", "now", "expected"),
    [
        (1000.0, 3600.0, 1000.0, 4600.0),   # fires exactly now -> next cycle
        (1000.0, 3600.0, 0.0, 1000.0),      # not yet due -> unchanged
        (1000.0, 3600.0, 4600.0, 8200.0),
        (1000.0, 3600.0, 9999.0, 11800.0),  # two missed cycles are skipped, not replayed
        (1000.0, 60.0, 1000.5, 1060.0),
    ],
)
def test_next_occurrence(due_at, interval, now, expected):
    assert next_occurrence(due_at, interval, now) == expected


def test_next_occurrence_rejects_a_non_positive_interval():
    with pytest.raises(ValueError):
        next_occurrence(1.0, 0.0, 2.0)


async def test_due_one_shot_fires_then_disappears(store):
    calls: list[tuple[str, float]] = []

    async def on_due(timer, lateness):
        calls.append((timer.name, lateness))

    scheduler = TimerScheduler(store, on_due)
    await store.add_timer(guild_id=1, channel_id=2, name="Boss", due_at=100.0, created_by=3)

    fired = await scheduler.run_once(now=101.5)
    assert [t.name for t in fired] == ["Boss"]
    assert calls == [("Boss", 1.5)]
    assert await store.list_timers(1) == []
    assert await scheduler.run_once(now=200.0) == []


async def test_repeating_timer_is_advanced_not_deleted(store):
    calls: list[str] = []

    async def on_due(timer, lateness):
        calls.append(timer.name)

    scheduler = TimerScheduler(store, on_due)
    await store.add_timer(
        guild_id=1, channel_id=2, name="World Boss", due_at=100.0, created_by=3, interval_seconds=3600.0
    )

    await scheduler.run_once(now=100.0)
    timer = (await store.list_timers(1))[0]
    assert timer.due_at == 3700.0
    assert timer.fired_count == 1
    assert calls == ["World Boss"]

    # a long outage advances by whole intervals instead of firing eight times
    await scheduler.run_once(now=20000.0)
    timer = (await store.list_timers(1))[0]
    assert timer.due_at == 3700.0 + 3600 * 5
    assert timer.fired_count == 2
    assert len(calls) == 2


async def test_a_failing_alert_still_advances_the_timer(store):
    async def on_due(timer, lateness):
        raise RuntimeError("channel is gone")

    scheduler = TimerScheduler(store, on_due)
    await store.add_timer(
        guild_id=1, channel_id=2, name="Boss", due_at=100.0, created_by=3, interval_seconds=60.0
    )

    fired = await scheduler.run_once(now=100.0)
    assert [t.name for t in fired] == ["Boss"]
    timer = (await store.list_timers(1))[0]
    assert timer.due_at == 160.0
    assert timer.fired_count == 1


async def test_only_timers_that_are_due_fire(store):
    calls: list[str] = []

    async def on_due(timer, lateness):
        calls.append(timer.name)

    scheduler = TimerScheduler(store, on_due)
    await store.add_timer(guild_id=1, channel_id=2, name="Soon", due_at=100.0, created_by=3)
    await store.add_timer(guild_id=1, channel_id=2, name="Later", due_at=900.0, created_by=3)

    await scheduler.run_once(now=150.0)
    assert calls == ["Soon"]
    assert [t.name for t in await store.list_timers(1)] == ["Later"]


async def test_start_and_stop_are_idempotent(store):
    async def on_due(timer, lateness):
        return None

    scheduler = TimerScheduler(store, on_due, tick_seconds=1.0)
    assert not scheduler.running
    await scheduler.start()
    await scheduler.start()
    assert scheduler.running
    await scheduler.stop()
    await scheduler.stop()
    assert not scheduler.running

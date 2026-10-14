"""Fetching, caching, TTL and override handling for the event schedule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aion2_timerbot.config import Config
from aion2_timerbot.schedule import merge_override, parse_override, parse_timers
from aion2_timerbot.schedule_source import RETRY_SECONDS, ScheduleSource

EXAMPLE_FILE = Path(__file__).resolve().parents[1] / "schedules.example.json"

PAYLOAD = {
    "updatedAt": "2026-10-09",
    "regions": [{"id": "global-as", "label": "Asia", "timeZone": "Asia/Seoul"}],
    "events": [
        {
            "id": "rift",
            "name": "Spacetime Rift",
            "icon": "🌀",
            "kind": "pvp",
            "durationMinutes": 60,
            "schedules": {"global-as": {"type": "daily", "times": ["02:00"]}},
        },
        {
            "id": "dimensional-invasion",
            "name": "Dimensional Invasion",
            "icon": "🛡️",
            "kind": "event",
            "durationMinutes": 15,
            "schedules": {"global-as": {"type": "hourly", "minute": 30}},
        },
    ],
}


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Stub:
    """Stands in for the network, and remembers what it was asked for."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        response = self.responses.pop(0) if self.responses else self.responses
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return response
        return json.dumps(response).encode("utf-8")


def _source(tmp_path: Path, opener, **kwargs) -> ScheduleSource:
    return ScheduleSource(
        region="global-as",
        url="https://example.invalid/timers.json",
        cache_path=tmp_path / "schedule-cache.json",
        override_path=tmp_path / "schedules.json",
        ttl_seconds=100.0,
        opener=opener,
        **kwargs,
    )


# ------------------------------------------------------------------- fetching


async def test_fetches_and_caches(tmp_path):
    stub = Stub(PAYLOAD)
    source = _source(tmp_path, stub)

    schedule = await source.get()

    assert source.last_error is None
    assert schedule.region.time_zone == "Asia/Seoul"
    assert [event.id for event in schedule.events] == ["rift", "dimensional-invasion"]
    assert stub.calls == ["https://example.invalid/timers.json"]

    cached = json.loads((tmp_path / "schedule-cache.json").read_text(encoding="utf-8"))
    assert cached["payload"]["updatedAt"] == "2026-10-09"


async def test_refetches_only_after_the_ttl(tmp_path):
    clock = FakeClock()
    stub = Stub(PAYLOAD, PAYLOAD, PAYLOAD)
    source = _source(tmp_path, stub, clock=clock)

    await source.get()
    clock.now += 50
    await source.get()
    assert len(stub.calls) == 1  # still fresh

    clock.now += 60  # now past the 100s TTL
    await source.get()
    assert len(stub.calls) == 2


async def test_a_failed_fetch_retries_sooner_than_the_ttl(tmp_path):
    clock = FakeClock()
    stub = Stub(PAYLOAD, OSError("network down"), PAYLOAD)
    source = _source(tmp_path, stub, clock=clock)

    await source.get()
    clock.now += 101  # stale
    await source.get()
    assert source.last_error is not None
    assert source.schedule.events  # the previous data is still served

    clock.now += RETRY_SECONDS + 1
    await source.get()
    assert len(stub.calls) == 3


async def test_falls_back_to_the_cache_when_the_fetch_fails(tmp_path):
    await _source(tmp_path, Stub(PAYLOAD)).get()  # warms the cache

    offline = _source(tmp_path, Stub(OSError("network down")))
    schedule = await offline.get()

    assert [event.id for event in schedule.events] == ["rift", "dimensional-invasion"]
    assert offline.last_error is not None
    assert "network down" in offline.last_error


async def test_no_network_and_no_cache_yields_an_empty_schedule_and_an_explanation(tmp_path):
    source = _source(tmp_path, Stub(OSError("dns is down")))

    schedule = await source.get()

    assert schedule.events == []
    assert source.last_error is not None


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (b"<html>not json</html>", "did not return JSON"),
        (b'[{"not": "an object"}]', "expected an object"),
    ],
)
async def test_a_bad_response_is_reported_not_raised(tmp_path, response, match):
    source = _source(tmp_path, Stub(response))

    schedule = await source.get()

    assert schedule.events == []
    assert match in source.last_error


async def test_an_unknown_region_is_reported(tmp_path):
    source = ScheduleSource(region="global-au", opener=Stub(PAYLOAD), cache_path=tmp_path / "c.json")

    schedule = await source.get()

    assert schedule.events == []
    # a data problem, not a fetch problem: it survives a successful download
    assert source.data_error is not None
    assert "global-au" in source.problem


async def test_a_fetch_can_be_switched_off(tmp_path):
    """SCHEDULE_URL="" and a cache on disk: no network at all."""
    await _source(tmp_path, Stub(PAYLOAD)).get()

    stub = Stub()  # would raise if called
    offline = ScheduleSource(
        region="global-as", url="", cache_path=tmp_path / "schedule-cache.json", opener=stub
    )

    schedule = await offline.get()
    assert len(schedule.events) == 2
    assert stub.calls == []


async def test_an_unwritable_cache_is_not_fatal(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.mkdir()
    source = ScheduleSource(
        region="global-as", opener=Stub(PAYLOAD), cache_path=blocker  # a directory, not a file
    )

    schedule = await source.get()

    assert latest_ids(schedule) == ["rift", "dimensional-invasion"]


# ------------------------------------------------------------------ overrides


def _write_override(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


async def test_override_patches_a_time(tmp_path):
    stub = Stub(PAYLOAD)
    source = _source(tmp_path, stub)
    _write_override(
        tmp_path / "schedules.json",
        {"events": [{"id": "rift", "schedules": {"global-as": {"type": "daily", "times": ["03:00"]}}}]},
    )

    schedule = await source.get()

    rift = schedule.find("rift")
    assert rift.spec_for("global-as").times == ((3, 0),)
    assert source.override_error is None


async def test_override_adds_and_removes_events(tmp_path):
    source = _source(tmp_path, Stub(PAYLOAD))
    _write_override(
        tmp_path / "schedules.json",
        {
            "events": [
                {"id": "dimensional-invasion", "remove": True},
                {
                    "id": "ancient-boss",
                    "name": "Ancient Boss",
                    "kind": "boss",
                    "durationMinutes": 30,
                    "schedule": {"type": "weekly", "days": ["fri"], "times": ["22:00"]},
                },
            ]
        },
    )

    schedule = await source.get()

    assert "dimensional-invasion" not in latest_ids(schedule)
    assert "ancient-boss" in latest_ids(schedule)


async def test_a_broken_override_is_ignored_and_explained(tmp_path):
    source = _source(tmp_path, Stub(PAYLOAD))
    _write_override(tmp_path / "schedules.json", {"events": [{"id": "rift", "schedules": {"global-as": {"type": "monthly"}}}]})

    schedule = await source.get()

    assert latest_ids(schedule) == ["rift", "dimensional-invasion"]  # remote data still served
    assert source.override_error is not None
    assert "monthly" in source.override_error


async def test_broken_json_in_the_override_file_is_reported(tmp_path):
    source = _source(tmp_path, Stub(PAYLOAD))
    (tmp_path / "schedules.json").write_text("{not json", encoding="utf-8")

    schedule = await source.get()

    assert latest_ids(schedule) == ["rift", "dimensional-invasion"]
    assert source.override_error is not None


async def test_editing_the_override_file_takes_effect_without_a_refetch(tmp_path):
    clock = FakeClock()
    stub = Stub(PAYLOAD)
    source = _source(tmp_path, stub, clock=clock)
    override = tmp_path / "schedules.json"

    _write_override(override, {"events": [{"id": "rift", "schedules": {"global-as": {"type": "daily", "times": ["02:00"]}}}]})
    first = await source.get()
    assert first.find("rift").spec_for("global-as").times == ((2, 0),)

    clock.now += 1
    _write_override(override, {"events": [{"id": "rift", "schedules": {"global-as": {"type": "daily", "times": ["02:00", "04:00"]}}}]})
    second = await source.get()

    assert second.find("rift").spec_for("global-as").times == ((2, 0), (4, 0))
    assert len(stub.calls) == 1  # no refetch needed


async def test_override_file_can_supply_the_region_without_any_network(tmp_path):
    source = ScheduleSource(
        region="global-as", url="", cache_path=tmp_path / "missing-cache.json", opener=Stub()
    )
    _write_override(
        tmp_path / "schedules.json",
        {
            "region": "global-as",
            "regionLabel": "Asia",
            "timeZone": "Asia/Seoul",
            "events": [
                {
                    "id": "my-boss",
                    "name": "My Boss",
                    "kind": "boss",
                    "schedule": {"type": "hourly", "minute": 5},
                }
            ],
        },
    )
    source.override_path = tmp_path / "schedules.json"

    schedule = await source.get()

    assert schedule.region.time_zone == "Asia/Seoul"
    assert latest_ids(schedule) == ["my-boss"]


def latest_ids(schedule) -> list[str]:
    return [event.id for event in schedule.events]


def test_the_shipped_example_override_file_still_works():
    """The documented example must load and layer the way the README says it does."""
    payload = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))

    overrides = parse_override(payload, "global-as")
    assert [event.id for event in overrides] == [
        "rift",
        "daily-reset",
        "ancient-boss",
        "dimensional-invasion",
    ]

    base = parse_timers(
        {
            "regions": [{"id": "global-as", "label": "Asia", "timeZone": "Asia/Seoul"}],
            "events": [
                {
                    "id": "rift",
                    "name": "Spacetime Rift",
                    "durationMinutes": 60,
                    "schedules": {"global-as": {"type": "daily", "times": ["02:00"]}},
                },
                {
                    "id": "daily-reset",
                    "name": "Daily reset",
                    "kind": "reset",
                    "schedules": {"global-as": {"type": "daily", "times": ["16:00"]}},
                },
                {
                    "id": "dimensional-invasion",
                    "name": "Dimensional Invasion",
                    "durationMinutes": 15,
                    "schedules": {"global-as": {"type": "hourly", "minute": 30}},
                },
            ],
        },
        "global-as",
    )
    merged = merge_override(base, overrides)

    # patched: the times in the example replace the fetched ones
    rift = merged.find("rift")
    assert rift.spec_for("global-as").times == (
        (2, 0), (5, 0), (8, 0), (11, 0), (14, 0), (17, 0), (20, 0), (23, 0)
    )
    # added: a brand new event, on the short "schedule" key
    added = merged.find("Ancient Boss")
    assert added is not None
    assert added.spec_for("global-as").kind == "weekly"
    assert added.duration_minutes == 30
    # removed
    assert merged.find("dimensional-invasion") is None


# --------------------------------------------------------------------- config


def test_config_paths_follow_the_database_location(tmp_path):
    config = Config(token="x", db_path=tmp_path / "data" / "timers.db")

    assert config.schedules_path == tmp_path / "data" / "schedules.json"
    assert config.schedule_cache_path == tmp_path / "data" / "schedule-cache.json"
    assert config.schedule_region == "global-as"
    assert config.schedule_url.startswith("https://")


def test_config_reads_the_schedule_settings_from_the_environment():
    config = Config.from_env(
        {
            "DISCORD_TOKEN": "x",
            "SCHEDULE_REGION": "global-eu",
            "SCHEDULE_URL": "https://example.test/timers.json",
            "SCHEDULE_TTL_HOURS": "12",
            "SCHEDULES_FILE": "custom-schedules.json",
        }
    )

    assert config.schedule_region == "global-eu"
    assert config.schedule_url == "https://example.test/timers.json"
    assert config.schedule_ttl_hours == 12.0
    assert config.schedules_path == Path("custom-schedules.json")

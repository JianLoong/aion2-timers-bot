"""Clock-schedule parsing, override merging and next-occurrence math."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from aion2_timerbot.schedule import (
    ScheduleError,
    merge_override,
    parse_override,
    parse_timers,
)

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

PAYLOAD = {
    "updatedAt": "2026-10-09",
    "regions": [
        {"id": "global-as", "label": "Asia", "timeZone": "Asia/Seoul", "note": "Korea/Japan time"},
        {"id": "global-nae", "label": "NA East", "timeZone": "America/New_York", "note": "NY time"},
        {"id": "kr", "label": "Korea", "timeZone": "Asia/Seoul"},
    ],
    "events": [
        {
            "id": "rift",
            "name": "Spacetime Rift",
            "icon": "🌀",
            "kind": "pvp",
            "description": "portal open 10 minutes",
            "durationMinutes": 60,
            "portalMinutes": 10,
            "schedules": {
                "global-as": {"type": "daily", "times": ["02:00", "05:00", "23:00"]},
                "kr": {"type": "daily", "times": ["02:00"]},
            },
        },
        {
            "id": "dimensional-invasion",
            "name": "Dimensional Invasion",
            "icon": "🛡️",
            "kind": "event",
            "durationMinutes": 15,
            "schedules": {"global-as": {"type": "hourly", "minute": 30}},
        },
        {
            "id": "artifact-siege",
            "name": "Artifact Siege",
            "icon": "🏰",
            "kind": "abyss",
            "durationMinutes": 30,
            "schedules": {
                "global-as": {
                    "type": "weekly",
                    "days": ["mon", "thu", "sat"],
                    "times": ["21:00"],
                    "timeLabels": ["Group 1"],
                }
            },
        },
        {
            "id": "daily-reset",
            "name": "Daily reset",
            "icon": "🔄",
            "kind": "reset",
            "durationMinutes": 0,
            "schedules": {
                # one instant worldwide: 16:00 Korean time whatever the region's clock
                "global-nae": {"type": "daily", "times": ["16:00"], "timeZone": "Asia/Seoul"}
            },
        },
    ],
}


def _kst(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=KST).timestamp()


@pytest.fixture
def schedule():
    return parse_timers(PAYLOAD, "global-as")


# ------------------------------------------------------------------- parsing


def test_parses_the_region_and_only_its_events(schedule):
    assert schedule.region.id == "global-as"
    assert schedule.region.time_zone == "Asia/Seoul"
    assert schedule.updated_at == "2026-10-09"
    # daily-reset only has a global-nae rule, so it is not part of the Asia board
    assert [event.id for event in schedule.sorted_events()] == [
        "artifact-siege",
        "dimensional-invasion",
        "rift",
    ]


def test_looks_events_up_by_name_id_and_whitespace(schedule):
    assert schedule.find("Spacetime Rift").id == "rift"
    assert schedule.find("rift").id == "rift"
    assert schedule.find("  spacetime   rift ").id == "rift"
    assert schedule.find("nothing") is None


def test_a_region_can_be_switched_by_reparsing():
    other = parse_timers(PAYLOAD, "global-nae")
    assert other.region.time_zone == "America/New_York"
    assert [event.id for event in other.events] == ["daily-reset"]


def test_unknown_region_lists_the_known_ones():
    with pytest.raises(ScheduleError, match="global-as"):
        parse_timers(PAYLOAD, "global-au")


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        ({"regions": [{"id": "x", "label": "X"}]}, "timeZone"),
        ({"events": [{"id": "e"}]}, "name"),
        ({"regions": []}, "no regions"),
    ],
)
def test_bad_payloads_are_rejected(edit, match):
    broken = {**PAYLOAD, **edit}
    with pytest.raises(ScheduleError, match=match):
        parse_timers(broken, "global-as")


def test_events_list_must_be_a_list():
    with pytest.raises(ScheduleError, match="must be a list"):
        parse_timers({**PAYLOAD, "events": "nope"}, "global-as")


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "monthly", "times": ["01:00"]},
        {"type": "daily"},
        {"type": "daily", "times": ["25:00"]},
        {"type": "daily", "times": ["1:00", "later"]},
        {"type": "weekly", "times": ["21:00"]},
        {"type": "weekly", "days": ["funday"], "times": ["21:00"]},
        {"type": "hourly", "minute": 90},
        {"type": "hourly"},
    ],
)
def test_bad_rules_are_rejected_with_context(spec):
    payload = {
        **PAYLOAD,
        "events": [{"id": "broken", "name": "Broken", "schedules": {"global-as": spec}}],
    }
    with pytest.raises(ScheduleError, match="broken"):
        parse_timers(payload, "global-as")


def test_describe_rule_summarises_each_rule(schedule):
    rift = schedule.find("rift")
    invasion = schedule.find("Dimensional Invasion")
    siege = schedule.find("Artifact Siege")
    assert schedule.region.id == "global-as"
    assert rift.describe_rule(rift.spec_for("global-as")) == "daily · 02:00, 05:00, 23:00"
    assert invasion.describe_rule(invasion.spec_for("global-as")) == "every hour at :30"
    assert siege.describe_rule(siege.spec_for("global-as")) == "Mon, Thu, Sat · 21:00"


# --------------------------------------------------------------- occurrences


def test_hourly_event_lands_on_the_next_half_hour(schedule):
    invasion = schedule.find("Dimensional Invasion")
    now = _kst(2026, 10, 14, 12, 34)

    occurrence = schedule.next_occurrence(invasion, now=now)

    assert datetime.fromtimestamp(occurrence.start, tz=KST) == datetime(
        2026, 10, 14, 13, 30, tzinfo=KST
    )
    assert occurrence.end - occurrence.start == 15 * 60
    assert occurrence.icon == "🛡️"
    assert occurrence.kind == "event"


def test_daily_event_rolls_to_the_next_listed_time(schedule):
    rift = schedule.find("rift")

    midnight = schedule.next_occurrence(rift, now=_kst(2026, 10, 14, 0, 30))
    assert datetime.fromtimestamp(midnight.start, tz=KST).strftime("%H:%M") == "02:00"

    # just after the last block of the day -> tomorrow's first
    late = schedule.next_occurrence(rift, now=_kst(2026, 10, 14, 23, 30))
    assert datetime.fromtimestamp(late.start, tz=KST) == datetime(2026, 10, 15, 2, 0, tzinfo=KST)


def test_weekly_event_lands_on_a_listed_day(schedule):
    siege = schedule.find("Artifact Siege")

    occurrence = schedule.next_occurrence(siege, now=_kst(2026, 10, 14, 12, 0))
    local = datetime.fromtimestamp(occurrence.start, tz=KST)

    assert local.weekday() in (0, 3, 5)  # Mon, Thu, Sat
    assert (local.hour, local.minute) == (21, 0)
    assert local > datetime(2026, 10, 14, 12, 0, tzinfo=KST)
    assert occurrence.label == "Group 1"


def test_an_event_is_skipped_when_its_rule_only_covers_another_region():
    na = parse_timers(PAYLOAD, "global-nae")
    assert na.find("rift") is None


def test_a_schedule_can_override_the_region_clock():
    """The reset is 16:00 KST even on a New York-region board."""
    na = parse_timers(PAYLOAD, "global-nae")
    reset = na.find("Daily reset")

    assert na.zone_for(reset).key == "Asia/Seoul"
    occurrence = na.next_occurrence(reset, now=_kst(2026, 10, 14, 12, 0))

    # 16:00 Korean time is 07:00 UTC, which their own note calls 03:00 New York (EDT)
    assert datetime.fromtimestamp(occurrence.start, tz=UTC) == datetime(
        2026, 10, 14, 7, 0, tzinfo=UTC
    )
    assert datetime.fromtimestamp(
        occurrence.start, tz=ZoneInfo("America/New_York")
    ).strftime("%H:%M") == "03:00"


def test_next_occurrences_are_ordered_and_capped(schedule):
    invasion = schedule.find("Dimensional Invasion")
    found = schedule.next_occurrences(invasion, now=_kst(2026, 10, 14, 12, 34), count=3)

    assert len(found) == 3
    assert found == sorted(found, key=lambda item: item.start)
    hours = [datetime.fromtimestamp(item.start, tz=KST).strftime("%H:%M") for item in found]
    assert hours == ["13:30", "14:30", "15:30"]


# --------------------------------------------------------------------- board


def test_board_lists_soonest_first_with_a_cap_per_event(schedule):
    rows = schedule.board(now=_kst(2026, 10, 14, 12, 34), hours=24, per_event=3)

    firsts = [row.occurrences[0].start for row in rows]
    assert firsts == sorted(firsts)
    # the :30-every-hour event is always the soonest thing on the board
    assert rows[0].event.id == "dimensional-invasion"
    assert all(1 <= len(row.occurrences) <= 3 for row in rows)
    assert [row.event.id for row in rows][-1] != "dimensional-invasion"
    assert "artifact-siege" in [row.event.id for row in rows]


def test_board_still_mentions_an_event_beyond_the_horizon(schedule):
    rows = schedule.board(now=_kst(2026, 10, 14, 12, 34), hours=1, per_event=3)

    siege = next(row for row in rows if row.event.id == "artifact-siege")
    assert siege.later is True
    assert len(siege.occurrences) == 1


def test_board_respects_the_per_event_cap(schedule):
    rows = schedule.board(now=_kst(2026, 10, 14, 12, 34), hours=48, per_event=2)
    assert all(len(row.occurrences) <= 2 for row in rows)


# ----------------------------------------------------------------- overrides


def _override(payload):
    return parse_override(payload, "global-as")


def test_override_patches_fields_of_an_existing_event(schedule):
    patched = merge_override(
        schedule,
        _override(
            {
                "region": "global-as",
                "events": [
                    {
                        "id": "rift",
                        "schedules": {"global-as": {"type": "hourly", "minute": 15}},
                    }
                ],
            }
        ),
    )

    rift = patched.find("rift")
    assert rift.name == "Spacetime Rift"  # untouched fields survive
    assert rift.duration_minutes == 60
    assert rift.describe_rule(rift.spec_for("global-as")) == "every hour at :15"
    assert cast_start(patched.next_occurrence(rift, now=_kst(2026, 10, 14, 12, 34))) == "13:15"


def test_override_adds_an_event(schedule):
    patched = merge_override(
        schedule,
        _override(
            {
                "events": [
                    {
                        "id": "ancient-boss",
                        "name": "Ancient Boss",
                        "icon": "🐲",
                        "kind": "boss",
                        "durationMinutes": 30,
                        "schedule": {"type": "weekly", "days": ["fri"], "times": ["22:00"]},
                    }
                ]
            }
        ),
    )

    added = patched.find("Ancient Boss")
    assert added is not None
    assert added.spec_for("global-as").kind == "weekly"
    assert "ancient-boss" in [event.id for event in patched.events]


def test_override_can_remove_an_event(schedule):
    patched = merge_override(schedule, _override({"events": [{"id": "rift", "remove": True}]}))

    assert patched.find("rift") is None
    assert len(patched.events) == 2


def test_override_only_replaces_the_regions_it_mentions(schedule):
    patched = merge_override(
        schedule,
        _override({"events": [{"id": "rift", "schedules": {"kr": {"type": "hourly", "minute": 5}}}]}),
    )

    rift = patched.find("rift")
    assert rift.spec_for("global-as").kind == "daily"  # untouched
    assert rift.spec_for("kr").kind == "hourly"  # replaced


def test_a_new_override_event_needs_a_name_and_a_schedule(schedule):
    with pytest.raises(ScheduleError, match="name"):
        merge_override(
            schedule,
            _override({"events": [{"id": "x", "schedule": {"type": "hourly", "minute": 0}}]}),
        )
    with pytest.raises(ScheduleError, match="schedule"):
        merge_override(schedule, _override({"events": [{"id": "x", "name": "X"}]}))


def test_override_entries_need_an_id():
    with pytest.raises(ScheduleError, match="id"):
        _override({"events": [{"name": "No id"}]})


def test_override_accepts_a_bare_list():
    events = _override([{"id": "x", "name": "X", "schedule": {"type": "hourly", "minute": 0}}])
    assert [event.id for event in events] == ["x"]


def test_override_rejects_a_bad_duration():
    with pytest.raises(ScheduleError, match="durationMinutes"):
        _override(
            {"events": [{"id": "x", "name": "X", "durationMinutes": -5, "schedule": {"type": "hourly", "minute": 0}}]}
        )


def cast_start(occurrence) -> str:
    return datetime.fromtimestamp(occurrence.start, tz=KST).strftime("%H:%M")

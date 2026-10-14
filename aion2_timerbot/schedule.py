"""Clock-anchored event schedules — the "every hour at :30", "daily 02:00/05:00", "Mon/Thu/Sat 21:00"
kind of recurrence, in a server's own timezone.

Everything here is pure: no I/O, no Discord. The shape of the data mirrors the public
``timers.json`` that shugo.gg publishes (regions with a timezone, events with a per-region
recurrence rule), so their file can be consumed directly and corrected by an override file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DAYS",
    "Event",
    "EventOccurrences",
    "Occurrence",
    "Region",
    "Schedule",
    "ScheduleError",
    "Spec",
    "merge_override",
    "parse_override",
    "parse_timers",
]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
KINDS = ("event", "boss", "abyss", "pvp", "reset")

# How far ahead to look when asked for "the next few times" of a weekly event.
SEARCH_DAYS = 30
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ScheduleError(Exception):
    """Raised when schedule data cannot be understood."""


@dataclass(slots=True, frozen=True)
class Region:
    id: str
    label: str
    time_zone: str
    note: str = ""


@dataclass(slots=True)
class Spec:
    """One region's recurrence rule: hourly at :MM, daily at fixed times, or weekly on days."""

    kind: str
    minute: int = 0
    times: tuple[tuple[int, int], ...] = ()
    days: tuple[int, ...] = ()
    labels: tuple[str, ...] = ()
    time_zone: str | None = None

    def time_label(self, index: int) -> str:
        return self.labels[index] if index < len(self.labels) else ""


@dataclass(slots=True)
class Event:
    id: str
    name: str
    icon: str = ""
    kind: str = "event"
    description: str = ""
    duration_minutes: int = 0
    portal_minutes: int = 0
    schedules: dict[str, Spec] = field(default_factory=dict)
    origin: str = "remote"

    def spec_for(self, region: str) -> Spec | None:
        return self.schedules.get(region)

    def describe_rule(self, spec: Spec) -> str:
        """A short human summary of when this repeats, e.g. "every hour at :30"."""
        if spec.kind == "hourly":
            return f"every hour at :{spec.minute:02d}"
        if spec.kind == "daily":
            return "daily · " + ", ".join(f"{h:02d}:{m:02d}" for h, m in spec.times)
        days = ", ".join(DAYS[d].title() for d in spec.days)
        return f"{days} · " + ", ".join(f"{h:02d}:{m:02d}" for h, m in spec.times)


@dataclass(slots=True, frozen=True)
class Occurrence:
    event_id: str
    name: str
    icon: str
    kind: str
    start: float
    end: float
    label: str = ""

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(slots=True)
class EventOccurrences:
    """An event with the times the board should show for it."""

    event: Event
    spec: Spec
    occurrences: list[Occurrence]
    later: bool = False  # True when the next one is beyond the board's horizon


@dataclass(slots=True)
class Schedule:
    region: Region
    events: list[Event] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    updated_at: str = ""
    fetched_at: float | None = None

    def names(self) -> list[str]:
        return [event.name for event in self.sorted_events()]

    def sorted_events(self) -> list[Event]:
        return sorted(self.events, key=lambda e: e.name.casefold())

    def find(self, name_or_id: str) -> Event | None:
        """Look an event up by name (case/space insensitive) or by id."""
        wanted = name_or_id.strip().casefold()
        for event in self.events:
            if event.id.casefold() == wanted or event.name.casefold() == wanted:
                return event
        squashed = re.sub(r"\s+", " ", wanted)
        for event in self.events:
            if re.sub(r"\s+", " ", event.name.casefold()) == squashed:
                return event
        return None

    # ---------------------------------------------------------------- occurrences

    def zone_for(self, event: Event, region: str | None = None) -> ZoneInfo:
        """The clock an event runs on: its own per-region override, else the region's.

        The region is looked up here rather than passed as a spec so a caller cannot
        accidentally skip an override like the resets' shared 16:00 Korean time.
        """
        spec = event.spec_for(region or self.region.id)
        name = (spec.time_zone if spec else None) or self.region.time_zone
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ScheduleError(
                f"Unknown timezone {name!r} for {event.name}. Install the `tzdata` package or "
                "fix the timeZone in your schedule override file."
            ) from exc

    def next_occurrences(
        self, event: Event, *, now: float, count: int = 1, search_days: int = SEARCH_DAYS
    ) -> list[Occurrence]:
        """The next ``count`` times this event starts, skipping ones already over."""
        spec = event.spec_for(self.region.id)
        if spec is None:
            return []
        zone = self.zone_for(event)
        moment = datetime.fromtimestamp(now, tz=zone)
        until = moment + timedelta(days=search_days)
        return _occurrences(event, spec, zone, after=moment, until=until, limit=count)

    def next_occurrence(self, event: Event, *, now: float) -> Occurrence | None:
        found = self.next_occurrences(event, now=now, count=1)
        return found[0] if found else None

    def board(
        self, *, now: float, hours: float = 24.0, per_event: int = 3
    ) -> list[EventOccurrences]:
        """What to show on the "next up" board: soonest first, a few times per event.

        An event whose next start is past the horizon is still listed once, so a quiet window
        still shows that next week's siege exists.
        """
        rows: list[EventOccurrences] = []
        for event in self.events:
            spec = event.spec_for(self.region.id)
            if spec is None:
                continue
            zone = self.zone_for(event)
            moment = datetime.fromtimestamp(now, tz=zone)
            horizon = moment + timedelta(hours=hours)
            found = _occurrences(
                event, spec, zone, after=moment, until=horizon, limit=per_event
            )
            if not found:
                found = self.next_occurrences(event, now=now, count=1)
                if not found:
                    continue
                rows.append(EventOccurrences(event, spec, found, later=True))
            else:
                rows.append(EventOccurrences(event, spec, found))
        rows.sort(key=lambda row: row.occurrences[0].start)
        return rows


def _occurrences(
    event: Event,
    spec: Spec,
    zone: ZoneInfo,
    *,
    after: datetime,
    until: datetime,
    limit: int | None,
) -> list[Occurrence]:
    """Walk days in [after, until] and collect this event's starts."""
    found: list[Occurrence] = []
    day = after.date()
    while day <= until.date():
        if limit is not None and len(found) >= limit:
            break
        for naive, label in _starts_on(spec, day):
            start = naive.replace(tzinfo=zone)
            if start < after:
                continue
            if start > until:
                break
            found.append(
                Occurrence(
                    event_id=event.id,
                    name=event.name,
                    icon=event.icon,
                    kind=event.kind,
                    start=start.timestamp(),
                    end=(start + timedelta(minutes=event.duration_minutes)).timestamp(),
                    label=label,
                )
            )
            if limit is not None and len(found) >= limit:
                break
        day += timedelta(days=1)
    return found


def _starts_on(spec: Spec, day) -> list[tuple[datetime, str]]:
    """Every wall-clock start of this spec on one local date, ascending."""
    if spec.kind == "hourly":
        return [(datetime.combine(day, time(hour, spec.minute)), "") for hour in range(24)]
    if spec.kind == "weekly" and day.weekday() not in spec.days:
        return []
    return [
        (datetime.combine(day, time(hour, minute)), spec.time_label(index))
        for index, (hour, minute) in enumerate(spec.times)
    ]


# ----------------------------------------------------------------------- parsing


def parse_timers(payload: object, region_id: str, *, origin: str = "remote") -> Schedule:
    """Read shugo.gg's timers.json (or the same shape) for one region."""
    where = "timers.json"
    if not isinstance(payload, dict):
        raise ScheduleError(f"{where}: expected an object at the top level.")

    regions = [_parse_region(entry, where, index) for index, entry in enumerate(_as_list(payload.get("regions"), where, "regions"))]
    if not regions:
        raise ScheduleError(f"{where}: no regions listed.")

    region = next((r for r in regions if r.id == region_id), None)
    if region is None:
        known = ", ".join(r.id for r in regions)
        raise ScheduleError(f"{where}: unknown region {region_id!r}. Known regions: {known}.")

    events: list[Event] = []
    for index, entry in enumerate(_as_list(payload.get("events"), where, "events")):
        event = _parse_event(entry, where, index, region_id, origin=origin)
        if event.spec_for(region_id) is not None:
            events.append(event)

    updated = payload.get("updatedAt")
    return Schedule(
        region=region,
        events=events,
        regions=regions,
        updated_at=str(updated).strip() if updated else "",
    )


def parse_override(payload: object, region_id: str, *, origin: str = "local") -> list[Event]:
    """Read the local override file: partial events that patch or add to the remote ones."""
    where = "schedule override"
    if isinstance(payload, dict):
        entries = _as_list(payload.get("events"), where, "events")
    else:
        entries = _as_list(payload, where, "events")

    events: list[Event] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ScheduleError(f"{where}: event #{index + 1} must be an object.")
        event_id = str(entry.get("id") or "").strip()
        if not event_id:
            raise ScheduleError(f"{where}: event #{index + 1} has no 'id'.")
        if entry.get("remove"):
            events.append(Event(id=event_id, name=event_id, origin="removed"))
            continue

        schedules: dict[str, Spec] = {}
        raw = entry.get("schedules")
        if raw is not None:
            if not isinstance(raw, dict):
                raise ScheduleError(f"{where}: schedules for {event_id!r} must be an object.")
            for key, spec_payload in raw.items():
                schedules[str(key)] = _parse_spec(
                    spec_payload, where, f"schedules.{key}", f"{event_id!r}"
                )
        elif "schedule" in entry:
            schedules[region_id] = _parse_spec(
                entry["schedule"], where, "schedule", f"{event_id!r}"
            )

        events.append(
            Event(
                id=event_id,
                name=str(entry.get("name") or "").strip(),
                icon=str(entry.get("icon") or "").strip(),
                kind=str(entry.get("kind") or "").strip().casefold(),
                description=str(entry.get("description") or "").strip(),
                duration_minutes=_minutes(entry.get("durationMinutes"), where, event_id),
                portal_minutes=_minutes(entry.get("portalMinutes"), where, event_id),
                schedules=schedules,
                origin=origin,
            )
        )
    return events


def merge_override(schedule: Schedule, overrides: list[Event]) -> Schedule:
    """Layer local overrides on top: same id patches fields, new id adds, remove drops."""
    events: dict[str, Event] = {event.id: event for event in schedule.events}
    order = [event.id for event in schedule.events]

    for override in overrides:
        if override.origin == "removed":
            events.pop(override.id, None)
            continue

        base = events.get(override.id)
        if base is None:
            if not override.name:
                raise ScheduleError(f"schedule override: new event {override.id!r} needs a 'name'.")
            if not override.schedules:
                raise ScheduleError(
                    f"schedule override: new event {override.id!r} needs a 'schedule' or "
                    "'schedules'."
                )
            events[override.id] = override
            order.append(override.id)
            continue

        events[override.id] = Event(
            id=base.id,
            name=override.name or base.name,
            icon=override.icon or base.icon,
            kind=override.kind or base.kind,
            description=override.description or base.description,
            duration_minutes=override.duration_minutes or base.duration_minutes,
            portal_minutes=override.portal_minutes or base.portal_minutes,
            schedules={**base.schedules, **override.schedules},
            origin=f"{base.origin}+{override.origin}",
        )

    return Schedule(
        region=schedule.region,
        events=[events[event_id] for event_id in order if event_id in events],
        regions=schedule.regions,
        updated_at=schedule.updated_at,
        fetched_at=schedule.fetched_at,
    )


def _parse_region(entry: object, where: str, index: int) -> Region:
    if not isinstance(entry, dict):
        raise ScheduleError(f"{where}: region #{index + 1} must be an object.")
    region_id = str(entry.get("id") or "").strip()
    zone = str(entry.get("timeZone") or "").strip()
    if not region_id or not zone:
        raise ScheduleError(f"{where}: region #{index + 1} needs 'id' and 'timeZone'.")
    return Region(
        id=region_id,
        label=str(entry.get("label") or region_id).strip(),
        time_zone=zone,
        note=str(entry.get("note") or "").strip(),
    )


def _parse_event(entry: object, where: str, index: int, region_id: str, *, origin: str) -> Event:
    if not isinstance(entry, dict):
        raise ScheduleError(f"{where}: event #{index + 1} must be an object.")
    event_id = str(entry.get("id") or "").strip()
    name = str(entry.get("name") or "").strip()
    if not event_id or not name:
        raise ScheduleError(f"{where}: event #{index + 1} needs 'id' and 'name'.")

    kind = str(entry.get("kind") or "event").strip().casefold()
    if kind not in KINDS:
        kind = "event"

    schedules: dict[str, Spec] = {}
    raw = entry.get("schedules")
    if isinstance(raw, dict):
        for key, spec_payload in raw.items():
            schedules[str(key)] = _parse_spec(
                spec_payload, where, f"{event_id}.schedules.{key}", event_id
            )

    return Event(
        id=event_id,
        name=name,
        icon=str(entry.get("icon") or "").strip(),
        kind=kind,
        description=str(entry.get("description") or "").strip(),
        duration_minutes=_minutes(entry.get("durationMinutes"), where, event_id),
        portal_minutes=_minutes(entry.get("portalMinutes"), where, event_id),
        schedules=schedules,
        origin=origin,
    )


def _parse_spec(payload: object, where: str, path: str, event_id: str) -> Spec:
    if not isinstance(payload, dict):
        raise ScheduleError(f"{where}: {event_id} {path} must be an object.")

    kind = str(payload.get("type") or "").strip().casefold()
    zone = payload.get("timeZone")
    time_zone = str(zone).strip() if zone else None

    if kind == "hourly":
        minute = payload.get("minute")
        if not isinstance(minute, int) or not 0 <= minute <= 59:
            raise ScheduleError(
                f"{where}: {event_id} {path} is hourly but its 'minute' is not 0-59."
            )
        return Spec(kind=kind, minute=minute, time_zone=time_zone)

    if kind in ("daily", "weekly"):
        raw_times = payload.get("times")
        if not isinstance(raw_times, list) or not raw_times:
            raise ScheduleError(f"{where}: {event_id} {path} has no 'times'.")
        times = tuple(sorted(_parse_time(value, where, f"{event_id} {path}") for value in raw_times))

        days: tuple[int, ...] = ()
        if kind == "weekly":
            raw_days = payload.get("days")
            if not isinstance(raw_days, list) or not raw_days:
                raise ScheduleError(f"{where}: {event_id} {path} is weekly but has no 'days'.")
            days = tuple(
                sorted(_parse_day(value, where, f"{event_id} {path}") for value in raw_days)
            )

        raw_labels = payload.get("timeLabels") or []
        labels = tuple(str(value).strip() for value in raw_labels) if isinstance(raw_labels, list) else ()
        return Spec(kind=kind, times=times, days=days, labels=labels, time_zone=time_zone)

    raise ScheduleError(
        f"{where}: {event_id} {path} has an unknown schedule type {kind!r} "
        "(expected hourly, daily or weekly)."
    )


def _parse_time(value: object, where: str, context: str) -> tuple[int, int]:
    match = _TIME_RE.match(str(value).strip())
    if match is None:
        raise ScheduleError(f"{where}: {context} has a bad time {value!r} (expected HH:MM).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleError(f"{where}: {context} has a bad time {value!r} (expected HH:MM).")
    return hour, minute


def _parse_day(value: object, where: str, context: str) -> int:
    day = str(value).strip().casefold()[:3]
    if day not in DAYS:
        raise ScheduleError(
            f"{where}: {context} has a bad weekday {value!r} (expected mon, tue, ...)."
        )
    return DAYS.index(day)


def _minutes(value: object, where: str, event_id: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, (int, float)) or value < 0:
        raise ScheduleError(f"{where}: {event_id} has a bad durationMinutes ({value!r}).")
    return int(value)


def _as_list(value: object, where: str, field_name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScheduleError(f"{where}: '{field_name}' must be a list.")
    return value

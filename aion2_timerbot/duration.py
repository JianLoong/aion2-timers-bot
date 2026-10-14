"""Parsing and formatting of human friendly durations ("90m", "2h30m", "1d")."""

from __future__ import annotations

import re
from datetime import timedelta

__all__ = ["DurationError", "format_timedelta", "parse_duration"]

DEFAULT_UNIT = "m"
DEFAULT_MAXIMUM = timedelta(days=365)
DEFAULT_MINIMUM = timedelta(seconds=1)

_USAGE = "Use a number with a unit such as `90m`, `2h30m`, `1d`, or a bare number of minutes like `45`."


class DurationError(ValueError):
    """Raised when a duration string cannot be understood."""


_UNIT_SECONDS: dict[str, float] = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}

_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([a-z]*)")

_PARTS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def parse_duration(
    text: str,
    *,
    minimum: timedelta = DEFAULT_MINIMUM,
    maximum: timedelta = DEFAULT_MAXIMUM,
    default_unit: str = DEFAULT_UNIT,
) -> timedelta:
    """Turn a duration string into a timedelta.

    A single bare number is read in ``default_unit`` (minutes by default); as soon
    as there is more than one part, every part must carry a unit so that "1h30"
    is rejected rather than silently read as 31 hours.
    """
    if text is None:
        raise DurationError("No duration given. " + _USAGE)

    compact = re.sub(r"\s+", "", str(text)).lower()
    if not compact:
        raise DurationError("No duration given. " + _USAGE)

    matches = list(_TOKEN_RE.finditer(compact))
    if not matches or "".join(m.group(0) for m in matches) != compact:
        raise DurationError(f"Could not read `{text}` as a duration. {_USAGE}")

    if len(matches) > 1 and any(not m.group(2) for m in matches):
        raise DurationError(
            f"Every part of `{text}` needs a unit — write `1h30m` rather than `1h30`."
        )

    seconds = 0.0
    for match in matches:
        unit = match.group(2) or default_unit
        if unit not in _UNIT_SECONDS:
            raise DurationError(f"Unknown time unit `{unit}` in `{text}`.")
        seconds += float(match.group(1)) * _UNIT_SECONDS[unit]

    delta = timedelta(seconds=seconds)
    if delta < minimum:
        raise DurationError(f"`{text}` is too short; the shortest timer is {format_timedelta(minimum)}.")
    if delta > maximum:
        raise DurationError(f"`{text}` is too long; the longest timer is {format_timedelta(maximum)}.")
    return delta


def format_timedelta(delta: timedelta) -> str:
    """Render a timedelta compactly, e.g. ``timedelta(hours=1, minutes=30)`` -> "1h 30m"."""
    remaining = int(delta.total_seconds())
    if remaining <= 0:
        return "0s"
    parts: list[str] = []
    for unit, size in _PARTS:
        value, remaining = divmod(remaining, size)
        if value:
            parts.append(f"{value}{unit}")
    return " ".join(parts)

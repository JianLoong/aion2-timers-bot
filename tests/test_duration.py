from datetime import timedelta

import pytest

from aion2_timerbot.duration import DurationError, format_timedelta, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90", timedelta(minutes=90)),
        ("45s", timedelta(seconds=45)),
        ("30m", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("1h 30m", timedelta(hours=1, minutes=30)),
        (" 2h30m ", timedelta(hours=2, minutes=30)),
        ("1.5h", timedelta(minutes=90)),
        ("1d", timedelta(days=1)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("1w", timedelta(weeks=1)),
        ("2 hours", timedelta(hours=2)),
        ("1H30M", timedelta(hours=1, minutes=30)),
    ],
)
def test_parses_readable_durations(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "banana", "h", "1h30", "1h30m!", "-5m", "0", "0s", "10 minutes and 3", "1y", "400d"],
)
def test_rejects_bad_durations(text):
    with pytest.raises(DurationError):
        parse_duration(text)


def test_multi_part_duration_demands_a_unit_on_every_part():
    # "1h30" would otherwise silently mean 31 hours.
    with pytest.raises(DurationError):
        parse_duration("1h30")


def test_bounds_are_configurable():
    assert parse_duration("5m", maximum=timedelta(minutes=10)) == timedelta(minutes=5)
    with pytest.raises(DurationError):
        parse_duration("11m", maximum=timedelta(minutes=10))
    with pytest.raises(DurationError):
        parse_duration("1s", minimum=timedelta(minutes=1))


def test_default_unit_is_minutes():
    assert parse_duration("15") == timedelta(minutes=15)
    assert parse_duration("15", default_unit="s") == timedelta(seconds=15)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(0), "0s"),
        (timedelta(seconds=-5), "0s"),
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=90), "1h 30m"),
        (timedelta(days=1, hours=1, minutes=1, seconds=1), "1d 1h 1m 1s"),
        (timedelta(days=7), "7d"),
    ],
)
def test_formats_durations(delta, expected):
    assert format_timedelta(delta) == expected


def test_error_messages_name_the_bad_input():
    with pytest.raises(DurationError, match="1y"):
        parse_duration("1y")
    with pytest.raises(DurationError, match="1h30"):
        parse_duration("1h30")

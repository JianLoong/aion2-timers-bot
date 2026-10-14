from pathlib import Path

import pytest

from aion2_timerbot.config import Config, ConfigError


def test_requires_a_token():
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        Config.from_env({})
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        Config.from_env({"DISCORD_TOKEN": "   "})


def test_defaults_are_sane():
    config = Config.from_env({"DISCORD_TOKEN": "abc"})
    assert config.token == "abc"
    assert config.db_path == Path("data/timers.db")
    assert config.dev_guild_id is None
    assert config.presets_file is None
    assert config.tick_seconds == 5.0
    assert config.max_timers_per_guild == 100
    assert config.log_level == "INFO"


def test_reads_every_setting():
    config = Config.from_env(
        {
            "DISCORD_TOKEN": "abc",
            "DB_PATH": "tmp/custom.db",
            "PRESETS_FILE": "custom-presets.json",
            "DEV_GUILD_ID": "123456789012345678",
            "TICK_SECONDS": "2.5",
            "MISSED_AFTER_SECONDS": "0",
            "MAX_TIMERS_PER_GUILD": "5",
            "LOG_LEVEL": "debug",
        }
    )
    assert config.db_path == Path("tmp/custom.db")
    assert config.presets_file == Path("custom-presets.json")
    assert config.dev_guild_id == 123456789012345678
    assert config.tick_seconds == 2.5
    assert config.missed_after_seconds == 0.0
    assert config.max_timers_per_guild == 5
    assert config.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"DISCORD_TOKEN": "a", "DEV_GUILD_ID": "not-a-number"}, "DEV_GUILD_ID"),
        ({"DISCORD_TOKEN": "a", "TICK_SECONDS": "fast"}, "TICK_SECONDS"),
        ({"DISCORD_TOKEN": "a", "TICK_SECONDS": "0"}, "TICK_SECONDS"),
        ({"DISCORD_TOKEN": "a", "MAX_TIMERS_PER_GUILD": "0"}, "MAX_TIMERS_PER_GUILD"),
        ({"DISCORD_TOKEN": "a", "MAX_TIMERS_PER_GUILD": "lots"}, "MAX_TIMERS_PER_GUILD"),
    ],
)
def test_bad_values_are_reported_with_the_variable_name(env, match):
    with pytest.raises(ConfigError, match=match):
        Config.from_env(env)


def test_dev_guild_id_is_optional():
    assert Config.from_env({"DISCORD_TOKEN": "a", "DEV_GUILD_ID": ""}).dev_guild_id is None

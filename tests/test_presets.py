import json

import pytest

from aion2_timerbot.models import GuildPreset
from aion2_timerbot.presets import BUILTIN_PATH, PresetError, build_library, load_library


def test_bundled_presets_load_and_are_usable():
    library = load_library(BUILTIN_PATH)
    assert library.presets, "the bundled preset file should ship at least one preset"
    for preset in library.presets:
        assert preset.interval_seconds > 0
        assert preset.source == "builtin"


def test_lookup_ignores_case_whitespace_and_uses_aliases():
    library = load_library(BUILTIN_PATH)
    assert library.get("World Boss") is not None
    assert library.get("world  boss") is library.get("WORLD BOSS")
    assert library.get("wb") is library.get("World Boss")
    assert library.get("nope") is None


def test_sorted_groups_by_category_then_name():
    library = load_library(BUILTIN_PATH)
    categories = [p.category for p in library.sorted()]
    assert categories == sorted(categories, key=str.casefold)
    assert library.categories() == list(dict.fromkeys(categories))


def test_guild_preset_overrides_builtin_of_same_name():
    builtin = load_library(BUILTIN_PATH)
    original = builtin.get("World Boss")
    assert original is not None

    guild_preset = GuildPreset(
        guild_id=1,
        key="world boss",
        display_name="World Boss",
        interval_seconds=1234.0,
        category="Open world",
        aliases=("wb2",),
    )
    merged = builtin.merged([guild_preset])

    assert merged.get("World Boss").interval_seconds == 1234.0
    assert merged.get("World Boss").source == "guild"
    assert merged.get("wb2") is merged.get("World Boss")
    # the built-in alias must now point at the guild version, not the old one
    assert merged.get("wb").interval_seconds == 1234.0
    # and the original library is untouched
    assert builtin.get("World Boss").interval_seconds == original.interval_seconds


def test_guild_presets_are_added_next_to_builtins():
    merged = build_library(
        [
            GuildPreset(
                guild_id=1,
                key="ancient boss",
                display_name="Ancient Boss",
                interval_seconds=3600.0,
                category="Custom",
            )
        ],
        path=BUILTIN_PATH,
    )
    assert merged.get("Ancient Boss").source == "guild"
    assert merged.get("World Boss") is not None


def test_missing_file_is_not_fatal(tmp_path):
    library = load_library(tmp_path / "absent.json")
    assert library.presets == []
    assert library.get("anything") is None


def test_bare_list_file_is_accepted(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps([{"name": "Boss", "interval": "2h"}]), encoding="utf-8")
    assert load_library(path).get("boss").interval_seconds == 7200


def test_aliases_accept_a_comma_separated_string(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(
        json.dumps({"presets": [{"name": "Rift", "interval": "2h", "aliases": "rf, rifts"}]}),
        encoding="utf-8",
    )
    library = load_library(path)
    assert library.get("rf") is library.get("Rift")
    assert library.get("rifts") is library.get("Rift")


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps({"presets": "nope"}),
        json.dumps({"presets": [{"interval": "2h"}]}),
        json.dumps({"presets": [{"name": "Boss"}]}),
        json.dumps({"presets": [{"name": "Boss", "interval": "soon"}]}),
        json.dumps({"presets": [{"name": "Boss", "interval": "2h", "aliases": 5}]}),
        json.dumps({"presets": [{"name": "Boss", "interval": "2h"}, {"name": "boss", "interval": "1h"}]}),
    ],
)
def test_bad_preset_files_are_reported_clearly(tmp_path, payload):
    path = tmp_path / "presets.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(PresetError):
        load_library(path)

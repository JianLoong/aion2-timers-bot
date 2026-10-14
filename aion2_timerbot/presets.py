"""Built-in and per-guild presets for recurring Aion 2 content.

Built-ins come from ``data/presets.json`` (override the location with PRESETS_FILE).
Their intervals are examples to be edited for your server, not official game data.
Guild presets created with ``/timer preset add`` are merged on top and win on a name clash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from .duration import DurationError, parse_duration
from .models import GuildPreset, name_key

__all__ = ["Preset", "PresetError", "PresetLibrary", "build_library", "load_library"]

log = logging.getLogger(__name__)

BUILTIN_PATH = Path(__file__).with_name("data") / "presets.json"


class PresetError(Exception):
    """Raised when a preset file cannot be read."""


@dataclass(slots=True)
class Preset:
    key: str
    display_name: str
    interval_seconds: float
    category: str = "Custom"
    aliases: tuple[str, ...] = ()
    note: str | None = None
    source: str = "builtin"

    @property
    def lookup_keys(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)


@dataclass(slots=True)
class PresetLibrary:
    """Name/alias lookup over a set of presets."""

    presets: list[Preset] = field(default_factory=list)
    _by_key: dict[str, Preset] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {}
        for preset in self.presets:
            self._index(preset)

    def _index(self, preset: Preset) -> None:
        self._by_key.setdefault(preset.key, preset)
        for alias in preset.aliases:
            self._by_key.setdefault(name_key(alias), preset)

    def with_preset(self, preset: Preset) -> None:
        """Replace an existing preset of the same key, or append a new one."""
        existing = self._by_key.get(preset.key)
        if existing is not None:
            self.presets[self.presets.index(existing)] = preset
        else:
            self.presets.append(preset)
        self._by_key[preset.key] = preset
        for alias in preset.aliases:
            self._by_key[name_key(alias)] = preset

    def get(self, name: str) -> Preset | None:
        """Look a preset up by name or alias, ignoring case and extra whitespace."""
        return self._by_key.get(name_key(name))

    def names(self) -> list[str]:
        return [p.display_name for p in self.sorted()]

    def sorted(self) -> list[Preset]:
        return sorted(self.presets, key=lambda p: (p.category.casefold(), p.display_name.casefold()))

    def categories(self) -> list[str]:
        seen: list[str] = []
        for preset in self.sorted():
            if preset.category not in seen:
                seen.append(preset.category)
        return seen

    def merged(self, guild_presets: Iterable[GuildPreset]) -> "PresetLibrary":
        """Return a copy where each guild preset replaces any built-in name it takes over."""
        guild_presets = list(guild_presets)
        taken = {name_key(p.display_name) for p in guild_presets}
        taken.update(name_key(alias) for p in guild_presets for alias in p.aliases)

        kept = [
            preset
            for preset in self.presets
            if not ({preset.key, *(name_key(a) for a in preset.aliases)} & taken)
        ]
        merged = PresetLibrary(kept)
        for guild_preset in guild_presets:
            preset = _from_guild(guild_preset)
            # A guild preset that takes over a built-in's name keeps the built-in's
            # aliases, so a shortcut like "wb" still works after the edit.
            inherited = self._by_key.get(preset.key)
            if inherited is not None and inherited.key == preset.key:
                known = {name_key(alias) for alias in preset.aliases} | {preset.key}
                extra = tuple(alias for alias in inherited.aliases if name_key(alias) not in known)
                if extra:
                    preset = replace(preset, aliases=preset.aliases + extra)
            merged.with_preset(preset)
        return merged


def _from_guild(preset: GuildPreset) -> Preset:
    return Preset(
        key=name_key(preset.display_name),
        display_name=preset.display_name,
        interval_seconds=preset.interval_seconds,
        category=preset.category,
        aliases=tuple(preset.aliases),
        note=preset.note,
        source="guild",
    )


def build_library(
    guild_presets: Iterable[GuildPreset] = (),
    *,
    path: Path | None = None,
) -> PresetLibrary:
    """Load the built-in presets and layer this guild's own presets on top."""
    return load_library(path or BUILTIN_PATH).merged(guild_presets)


def load_library(path: Path = BUILTIN_PATH) -> PresetLibrary:
    """Read a preset JSON file. A missing file yields an empty library."""
    path = Path(path)
    if not path.exists():
        log.warning("preset file %s not found; continuing with guild presets only", path)
        return PresetLibrary([])

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PresetError(f"Could not read preset file {path}: {exc}") from exc

    entries = raw.get("presets") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise PresetError(f"{path}: expected an object with a 'presets' list, or a list of presets.")

    presets: list[Preset] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        preset = _parse_entry(entry, path=path, index=index)
        if preset.key in seen:
            raise PresetError(f"{path}: preset #{index + 1} duplicates the name {preset.display_name!r}.")
        seen.add(preset.key)
        seen.update(name_key(alias) for alias in preset.aliases)
        presets.append(preset)
    return PresetLibrary(presets)


def _parse_entry(entry: object, *, path: Path, index: int) -> Preset:
    where = f"{path}: preset #{index + 1}"
    if not isinstance(entry, dict):
        raise PresetError(f"{where} must be an object.")

    name = str(entry.get("name") or "").strip()
    if not name:
        raise PresetError(f"{where} has no 'name'.")

    interval_raw = entry.get("interval")
    if interval_raw is None:
        raise PresetError(f"{where} ({name}) has no 'interval'.")
    try:
        interval = parse_duration(interval_raw).total_seconds()
    except DurationError as exc:
        raise PresetError(f"{where} ({name}) has an unreadable interval: {exc}") from exc

    aliases_raw = entry.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [a for a in aliases_raw.split(",")]
    if not isinstance(aliases_raw, list):
        raise PresetError(f"{where} ({name}) has 'aliases' that are not a list.")

    note = entry.get("note")
    return Preset(
        key=name_key(name),
        display_name=name,
        interval_seconds=interval,
        category=str(entry.get("category") or "Custom").strip() or "Custom",
        aliases=tuple(str(a).strip() for a in aliases_raw if str(a).strip()),
        note=str(note).strip() if note else None,
        source="builtin",
    )

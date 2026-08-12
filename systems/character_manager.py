# -*- coding: utf-8 -*-
"""
Character Manager - Multi-character expression resolution layer.

Sits between the unified emotion system (emotion_scheduler) and the
UI rendering. Its sole responsibility is to map a unified emotion key
to the correct expression key for the currently active character.

Principles:
- The emotion_scheduler is NEVER aware of characters. It only outputs
  unified emotion keys like "HAPPY", "NEUTRAL", etc.
- CharacterManager resolves these to character-specific expressions.
- If a mapping or asset is missing, a fallback chain is used.

Architecture:
    emotion_scheduler → "HAPPY"
           ↓
    CharacterManager.resolve("shinchan", "HAPPY") → "HAPPY"
           ↓
    UI loads assets/characters/shinchan/HAPPY.gif

Extending with a new character:
    1. Add a section to config/expression_mapping.json.
    2. Add assets under assets/characters/<character>/.
    3. Register in VALID_CHARACTERS if needed (for menu display).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from common.paths import (
    CHARACTER_ASSETS_DIR,
    CHARACTER_PROFILES_FILE,
    EXPRESSION_MAPPING_FILE,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAPPING_PATH = EXPRESSION_MAPPING_FILE
_PROFILES_PATH = CHARACTER_PROFILES_FILE


def _load_profiles():
    try:
        with open(_PROFILES_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[CharacterManager] Failed to load profiles: {exc}")
        return {
            "personal": {"display_name": "老勺子"},
        }


_PROFILES = _load_profiles()


CHARACTER_DISPLAY_NAMES: dict[str, str] = {
    cid: p.get("display_name", cid) for cid, p in _PROFILES.items()
}

# Stable character order follows character_profiles.json.
CHARACTER_IDS: tuple[str, ...] = tuple(CHARACTER_DISPLAY_NAMES.keys())

# All valid character identifiers
VALID_CHARACTERS: set[str] = set(CHARACTER_IDS)

# The main application is the standalone Laoshaozi mode. Never derive the
# default from a set: hash randomization previously made startup alternate
# unpredictably between personal and shinchan.
DEFAULT_CHARACTER: str = (
    "personal" if "personal" in VALID_CHARACTERS
    else (CHARACTER_IDS[0] if CHARACTER_IDS else "personal")
)

# Ultimate fallback expression when everything else fails
FALLBACK_EXPRESSION: str = "NEUTRAL"


# ---------------------------------------------------------------------------
# CharacterManager
# ---------------------------------------------------------------------------

@dataclass
class CharacterManager:
    """Resolves unified emotions into character-specific expressions.

    Usage:
        mgr = CharacterManager()
        mgr.set_character("shinchan")
        expression = mgr.resolve("WORKING")  # → "THINKING" (shinchan maps working→thinking)
        expression = mgr.resolve("HAPPY")    # → "HAPPY" (identity mapping)

    The manager:
    1. Loads expression_mapping.json at init time.
    2. Provides set_character() to switch the active character.
    3. Provides resolve() to map a unified emotion to a character expression.
    4. Handles fallback: if an emotion has no mapping, the emotion itself
       is used. If the mapped expression is not in the character's known
       expressions, falls back to the character's NEUTRAL.
    """

    current_character: str = DEFAULT_CHARACTER
    _mapping: dict = field(default_factory=dict)
    _loaded: bool = False
    _profiles: dict = field(default_factory=lambda: dict(_PROFILES))

    def __post_init__(self) -> None:
        if self.current_character not in self._profiles:
            raise ValueError(f"Unknown character: {self.current_character}")
        self._load_mapping()

    # ── Mapping loading ────────────────────────────────────────────────

    def _load_mapping(self) -> None:
        """Load expression_mapping.json into memory."""
        try:
            with open(_MAPPING_PATH, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            # Strip metadata keys (those starting with _)
            self._mapping = {
                k: v for k, v in raw.items() if not k.startswith("_")
            }
            self._loaded = True
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"[CharacterManager] Failed to load mapping: {exc}")
            self._mapping = {}
            self._loaded = False

    def reload_mapping(self) -> None:
        """Hot-reload the mapping file (useful during development)."""
        self._load_mapping()

    # ── Character switching ────────────────────────────────────────────

    def set_character(self, character: str) -> bool:
        """Switch the active character.

        Args:
            character: Character id (e.g. "personal", "shinchan").

        Returns:
            True if the switch succeeded, False if the character is invalid.
        """
        if character not in VALID_CHARACTERS:
            print(f"[CharacterManager] Unknown character: {character}")
            return False
        if character == self.current_character:
            return True
        self.current_character = character
        return True

    def get_display_name(self) -> str:
        """Return the display name of the current character."""
        return CHARACTER_DISPLAY_NAMES.get(
            self.current_character, self.current_character
        )

    def get_all_characters(self) -> list[tuple[str, str]]:
        """Return all available characters as (id, display_name) pairs."""
        return [(cid, CHARACTER_DISPLAY_NAMES.get(cid, cid))
                for cid in CHARACTER_IDS]

    def get_profile(self, character=None):
        char = character or self.current_character
        return self._profiles.get(char, {})

    def reload_profiles(self):
        global _PROFILES, CHARACTER_DISPLAY_NAMES, CHARACTER_IDS, VALID_CHARACTERS
        _PROFILES = _load_profiles()
        CHARACTER_DISPLAY_NAMES = {
            cid: p.get("display_name", cid) for cid, p in _PROFILES.items()
        }
        CHARACTER_IDS = tuple(CHARACTER_DISPLAY_NAMES.keys())
        VALID_CHARACTERS = set(CHARACTER_IDS)
        self._profiles = dict(_PROFILES)

    # ── Expression resolution ──────────────────────────────────────────

    def resolve(self, emotion: str) -> str:
        """Map a unified emotion to a character-specific expression.

        Resolution order:
        1. Normalize emotion to lowercase for mapping lookup.
        2. Look up mapping[current_character][emotion_lower].
        3. If found, return the mapped expression.
        4. If not found in mapping, use the emotion itself (identity).
        5. The returned value is the expression key to use for asset lookup.

        Args:
            emotion: Unified emotion key from emotion_scheduler (e.g. "HAPPY").

        Returns:
            Character-specific expression key (e.g. "HAPPY" or "THINKING").
        """
        emotion_lower = emotion.lower()

        char_mapping = self._mapping.get(self.current_character, {})
        if char_mapping and emotion_lower in char_mapping:
            return char_mapping[emotion_lower]

        # Identity: use the emotion as-is (uppercase normalized)
        return emotion.upper()

    def resolve_with_fallback(self, emotion: str, available: set[str]) -> str:
        """Resolve with awareness of which assets actually exist.

        If the resolved expression does not exist in `available`,
        falls back to the character's NEUTRAL expression.

        Args:
            emotion: Unified emotion key.
            available: Set of expression keys that have actual image files.

        Returns:
            Expression key guaranteed to be in available (or FALLBACK_EXPRESSION).
        """
        expression = self.resolve(emotion)
        if expression in available:
            return expression

        # Try the character's NEUTRAL
        neutral = self.resolve("NEUTRAL")
        if neutral in available:
            return neutral

        # Ultimate fallback
        return FALLBACK_EXPRESSION

    # ── Helpers ────────────────────────────────────────────────────────

    def get_character_asset_dir(self, character: Optional[str] = None) -> str:
        """Get the assets directory for a character.

        Args:
            character: Character id. Defaults to current character.

        Returns:
            Absolute path to assets/characters/<character>/.
        """
        char = character or self.current_character
        return os.fspath(CHARACTER_ASSETS_DIR / char)

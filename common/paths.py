"""Canonical filesystem locations for the toppet project."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
ASSETS_DIR = PROJECT_ROOT / "assets"
CHARACTER_ASSETS_DIR = ASSETS_DIR / "characters"
AUDIO_ASSETS_DIR = ASSETS_DIR / "audio"
WHISPER_MODELS_DIR = ASSETS_DIR / "whisper_models"

DOCS_DIR = PROJECT_ROOT / "docs"
TOOLS_DIR = PROJECT_ROOT / "tools"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
LOGS_DIR = PROJECT_ROOT / "logs"

CHARACTER_PROFILES_FILE = CONFIG_DIR / "character_profiles.json"
EXPRESSION_MAPPING_FILE = CONFIG_DIR / "expression_mapping.json"
API_KEY_FILE = PROJECT_ROOT / "api_key.txt"
VOICE_REFERENCE_FILE = AUDIO_ASSETS_DIR / "ref_normal.wav"

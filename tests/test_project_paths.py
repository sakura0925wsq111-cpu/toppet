from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.asr.whisper_asr import FasterWhisperEngine  # noqa: E402
from common.paths import (  # noqa: E402
    CHARACTER_ASSETS_DIR,
    CHARACTER_PROFILES_FILE,
    EXPRESSION_MAPPING_FILE,
    PROJECT_ROOT,
    VOICE_REFERENCE_FILE,
    WHISPER_MODELS_DIR,
)
from systems.character_manager import CharacterManager  # noqa: E402
from systems.voxcpm_service_manager import DEFAULT_REFERENCE_PATH  # noqa: E402


class ProjectPathTests(unittest.TestCase):
    def test_config_and_character_assets_use_canonical_directories(self):
        self.assertTrue(CHARACTER_PROFILES_FILE.is_file())
        self.assertTrue(EXPRESSION_MAPPING_FILE.is_file())
        self.assertEqual(
            Path(CharacterManager().get_character_asset_dir()),
            CHARACTER_ASSETS_DIR / "personal",
        )

    def test_audio_assets_are_resolved_independently_of_working_directory(self):
        self.assertEqual(DEFAULT_REFERENCE_PATH, VOICE_REFERENCE_FILE)
        self.assertTrue(VOICE_REFERENCE_FILE.is_file())
        self.assertEqual(
            Path(FasterWhisperEngine.DEFAULT_MODEL_DIR),
            WHISPER_MODELS_DIR,
        )

    def test_project_root_is_repository_root(self):
        self.assertEqual(PROJECT_ROOT, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()

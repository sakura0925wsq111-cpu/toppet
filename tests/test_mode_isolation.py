from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.config import PetConfig  # noqa: E402
from core.pet_brain import PetBrain  # noqa: E402
from laoshaozi import create_laoshaozi  # noqa: E402
from systems.character_manager import DEFAULT_CHARACTER  # noqa: E402


class LaoshaoziBrainTests(unittest.TestCase):
    def setUp(self):
        self.brain = PetBrain(
            PetConfig(api_key="test-key"),
            character="personal",
        )

    def tearDown(self):
        self.brain.close(wait=True)

    def test_default_mode_is_deterministically_personal(self):
        self.assertEqual(DEFAULT_CHARACTER, "personal")
        self.assertEqual(self.brain.current_character, "personal")
        self.assertEqual(
            self.brain.character_manager.current_character,
            "personal",
        )

    def test_persona_and_memory_are_laoshaozi_only(self):
        prompt = self.brain.system_prompt
        self.assertIn("\u8001\u52fa\u5b50", prompt)
        self.assertNotIn("\u91ce\u539f\u65b0\u4e4b\u52a9", prompt)
        self.assertTrue(
            self.brain._get_memory().file_path.endswith("memory_personal.json")
        )

    def test_local_time_reply_runs_without_ai_api(self):
        self.brain.client._client = None
        reply = self.brain.process("\u73b0\u5728\u51e0\u70b9\u4e86")
        self.assertTrue(reply.is_local)
        self.assertIn("\u5446\u5446\u5b9d\u5b9d", reply.content)


class LaoshaoziDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_loads_only_personal_mode_assets(self):
        brain, pet = create_laoshaozi(
            PetConfig(api_key="test-key"),
            start_background_services=False,
        )
        try:
            loaded = set(pet.movies) | set(pet.pixmaps)
            self.assertEqual(pet.mode_id, "personal")
            self.assertEqual(pet.character_manager.current_character, "personal")
            self.assertEqual(len(pet._character_expressions["personal"]), 20)
            self.assertTrue(loaded)
            self.assertTrue(all(key.startswith("personal/") for key in loaded))
            self.assertFalse(any(key.startswith("shinchan/") for key in loaded))
            self.assertIsNone(pet.tts_service)
            self.assertIn("VoxCPM2", pet.tts_manager.engine_name)
            self.assertIn("Yunxi", pet.tts_manager.engine_name)
        finally:
            pet.shutdown_services()
            pet.deleteLater()

    def test_text_request_completes_through_standalone_window(self):
        brain, pet = create_laoshaozi(
            PetConfig(api_key="test-key"),
            start_background_services=False,
        )
        brain.client._client = None
        spoken: list[str] = []
        pet.tts_manager.begin_stream = lambda _session_id: None
        pet.tts_manager.enqueue_text = (
            lambda _session_id, text: spoken.append(text) or True
        )
        pet.tts_manager.end_stream = lambda _session_id: None
        try:
            pet._on_send("\u73b0\u5728\u51e0\u70b9\u4e86")
            deadline = time.monotonic() + 5
            while pet.is_ai_busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

            self.app.processEvents()
            self.assertFalse(pet.is_ai_busy)
            self.assertNotIn("\u5904\u7406\u5931\u8d25", pet.chat_output.text())
            self.assertEqual("".join(spoken), pet.chat_output.text())
        finally:
            pet._tts_pending = False
            pet.shutdown_services()
            pet.deleteLater()


if __name__ == "__main__":
    unittest.main()

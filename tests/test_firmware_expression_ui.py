from __future__ import annotations

import importlib.util
import re
import struct
import tempfile
import unittest
from pathlib import Path

from systems.idle_scheduler import IDLE_ACTION_POOL


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_SOURCE = ROOT / "firmware" / "toppet_pet" / "toppet_pet.ino"
GENERATOR_PATH = ROOT / "firmware" / "tools" / "generate_expression_assets.py"
PERSONAL_EXPRESSIONS = ROOT / "characters" / "personal" / "expressions"


def _load_generator():
    spec = importlib.util.spec_from_file_location("expression_asset_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load expression asset generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FirmwareExpressionUiTests(unittest.TestCase):
    def test_default_asset_has_no_fixed_speech_bubble(self):
        generator = _load_generator()
        self.assertEqual(generator.EXPRESSIONS["NEUTRAL"], "")
        self.assertEqual(
            generator.DEFAULT_SOURCE_DIR,
            ROOT / "assets" / "characters" / "personal",
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "NEUTRAL.tpa"
            generator.build_asset(
                "NEUTRAL",
                "",
                PERSONAL_EXPRESSIONS / "NEUTRAL.gif",
                output,
                Path("C:/Windows/Fonts/msyhbd.ttc"),
                None,
            )
            payload = output.read_bytes()
            header = generator.ASSET_HEADER.unpack_from(payload)
            text_mask_offset = header[7]
            text_mask_size = header[8]
            self.assertEqual(
                payload[text_mask_offset : text_mask_offset + text_mask_size],
                bytes(text_mask_size),
            )

    def test_firmware_keeps_default_listening_and_thinking_states(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        state_block = re.search(
            r"enum class ButtonTestState[^\{]*\{(?P<body>.*?)\};",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(state_block)
        self.assertIn("IDLE", state_block.group("body"))
        self.assertIn("LISTENING", state_block.group("body"))
        self.assertIn("THINKING", state_block.group("body"))
        self.assertIn("UiCommandType::THINKING", source)

    def test_reply_animation_is_independent_from_normal_audio_playback(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        start = source.index("void uiTask(void *parameter) {")
        end = source.index("\n\nvoid createVoiceTasks", start)
        body = source[start:end]
        self.assertIn("EVENT_PLAYBACK_LOW_WATER", body)
        self.assertNotIn("EVENT_PLAYBACK_ACTIVE", body)
        self.assertIn("serviceIdleExpressionFlow", body)
        self.assertIn('"AudioPlaybackTask"', source)
        self.assertIn('"UiTask"', source)

    def test_expression_switching_uses_psram_caches_and_background_decode(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("EXPRESSION_CACHE_SLOT_COUNT = 5", source)
        self.assertIn("EXPRESSION_COMMON_CACHE_COUNT = 3", source)
        self.assertIn('"ExpressionDecodeTask"', source)
        self.assertIn("xQueueOverwrite(expression_load_queue", source)
        self.assertIn("renderExpressionDelta", source)
        self.assertIn("expression_previews[EXPRESSION_COUNT]", source)
        self.assertIn("DISPLAY_BUS_HZ = 80 * 1000 * 1000", source)
        self.assertIn("expression_switch emotion=", source)
        cache_bytes = (20 + 5 * 4) * 420 * 340
        self.assertLess(cache_bytes, 6 * 1024 * 1024)

    def test_duplicate_subtitles_are_not_redrawn(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("subtitle_hash == last_subtitle_hash", source)
        self.assertIn("duplicate subtitle skipped", source)

    def test_response_expression_and_subtitle_share_one_ui_transaction(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("UiCommandType::RESPONSE", source)
        self.assertIn('type == "response_ui"', source)
        self.assertIn("applyHostResponseUi", source)
        self.assertIn("pending_response_emotion", source)
        self.assertIn("SUBTITLE_LEAD_FRAMES = 4", source)
        self.assertIn("EVENT_UI_SYNC_READY", source)
        self.assertIn("response sync timeout before playback", source)
        self.assertIn("EXPRESSION_DIRTY_MASK_BYTES", source)

    def test_direct_playback_diagnostics_cover_audio_concurrency(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("#define TOPPET_ENABLE_USB_TESTS 0", source)
        self.assertIn('startsWith("TEST_TONE ")', source)
        self.assertIn("ACK TEST_TONE", source)

    def test_firmware_idle_pool_matches_desktop_pool(self):
        source = FIRMWARE_SOURCE.read_text(encoding="utf-8")
        pool_block = re.search(
            r"IDLE_EXPRESSION_ACTIONS\[\][^\{]*\{(?P<body>.*?)\};",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(pool_block)
        firmware_pool = {
            emotion: int(weight)
            for emotion, weight in re.findall(
                r'\{"([A-Z_]+)",\s*(\d+)\}', pool_block.group("body")
            )
        }
        desktop_pool = {
            action.expression: round(action.weight * 10)
            for action in IDLE_ACTION_POOL
        }
        self.assertEqual(firmware_pool, desktop_pool)


if __name__ == "__main__":
    unittest.main()

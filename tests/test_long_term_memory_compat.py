from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.long_term_memory import LongTermMemory, UserMemory


class LongTermMemoryCompatibilityTests(unittest.TestCase):
    def test_legacy_string_recent_events_remain_readable_and_mergeable(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = LongTermMemory(str(Path(directory) / "memory.json"))
            memory.memory = UserMemory(
                recent_events=[
                    "旧格式事件",
                    {"date": "2026-08-23", "summary": "新格式事件"},
                ]
            )
            prompt = memory.get_prompt_text()
            self.assertIn("旧格式事件", prompt)
            self.assertIn("新格式事件", prompt)
            memory._merge_updates({
                "recent_events": [
                    {"date": "2026-08-24", "summary": "后来发生的事"}
                ]
            })
            self.assertEqual(memory.memory.recent_events[0]["summary"], "后来发生的事")


if __name__ == "__main__":
    unittest.main()

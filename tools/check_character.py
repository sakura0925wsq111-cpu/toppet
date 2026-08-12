import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from systems.character_manager import CharacterManager

mgr = CharacterManager()
print("Default:", mgr.current_character)

mgr.set_character("shinchan")
tests = [
    ("WORKING", "THINKING"),
    ("READING", "EATING"),
    ("LISTENING", "THINKING"),
    ("HEART", "LOVE"),
    ("CELEBRATE", "HAPPY"),
    ("HELLO", "GREETING"),
    ("HAPPY", "HAPPY"),
]
all_ok = True
for emotion, expected in tests:
    result = mgr.resolve(emotion)
    ok = result == expected
    if not ok:
        all_ok = False
    print(f"  {emotion} -> {result} (expected {expected}) {'OK' if ok else 'FAIL'}")

mgr.set_character("personal")
print("Personal resolve HAPPY:", mgr.resolve("HAPPY"))
print("All characters:", mgr.get_all_characters())
print()
print("ALL OK" if all_ok else "SOME FAILED")

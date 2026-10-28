"""Existing desktop tests also instantiate real memories: isolate all runtime writes."""
from pathlib import Path
import os
import tempfile
import uuid
import pytest


def pytest_configure(config):
    """Keep this project's tests and caches in its ignored artifact directory."""
    root = Path(__file__).resolve().parents[1] / 'artifacts' / 'pytest-temp'
    root.mkdir(parents=True, exist_ok=True)
    os.environ['TEMP'] = str(root)
    os.environ['TMP'] = str(root)
    tempfile.tempdir = None
    if not config.option.basetemp:
        config.option.basetemp = str(root / uuid.uuid4().hex)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    import core.pet_brain
    import core.long_term_memory
    monkeypatch.setattr(core.pet_brain, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(core.long_term_memory, "LOGS_DIR", tmp_path)

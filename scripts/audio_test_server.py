"""Isolated browser-test API. Never falls back to the user's project directory."""
import os
from dataclasses import replace
from pathlib import Path

path = os.environ.get("AUDIO_TEST_DATA_DIR")
if not path:
    raise RuntimeError("AUDIO_TEST_DATA_DIR is required for the browser-test server")
# main.py creates its default app at import; set the test directory first too.
os.environ["DATA_DIR"] = str(Path(path).resolve())

from backend.app.config import Settings
from backend.app.main import create_app

settings = replace(
    Settings.from_env(),
    data_dir=Path(path).resolve(),
    allowed_origins=("http://127.0.0.1:5174", "http://localhost:5174"),
)
app = create_app(settings)

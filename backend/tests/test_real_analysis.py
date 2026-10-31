"""Opt-in real model smoke test, using generated test audio (never fake output).

RUN_MODEL_TESTS=1 python -m pytest backend/tests/test_real_analysis.py -q
Requires requirements-analysis.txt, pytest/httpx, and FFmpeg. The test accepts
FFMPEG_BINARY or the optional imageio-ffmpeg testing package for its binary.
"""
from __future__ import annotations

import array
import io
import math
import os
import shutil
import wave

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS") != "1", reason="Opt-in real Basic Pitch inference test")


def fixture_audio() -> bytes:
    """Two synthetic, harmonically rich notes, explicitly a development fixture."""
    sample_rate = 22050
    values = array.array("h")
    for i in range(sample_rate * 3):
        t = i / sample_rate
        local = t if t < 1.5 else t - 1.5
        hz = 261.625565 if t < 1.5 else 329.627557
        envelope = max(0, min(1, local / 0.025, (1.25 - local) / 0.05))
        signal = sum(math.sin(2 * math.pi * hz * harmonic * t) / harmonic for harmonic in (1, 2, 3))
        values.append(round(8000 * envelope * signal))
    data = io.BytesIO()
    with wave.open(data, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(sample_rate)
        file.writeframes(values.tobytes())
    return data.getvalue()


def test_real_upload_decode_inference_export_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from backend.app.config import Settings
    from backend.app.main import create_app
    from backend.app.worker import Worker
    from music21 import converter

    ffmpeg = os.getenv("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not ffmpeg:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    settings = Settings(data_dir=tmp_path / "data", ffmpeg_binary=ffmpeg, basic_pitch_runtime="onnx")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/projects", data={"title": "DEVELOPMENT FIXTURE — two generated tones"},
                               files={"file": ("development-fixture.wav", fixture_audio(), "audio/wav")})
        assert response.status_code == 201
        project_id = response.json()["id"]
        worker = Worker(settings)
        assert worker.run_once()
        project = client.get(f"/api/projects/{project_id}").json()
        assert project["status"] == "completed", project["jobs"]
        assert project["note_count"] > 0
        assert {60, 64}.issubset({note["pitch"] for note in project["notes"]}), project["notes"]
        assert project["duration_seconds"] == pytest.approx(3.0)
        assert set(project["exports"]) == {"raw-midi", "musicxml", "midi"}
        for format, suffix in (("musicxml", "musicxml"), ("midi", "mid"), ("raw-midi", "raw.mid")):
            artifact = client.get(f"/api/projects/{project_id}/exports/{format}")
            assert artifact.status_code == 200
            path = tmp_path / f"roundtrip.{suffix}"
            path.write_bytes(artifact.content)
            assert len(converter.parse(path).recurse().notes) > 0

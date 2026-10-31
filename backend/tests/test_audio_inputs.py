"""Audio-input validation fixtures. Generated audio and test-only engine spies.

These tests never stand in for real model quality evaluation. FFmpeg tests use
an installed executable or the explicitly test-only imageio-ffmpeg binary.
"""
from __future__ import annotations

import array
import importlib.util
import io
import math
import shutil
import sqlite3
import subprocess
import sys
import wave
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.app.audio import AnalysisError, normalize_audio, prepare_analysis_audio
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.worker import Worker


def audio_fixture(seconds=1.0, amplitude=8000, silence_until=0.0):
    samples = array.array("h", (
        0 if i / 22050 < silence_until else round(amplitude * math.sin(2 * math.pi * 440 * i / 22050))
        for i in range(round(seconds * 22050))
    ))
    if sys.byteorder != "little":
        samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(22050)
        stream.writeframes(samples.tobytes())
    return output.getvalue()


@pytest.fixture
def ffmpeg():
    binary = shutil.which("ffmpeg")
    if not binary and importlib.util.find_spec("imageio_ffmpeg"):
        import imageio_ffmpeg
        binary = imageio_ffmpeg.get_ffmpeg_exe()
    if not binary:
        pytest.skip("Install FFmpeg to run real decoder fixture tests")
    return binary


@pytest.fixture
def settings(tmp_path, ffmpeg):
    return Settings(data_dir=tmp_path / "projects-data", ffmpeg_binary=ffmpeg)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def submit(client, content=None, filename="DEVELOPMENT-FIXTURE.wav", **fields):
    return client.post("/api/projects", data=fields,
                       files={"file": (filename, audio_fixture() if content is None else content)})


class ValidationOnlyEngine:
    """Test-only spy verifies analysis receives the chosen PCM, generates no notes."""
    name = "TEST-ONLY-validation-spy"

    def __init__(self):
        self.received = None

    def transcribe(self, path):
        with wave.open(str(path), "rb") as audio:
            self.received = (audio.getnchannels(), audio.getframerate(), audio.getnframes())
        return []


@pytest.mark.parametrize("extension,codec", [("wav", "pcm_s16le"), ("mp3", "libmp3lame"), ("m4a", "aac"), ("flac", "flac")])
def test_real_decoder_formats_preview_and_original_preservation(client, settings, tmp_path, extension, codec):
    original = tmp_path / "input.wav"
    original.write_bytes(audio_fixture(1.5))
    source = tmp_path / f"fixture.{extension}"
    encoded = subprocess.run([settings.ffmpeg_binary, "-nostdin", "-y", "-v", "error", "-i", str(original), "-c:a", codec, str(source)], capture_output=True)
    if encoded.returncode:
        pytest.skip(f"Test binary cannot encode {codec}; decoder support cannot be tested with this fixture")
    data = source.read_bytes()
    preview = client.post("/api/audio-preview", files={"file": (source.name, data)})
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(preview.content)) as audio:
        assert audio.getnchannels() == 1 and audio.getframerate() == 22050
        assert 1.45 < audio.getnframes() / 22050 < 1.6
    assert client.get("/api/projects").json() == []
    assert not list(settings.data_dir.glob("audio-preview-*"))
    created = submit(client, data, source.name, clip_start_seconds=0.25, clip_end_seconds=0.75,
                     instrument_hint="voice", tempo_hint=90, time_signature_hint="3/4")
    assert created.status_code == 201
    project = created.json()
    engine = ValidationOnlyEngine()
    assert Worker(settings, engine).run_once()
    expected_frames = round(0.75 * 22050) - round(0.25 * 22050)
    assert engine.received == (1, 22050, expected_frames)
    with TestClient(create_app(settings)) as refreshed:
        project = refreshed.get(f'/api/projects/{project["id"]}').json()
        assert project["status"] == "completed"
        assert project["clip_start_seconds"] == pytest.approx(0.25, abs=1 / 22050)
        assert project["clip_end_seconds"] == pytest.approx(0.75, abs=1 / 22050)
        assert project["clip_duration_seconds"] == pytest.approx(expected_frames / 22050)
        assert project["original_duration_seconds"] > 1.4
        assert project["instrument_hint"] == "voice"
        assert project["tempo_hint"] == 90 and project["time_signature_hint"] == "3/4"
        assert refreshed.get(f'/api/projects/{project["id"]}/audio').content == data
        clip = refreshed.get(f'/api/projects/{project["id"]}/analysis-audio')
        assert clip.status_code == 200
        with wave.open(io.BytesIO(clip.content)) as audio:
            assert audio.getnframes() == expected_frames


@pytest.mark.parametrize("fields", [
    {"clip_start_seconds": -1}, {"clip_start_seconds": "nan"}, {"clip_start_seconds": "inf"},
    {"clip_start_seconds": 3600}, {"clip_end_seconds": "nan"}, {"clip_end_seconds": "inf"},
    {"clip_end_seconds": -1}, {"clip_start_seconds": 0.9, "clip_end_seconds": 0.5},
    {"clip_end_seconds": 0.05}, {"tempo_hint": 0}, {"tempo_hint": 301},
    {"time_signature_hint": "7/0"}, {"instrument_hint": "not-an-instrument"},
])
def test_invalid_clip_and_hint_metadata_rejected_without_project(client, settings, fields):
    assert submit(client, **fields).status_code == 422
    assert client.get("/api/projects").json() == []
    assert not list((settings.data_dir / "projects").iterdir())


@pytest.mark.parametrize("content,fields,match", [
    (audio_fixture(amplitude=0), {}, "silent"),
    (audio_fixture(seconds=0.05), {}, "shorter"),
    (audio_fixture(silence_until=0.5), {"clip_end_seconds": 0.4}, "silent"),
    (audio_fixture(), {"clip_start_seconds": 1.1}, "outside"),
    (audio_fixture(), {"clip_end_seconds": 1.2}, "beyond"),
    (audio_fixture()[:80], {}, "corrupt"),
])
def test_invalid_decoded_audio_fails_persistently_before_inference(client, settings, content, fields, match):
    created = submit(client, content, **fields)
    assert created.status_code == 201
    engine = ValidationOnlyEngine()
    Worker(settings, engine).run_once()
    assert engine.received is None
    project = client.get(f'/api/projects/{created.json()["id"]}').json()
    assert project["status"] == "failed"
    assert match in project["jobs"][0]["error"]
    assert not project["analysis_available"]
    assert project["notes"] is None and not project["exports"]
    assert client.get(f'/api/projects/{project["id"]}/audio').content == content
    assert not list(settings.data_dir.rglob("normalized.wav"))
    assert not list(settings.data_dir.rglob("decoded-original.wav"))


def test_overlong_original_not_evaded_by_short_clip(settings):
    settings = replace(settings, max_duration_seconds=1)
    with TestClient(create_app(settings)) as client:
        project = submit(client, audio_fixture(1.2), clip_end_seconds=0.3).json()
        engine = ValidationOnlyEngine()
        Worker(settings, engine).run_once()
        result = client.get(f'/api/projects/{project["id"]}').json()
        assert "exceeds" in result["jobs"][0]["error"]
        assert engine.received is None


def test_rounding_tolerance_and_omitted_end(settings, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(audio_fixture())
    for stop in (None, 1.03):
        result = prepare_analysis_audio(source, tmp_path / "clip.wav", settings, 0.2, stop)
        assert result.clip_start == 0.2 and result.clip_end == 1.0


def test_silence_floor_is_configurable(settings, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(audio_fixture(amplitude=3))
    with pytest.raises(AnalysisError, match="silent"):
        prepare_analysis_audio(source, tmp_path / "clip.wav", settings)
    result = prepare_analysis_audio(source, tmp_path / "clip.wav", replace(settings, silence_threshold_dbfs=-100))
    assert result.clip_duration == 1


def test_corrupt_preview_cleans_up_without_creating_project(client, settings):
    response = client.post("/api/audio-preview", files={"file": ("corrupt.wav", audio_fixture()[:80])})
    assert response.status_code == 422
    assert "corrupt" in response.json()["detail"]
    assert not list(settings.data_dir.glob("audio-preview-*"))
    assert client.get("/api/projects").json() == []


def test_decode_wall_timeout_cleanup(monkeypatch, settings, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target.wav"
    def timeout(*args, **kwargs):
        target.write_bytes(b"partial")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr("backend.app.audio.subprocess.run", timeout)
    with pytest.raises(AnalysisError, match="time limit"):
        normalize_audio(source, target, settings)
    assert not target.exists()


def test_schema_migration_preserves_old_project(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,source TEXT NOT NULL,
                original_filename TEXT NOT NULL,media_type TEXT NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,status TEXT NOT NULL,duration_seconds REAL,note_count INTEGER,
                notes_json TEXT,exports_json TEXT NOT NULL DEFAULT '[]');
            INSERT INTO projects VALUES('old','Existing project','upload','old.wav','audio/wav',
                '2026-10-27','2026-10-27','completed',2.0,0,'[]','[]');
            PRAGMA user_version=1;
        """)
    migrated = Database(path)
    migrated.initialize()
    migrated.initialize()  # Repeated process startup is safe.
    project = migrated.get_project("old")
    assert project["title"] == "Existing project" and project["original_duration_seconds"] == 2
    assert project["clip_start_seconds"] == 0 and project["clip_end_seconds"] is None
    assert not project["analysis_available"]


def test_preview_cleanup_when_client_disconnects(tmp_path):
    import asyncio
    from backend.app.main import PreviewResponse
    directory = tmp_path / "transient"
    directory.mkdir()
    path = directory / "preview.wav"
    path.write_bytes(audio_fixture())
    response = PreviewResponse(path, directory)
    async def disconnected_send(message):
        raise OSError("TEST FIXTURE: client disconnected")
    async def receive():
        return {"type": "http.disconnect"}
    async def run():
        with pytest.raises(OSError):
            await response({"type": "http", "method": "GET", "headers": [],
                            "asgi": {"spec_version": "2.4"}}, receive, disconnected_send)
    asyncio.run(run())
    assert not directory.exists()

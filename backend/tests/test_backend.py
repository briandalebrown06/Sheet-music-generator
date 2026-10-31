"""Unit/integration fixtures are synthetic silence, never user transcription output."""
from __future__ import annotations

import io
import json
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.app.audio import AnalysisError, normalize_audio
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.engine import BasicPitchEngine
from backend.app.main import create_app
from backend.app.worker import Worker


def silence_wav(seconds=0.2):
    data = io.BytesIO()
    with wave.open(data, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(b"\0\0" * round(seconds * 22050))
    return data.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", ffmpeg_binary="definitely-no-ffmpeg-test-binary")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client, **kwargs):
    return client.post("/api/projects", files={"file": ("test-fixture-silence.wav", silence_wav(), "audio/wav")}, **kwargs)


def test_project_persists_between_api_instances(client, settings):
    response = upload(client, data={"title": " Piano sketch ", "source": "upload"})
    assert response.status_code == 201
    project = response.json()
    assert project["title"] == "Piano sketch"
    assert project["notes"] is None and project["exports"] == []
    assert project["jobs"][0]["status"] == "queued"
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f'/api/projects/{project["id"]}').json() == project
        assert len(restarted.get("/api/projects").json()) == 1
        assert restarted.get(f'/api/projects/{project["id"]}/audio').content == silence_wav()


def test_empty_library_and_health(client):
    assert client.get("/api/projects").json() == []
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["worker"]["online"] is False
    assert health["capabilities"]["ffmpeg"] is False


@pytest.mark.parametrize("filename,content", [("attack.html", b"<html>"), ("pretend.wav", b"<html>"), ("empty.wav", b"")])
def test_rejects_non_audio(client, filename, content):
    response = client.post("/api/projects", files={"file": (filename, content)})
    assert response.status_code == 415
    assert client.get("/api/projects").json() == []


def test_size_limit_on_file_and_body(settings):
    with TestClient(create_app(replace(settings, max_upload_bytes=100))) as client:
        assert upload(client).status_code == 413
        assert client.post("/api/projects", content=b"a" * 66000).status_code == 413
        assert client.get("/api/projects").json() == []
    assert list((settings.data_dir / "projects").iterdir()) == []


def test_local_browser_boundary(client, settings):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "[bad]"}).status_code == 400
    assert client.get("/api/health", headers={"Host": "localhost:garbage"}).status_code == 400
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://localhost:5173"}).status_code == 200
    with TestClient(create_app(settings), client=("192.0.2.1", 1234)) as remote:
        assert remote.get("/api/health").status_code == 403


def test_microphone_and_filename_sanitization(client):
    response = client.post("/api/projects", data={"source": "microphone"},
                           files={"file": ("../../my take.wav", silence_wav())})
    assert response.status_code == 201
    assert response.json()["original_filename"] == "my take.wav"
    assert response.json()["source"] == "microphone"


def test_missing_project_and_export(client):
    assert client.get("/api/projects/not-a-uuid").status_code == 404
    p = upload(client).json()
    assert client.get(f'/api/projects/{p["id"]}/exports/musicxml').status_code == 404
    assert client.post(f'/api/projects/{p["id"]}/retry').status_code == 409


def test_real_worker_failure_and_retry_preserves_history(client, settings):
    p = upload(client).json()
    worker = Worker(settings)
    assert worker.run_once()
    p = client.get(f'/api/projects/{p["id"]}').json()
    assert p["status"] == "failed"
    assert "FFmpeg is not installed" in p["jobs"][0]["error"]
    assert p["notes"] is None and p["note_count"] is None
    assert client.post(f'/api/projects/{p["id"]}/retry').status_code == 200
    assert client.post(f'/api/projects/{p["id"]}/retry').status_code == 409
    detail = client.get(f'/api/projects/{p["id"]}').json()
    assert len(detail["jobs"]) == 2
    assert [j["status"] for j in detail["jobs"]] == ["queued", "failed"]


def test_transactional_claim_prevents_duplicate_processing(client, settings):
    upload(client)
    db = Database(settings.database_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(db.claim_job, ["worker-one", "worker-two"]))
    assert sum(job is not None for job in claims) == 1


def test_expired_lease_fences_old_worker_and_recovers_within_budget(client, settings):
    p = upload(client).json()
    db = Database(settings.database_path)
    first = db.claim_job("old", lease_seconds=-1)
    second = db.claim_job("new")
    assert db.get_project(p["id"])["status"] == "processing"
    assert db.finish(first, "old", notes=[]) is False
    assert db.get_project(p["id"])["jobs"][1]["stage"] == "Interrupted"
    assert second["id"] != first["id"]
    assert db.finish(second, "new", notes=[], duration=0.2)
    assert db.get_project(p["id"])["notes"] == []


def test_worker_capabilities_override_api_environment(client, settings, monkeypatch):
    db = Database(settings.database_path)
    reported = {"ffmpeg": True, "transcription": True, "notation": True}
    # PDF generation runs in the API interpreter, separately from the worker.
    pdf = {"available": False, "engine": "Verovio", "missing": ["verovio"]}
    monkeypatch.setattr("backend.app.main.pdf_capability", lambda: pdf)
    db.heartbeat("worker", capabilities=reported)
    health = client.get("/api/health").json()
    assert health["worker"]["online"] is True
    assert health["capabilities"] == {**reported, "pdf": False, "separation": health["separation"]["available"], "resumable_upload": True}
    assert health["pdf_export"] == pdf
    assert health["capabilities_source"] == "worker"


def test_no_fake_fallback_when_engine_unavailable(tmp_path, monkeypatch):
    # Deterministic unsupported environment; never run a model in this unit test.
    monkeypatch.setattr("backend.app.engine.sys.version_info", (3, 12, 0))
    with pytest.raises(AnalysisError, match="supported analysis environment"):
        BasicPitchEngine().transcribe(tmp_path / "missing.wav")


def test_normalization_is_bounded_and_blocks_playlist_demuxers(monkeypatch, settings, tmp_path):
    captured = {}
    monkeypatch.setattr("backend.app.audio.shutil.which", lambda _: "/test/ffmpeg")
    def subprocess_fixture(command, **kwargs):
        captured["command"], captured["options"] = command, kwargs
        (tmp_path / "result.wav").write_bytes(silence_wav())
    monkeypatch.setattr("backend.app.audio.subprocess.run", subprocess_fixture)
    duration = normalize_audio(tmp_path / "source", tmp_path / "result.wav", settings)
    assert duration == pytest.approx(0.2)
    assert captured["options"]["timeout"] == settings.decode_timeout_seconds
    assert "file,pipe" in captured["command"]
    assert "wav,mp3,mov,matroska,webm,ogg,flac" in captured["command"]
    assert str(settings.max_duration_seconds + 1) in captured["command"]


def test_over_duration_audio_is_rejected_not_silently_truncated(monkeypatch, settings, tmp_path):
    monkeypatch.setattr("backend.app.audio.shutil.which", lambda _: "/test/ffmpeg")
    monkeypatch.setattr("backend.app.audio.subprocess.run", lambda *args, **kwargs: (tmp_path / "out.wav").write_bytes(silence_wav(1.1)))
    with pytest.raises(AnalysisError, match="exceeds"):
        normalize_audio(tmp_path / "source", tmp_path / "out.wav", replace(settings, max_duration_seconds=1))
    assert not (tmp_path / "out.wav").exists()

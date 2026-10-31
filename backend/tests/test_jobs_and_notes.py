"""Durable job lifecycle and editable-note regression tests; no fake model inference."""
from __future__ import annotations

import io
import json
import os
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.engine import atomic_json
from backend.app.main import create_app
from backend.app.performance import write_performance_midi
from backend.app.worker import Worker


def audio_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\0\0" * 22050)
    return output.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", max_job_attempts=3)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def upload(client, **metadata):
    response = client.post("/api/projects", data=metadata,
                           files={"file": ("TEST-FIXTURE.wav", audio_fixture(), "audio/wav")})
    assert response.status_code == 201
    return response.json()


def test_queued_and_processing_cancellation_fences_publication(client, settings):
    p = upload(client)
    db = Database(settings.database_path)
    assert client.post(f'/api/projects/{p["id"]}/cancel').json()["status"] == "canceled"
    assert db.claim_job("worker") is None
    assert client.post(f'/api/projects/{p["id"]}/cancel').status_code == 200
    assert client.post(f'/api/projects/{p["id"]}/retry').status_code == 200
    job = db.claim_job("worker")
    assert client.post(f'/api/projects/{p["id"]}/cancel').json()["status"] == "canceled"
    assert not db.stage(job["id"], "worker", "must not publish")
    assert not db.record_decoded(job, "worker", 1, 0, 1)
    assert not db.finish(job, "worker", notes=[], exports=["midi"])
    result = client.get(f'/api/projects/{p["id"]}').json()
    assert result["notes"] is None and result["exports"] == []
    assert all(job["status"] == "canceled" for job in result["jobs"])


def test_retry_budget_and_recovery_budget_are_durable(client, settings):
    p = upload(client)
    db = Database(settings.database_path)
    for attempt in range(3):
        job = db.claim_job(f"worker-{attempt}", lease_seconds=-1)
        assert job is not None
        assert not db.finish(job, f"worker-{attempt}", notes=[])
    assert db.claim_job("last") is None
    with TestClient(create_app(settings)) as restarted:
        result = restarted.get(f'/api/projects/{p["id"]}').json()
        assert result["status"] == "failed" and result["retries_remaining"] == 0
        assert len(result["jobs"]) == 3
        response = restarted.post(f'/api/projects/{p["id"]}/retry')
        assert response.status_code == 409 and "3-attempt" in response.json()["detail"]


def test_competing_retry_requests_create_only_one_attempt(client, settings):
    p = upload(client)
    db = Database(settings.database_path)
    db.cancel(p["id"])
    def retry(_):
        try:
            return db.retry(p["id"])["status"]
        except ValueError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(retry, range(2))) == ["conflict", "queued"]
    assert len(db.get_project(p["id"])["jobs"]) == 2


def test_expired_worker_cannot_renew_or_publish(client, settings):
    p = upload(client)
    db = Database(settings.database_path)
    job = db.claim_job("expired", lease_seconds=-1)
    db.heartbeat("expired", job["id"], 100)
    assert not db.owns_job(job["id"], "expired")
    assert not db.finish(job, "expired", notes=[])
    replacement = db.claim_job("replacement")
    assert replacement["id"] != job["id"]


@pytest.mark.parametrize("field,value", [("minimum_note_seconds", "nan"), ("merge_gap_seconds", "inf"),
                                        ("minimum_note_seconds", -1), ("merge_gap_seconds", 0.3)])
def test_cleanup_settings_are_validated(client, field, value):
    response = client.post("/api/projects", data={field: value},
                           files={"file": ("fixture.wav", audio_fixture(), "audio/wav")})
    assert response.status_code == 422
    assert client.get("/api/projects").json() == []


def test_cleanup_settings_saved_for_retries(client, settings):
    p = upload(client, minimum_note_seconds=0.12, merge_gap_seconds=0)
    assert p["cleanup"] == {"minimum_note_seconds": 0.12, "merge_gap_seconds": 0}
    client.post(f'/api/projects/{p["id"]}/cancel')
    retried = client.post(f'/api/projects/{p["id"]}/retry').json()
    assert retried["cleanup"] == p["cleanup"]


@pytest.fixture
def completed_fixture(client, settings):
    # Explicit editable-storage fixture, not a substituted production inference.
    pytest.importorskip("pretty_midi")
    p = upload(client)
    db = Database(settings.database_path)
    job = db.claim_job("TEST-FIXTURE-worker")
    identifier = str(uuid.uuid4())
    note = {"id": identifier, "pitch": 60, "start": 0.1, "end": 0.8, "velocity": 80,
            "part_id": "part-1", "model_score": 0.62, "provenance": "detected", "source_note_ids": [identifier],
            "original_timing": {"start": 0.1, "end": 0.8, "pitch": 60, "velocity": 80}, "score_timing": None}
    directory = settings.data_dir / "projects" / p["id"] / job["id"]
    directory.mkdir()
    raw = {"TEST_FIXTURE": True, "note_events": [[0.1, 0.8, 60, 0.62]]}
    atomic_json(directory / "raw-note-events.json", raw)
    atomic_json(directory / "notes.json", {"notes": [note]})
    write_performance_midi([note], directory / "raw-performance.mid")
    write_performance_midi([note], directory / "score.mid")
    db.record_decoded(job, "TEST-FIXTURE-worker", 1, 0, 1)
    db.finish(job, "TEST-FIXTURE-worker", notes=[note], duration=1, exports=["raw-midi", "midi", "musicxml"],
              artifacts=["raw-note-events", "raw-performance", "normalized-notes"])
    return db.get_project(p["id"])


def editable(note):
    return {key: note[key] for key in ("id", "pitch", "start", "end", "velocity", "part_id")}


def test_edits_keep_ids_raw_outputs_and_original_timing(client, completed_fixture, settings):
    p = completed_fixture
    raw_url = f'/api/projects/{p["id"]}/artifacts/raw-note-events'
    raw_midi = f'/api/projects/{p["id"]}/exports/raw-midi'
    before, midi_before = client.get(raw_url).content, client.get(raw_midi).content
    changed = {**editable(p["notes"][0]), "pitch": 62, "start": 0.2}
    manual = {"id": None, "pitch": 67, "start": 0.3, "end": 0.9, "velocity": None, "part_id": "part-2"}
    response = client.put(f'/api/projects/{p["id"]}/notes', json={"revision": 1, "notes": [changed, manual]})
    assert response.status_code == 200
    result = response.json()
    detected, added = result["notes"]
    assert detected["id"] == p["notes"][0]["id"] and detected["provenance"] == "edited"
    assert detected["original_timing"] == p["notes"][0]["original_timing"]
    assert added["provenance"] == "manually_added" and added["model_score"] is None
    assert added["original_timing"] is None and added["source_note_ids"] == []
    assert all(note["score_timing"] is None for note in result["notes"])
    assert result["notes_revision"] == 2
    assert "musicxml" not in result["exports"]
    assert client.get(raw_url).content == before and client.get(raw_midi).content == midi_before
    latest = client.get(f'/api/projects/{p["id"]}/artifacts/normalized-notes').json()
    assert latest["notes"] == result["notes"]
    assert client.get(f'/api/projects/{p["id"]}/exports/midi').content != midi_before
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f'/api/projects/{p["id"]}').json()["notes"] == result["notes"]
    conflict = client.put(f'/api/projects/{p["id"]}/notes', json={"revision": 1, "notes": []})
    assert conflict.status_code == 409
    assert client.put(f'/api/projects/{p["id"]}/notes', json={"revision": 2, "notes": []}).json()["notes"] == []
    assert client.get(raw_midi).content == midi_before


@pytest.mark.parametrize("change", [{"id": str(uuid.uuid4())}, {"pitch": 128}, {"start": -1},
                                     {"end": 1.2}, {"part_id": "../escape"}, {"provenance": "detected"}])
def test_invalid_edits_do_not_change_revision(client, completed_fixture, change):
    p = completed_fixture
    note = {**editable(p["notes"][0]), **change}
    response = client.put(f'/api/projects/{p["id"]}/notes', json={"revision": 1, "notes": [note]})
    assert response.status_code == 422
    assert client.get(f'/api/projects/{p["id"]}').json()["notes_revision"] == 1


@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS") != "1", reason="Real process cancellation test")
def test_active_model_process_cancels_without_publishing(settings):
    from backend.tests.test_real_analysis import fixture_audio
    ffmpeg = os.getenv("FFMPEG_BINARY")
    if not ffmpeg:
        pytest.skip("FFMPEG_BINARY required")
    settings = replace(settings, ffmpeg_binary=ffmpeg)
    with TestClient(create_app(settings)) as client:
        p = client.post("/api/projects", files={"file": ("REAL-CANCEL-TEST.wav", fixture_audio())}).json()
        worker = Worker(settings)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(worker.run_once)
            deadline = time.monotonic() + 15
            saw_model_stage = False
            while time.monotonic() < deadline:
                current = client.get(f'/api/projects/{p["id"]}').json()
                if current["jobs"][0]["stage"] == "Loading transcription model":
                    saw_model_stage = True
                    break
                if current["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            assert saw_model_stage, current
            assert client.post(f'/api/projects/{p["id"]}/cancel').json()["status"] == "canceled"
            assert running.result(timeout=5)
        final = client.get(f'/api/projects/{p["id"]}').json()
        assert final["status"] == "canceled" and final["notes"] is None
        assert final["exports"] == [] and final["artifacts"] == []
        assert client.get(f'/api/projects/{p["id"]}/exports/raw-midi').status_code == 404


def test_editing_legacy_result_exposes_only_real_normalized_artifact(client, completed_fixture, settings):
    p = completed_fixture
    db = Database(settings.database_path)
    with db.connect() as connection:
        connection.execute("UPDATE projects SET artifacts_json='[]',exports_json='[\"midi\"]' WHERE id=?", (p["id"],))
    response = client.put(f'/api/projects/{p["id"]}/notes', json={"revision": 1, "notes": [editable(p["notes"][0])]})
    assert response.status_code == 200
    assert response.json()["artifacts"] == ["normalized-notes"]
    assert client.get(f'/api/projects/{p["id"]}/artifacts/normalized-notes').status_code == 200
    assert client.get(f'/api/projects/{p["id"]}/artifacts/raw-model-output').status_code == 404

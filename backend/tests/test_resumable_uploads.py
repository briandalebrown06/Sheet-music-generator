"""Real bytes, durable transfer state and private ownership; isolated fixtures."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.uploads import cleanup_uploads, _lease


def wav_bytes(seconds=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(22050)
        audio.writeframes(b"\x00\x20" * int(seconds * 22050))
    return output.getvalue()


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", upload_chunk_bytes=65536)
    app = create_app(settings)
    with TestClient(app) as client:
        yield settings, app, client


def begin(client, data, *, key="resumable-fixture-1", headers=None, **metadata):
    return client.post("/api/uploads", headers={"Idempotency-Key": key, **(headers or {})}, json={
        "filename": "DEVELOPMENT-fixture.wav", "size_bytes": len(data),
        "title": "EXPLICIT DEVELOPMENT FIXTURE", "clip_end_seconds": 2,
        **metadata})


def chunk(client, identifier, offset, data, headers=None, digest=None):
    return client.put(f"/api/uploads/{identifier}/chunks/{offset}", content=data, headers={
        "Content-Type": "application/octet-stream", "X-Chunk-SHA256": digest or hashlib.sha256(data).hexdigest(), **(headers or {})})


def transfer(client, state, data, headers=None):
    for start in range(state["offset"], len(data), state["chunk_size"]):
        response = chunk(client, state["id"], start, data[start:start + state["chunk_size"]], headers)
        assert response.status_code == 200, response.text
    return response.json()


def test_restart_resume_duplicate_chunk_and_atomic_idempotent_completion(setup):
    settings, app, client = setup
    data = wav_bytes(); response = begin(client, data)
    assert response.status_code == 201
    state = response.json(); identifier = state["id"]
    assert begin(client, data).json()["id"] == identifier
    assert begin(client, data, title="Different settings").status_code == 409
    first = data[:state["chunk_size"]]
    accepted = chunk(client, identifier, 0, first)
    assert accepted.status_code == 200 and accepted.json()["offset"] == len(first)
    assert chunk(client, identifier, 0, first).json()["offset"] == len(first), "lost response replay is not appended twice"
    assert chunk(client, identifier, 0, first[:-1] + b"x").status_code == 409
    assert client.post(f"/api/uploads/{identifier}/complete").status_code == 409
    # Interrupted writes beyond the DB offset are never treated as acknowledged.
    source = settings.data_dir / "uploads" / identifier / "source.part"
    with source.open("ab") as abandoned:
        abandoned.write(b"UNCOMMITTED REQUEST BYTES")
    restarted = create_app(settings)
    with TestClient(restarted) as resumed:
        state = resumed.get(f"/api/uploads/{identifier}").json()
        assert state["offset"] == len(first)
        state = transfer(resumed, state, data)
        assert state["offset"] == len(data) and len(state["chunks"]) == 2
        complete = resumed.post(f"/api/uploads/{identifier}/complete")
        assert complete.status_code == 201, complete.text
        project = complete.json()
        assert resumed.post(f"/api/uploads/{identifier}/complete").json()["id"] == project["id"]
        assert resumed.post(f"/api/uploads/{identifier}/complete", json={"title": "Changed"}).status_code == 409
        state = resumed.get(f"/api/uploads/{identifier}").json()
        assert state["status"] == "completed" and state["sha256"] == hashlib.sha256(data).hexdigest()
        assert state["preview_url"] is None
        assert resumed.delete(f"/api/uploads/{identifier}").status_code == 409
    original = settings.data_dir / "projects" / project["id"] / "source"
    assert original.read_bytes() == data
    manifest = json.loads(original.with_name("upload-manifest.json").read_text())
    assert manifest["size_bytes"] == len(data) and manifest["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(app.state.db.list_projects()) == 1 and len(project["jobs"]) == 1
    assert not source.parent.exists(), "temporary preview and transfer directory are removed after publication"


def test_chunk_integrity_offsets_size_and_container_validation(setup):
    settings, app, client = setup; data = wav_bytes()
    state = begin(client, data).json(); identifier = state["id"]
    first = data[:state["chunk_size"]]
    assert chunk(client, identifier, 9, first).status_code == 409
    assert chunk(client, identifier, 0, first, digest="a" * 64).status_code == 422
    assert client.get(f"/api/uploads/{identifier}").json()["offset"] == 0
    assert chunk(client, identifier, 0, data).status_code == 413
    assert chunk(client, identifier, 0, data[:100]).status_code == 422
    assert chunk(client, identifier, 0, b"corrupt bytes").status_code == 415
    assert chunk(client, identifier, 0, b"").status_code == 422
    assert (settings.data_dir / "uploads" / identifier / "source.part").stat().st_size == 0
    assert client.put(f"/api/uploads/{identifier}/chunks/0", content=first, headers={"X-Chunk-SHA256": hashlib.sha256(first).hexdigest()}).status_code == 415
    assert chunk(client, identifier, 0, first, digest="invalid").status_code == 422
    assert chunk(client, identifier, 0, first).status_code == 200
    # Repeating a correct digest with altered bytes is not a valid retry.
    assert chunk(client, identifier, 0, b"x" * len(first), digest=hashlib.sha256(first).hexdigest()).status_code == 422


def test_finalize_rechecks_saved_bytes_before_creating_any_project(setup):
    settings, app, client = setup; data = wav_bytes()
    state = transfer(client, begin(client, data).json(), data)
    source = settings.data_dir / "uploads" / state["id"] / "source.part"
    with source.open("r+b") as changed:
        changed.seek(100); changed.write(b"FILE TAMPERING")
    result = client.post(f'/api/uploads/{state["id"]}/complete')
    assert result.status_code == 422 and "integrity" in result.json()["detail"]
    assert app.state.db.list_projects() == []


def test_crash_after_source_move_recovers_without_resending_bytes(setup):
    settings, app, client = setup; data = wav_bytes()
    state = transfer(client, begin(client, data).json(), data)
    with app.state.db.connect() as connection:
        project_id = connection.execute("SELECT project_id FROM upload_sessions WHERE id=?", (state["id"],)).fetchone()[0]
    directory = settings.data_dir / "projects" / project_id
    directory.mkdir()
    os.replace(settings.data_dir / "uploads" / state["id"] / "source.part", directory / "source")
    result = client.post(f'/api/uploads/{state["id"]}/complete')
    assert result.status_code == 201, result.text
    assert result.json()["id"] == project_id and (directory / "source").read_bytes() == data


def test_discard_expiry_and_cleanup_do_not_delete_published_projects(setup):
    settings, app, client = setup; data = wav_bytes()
    state = begin(client, data).json(); identifier = state["id"]
    chunk(client, identifier, 0, data[:65536])
    assert client.delete(f"/api/uploads/{identifier}").status_code == 204
    assert client.get(f"/api/uploads/{identifier}").status_code == 410
    assert begin(client, data).status_code == 410
    assert not (settings.data_dir / "uploads" / identifier).exists()
    state = begin(client, data, key="expiry-fixture").json(); identifier = state["id"]
    chunk(client, identifier, 0, data[:65536])
    with app.state.db.connect() as connection:
        connection.execute("UPDATE upload_sessions SET expires_at=? WHERE id=?", (time.time() - 1, identifier))
    with _lease(settings, identifier):
        cleanup_uploads(settings, app.state.db)
        assert (settings.data_dir / "uploads" / identifier).exists(), "active transfer lease fences cleanup"
    cleanup_uploads(settings, app.state.db)
    assert client.get(f"/api/uploads/{identifier}").status_code == 410
    assert not (settings.data_dir / "uploads" / identifier).exists()
    state = transfer(client, begin(client, data, key="completed-expiry").json(), data)
    project = client.post(f'/api/uploads/{state["id"]}/complete').json()
    with app.state.db.connect() as connection:
        connection.execute("UPDATE upload_sessions SET expires_at=? WHERE id=?", (time.time() - 1, state["id"]))
    cleanup_uploads(settings, app.state.db)
    assert (settings.data_dir / "projects" / project["id"] / "source").read_bytes() == data
    assert app.state.db.get_project(project["id"]) is not None


def test_disconnect_before_chunk_commit_retains_durable_offset(setup):
    settings, app, client = setup; data = wav_bytes()
    state = begin(client, data).json(); identifier = state["id"]
    first = data[:65536]; chunk(client, identifier, 0, first)
    messages = iter([{"type": "http.request", "body": data[65536:70000], "more_body": True}, {"type": "http.disconnect"}])
    async def receive(): return next(messages)
    async def send(message): pass
    scope = {"type": "http", "http_version": "1.1", "scheme": "http", "method": "PUT", "path": f"/api/uploads/{identifier}/chunks/65536", "query_string": b"", "root_path": "", "server": ("testserver", 80), "client": ("testclient", 5000), "headers": [(b"host", b"testserver"), (b"content-type", b"application/octet-stream"), (b"x-chunk-sha256", hashlib.sha256(data[65536:]).hexdigest().encode())]}
    asyncio.run(app(scope, receive, send))
    assert client.get(f"/api/uploads/{identifier}").json()["offset"] == 65536
    assert (settings.data_dir / "uploads" / identifier / "source.part").read_bytes() == first


def test_size_boundary_is_independent_of_duration_selection(setup):
    settings, app, client = setup
    for size, status in ((500_000_000, 201), (500_000_001, 413)):
        result = client.post("/api/uploads", headers={"Idempotency-Key": f"size-boundary-{size}"}, json={
            "filename": "DEVELOPMENT-large.wav", "size_bytes": size, "clip_start_seconds": 0, "clip_end_seconds": 0.1})
        assert result.status_code == status, result.text
    assert begin(client, b"x", key="duration-boundary", clip_end_seconds=3600).status_code == 201
    assert begin(client, b"x", key="duration-too-long", clip_end_seconds=3600.1).status_code == 422
    assert begin(client, b"x", key="unsupported-file", filename="no.txt").status_code == 415


def test_owner_auth_csrf_quotas_and_chunk_rate_budget(tmp_path):
    origin = "https://score.example.test"
    settings = Settings(data_dir=tmp_path, app_mode="hosted", public_origin=origin,
                        max_duration_seconds=2, max_storage_mb_per_user=64, write_rate_limit_per_minute=1,
                        upload_chunk_bytes=65536)
    app = create_app(settings)
    password = "EXPLICIT DEVELOPMENT PASSWORD 7654"
    app.state.auth_store.create_user("alice", password)
    app.state.auth_store.create_user("bob", password)
    with TestClient(app, base_url=origin) as alice, TestClient(app, base_url=origin) as bob, TestClient(app, base_url=origin) as anon:
        def login(client, name):
            result = client.post("/api/auth/login", headers={"Origin": origin}, json={"username": name, "password": password}).json()
            return {"Origin": origin, "X-CSRF-Token": result["csrf_token"]}
        ah, bh = login(alice, "alice"), login(bob, "bob")
        data = wav_bytes(); state = begin(alice, data, headers=ah).json(); identifier = state["id"]
        assert anon.get(f"/api/uploads/{identifier}").status_code == 401
        assert bob.get(f"/api/uploads/{identifier}").status_code == 404
        assert bob.get(f"/api/uploads/{identifier}/preview").status_code == 404
        assert chunk(bob, identifier, 0, data[:65536], bh).status_code == 404
        assert chunk(alice, identifier, 0, data[:65536]).status_code == 403
        # Metadata creation exhausted the one-write mutation budget. Multiple
        # byte chunks still use their separate, bounded transfer budget.
        assert chunk(alice, identifier, 0, data[:65536], ah).status_code == 200
        assert chunk(alice, identifier, 65536, data[65536:], ah).status_code == 200
        assert alice.post(f"/api/uploads/{identifier}/complete", headers=ah).status_code == 429
        with app.state.auth_store.connection() as connection:
            connection.execute("DELETE FROM auth_rate_buckets")
        complete = alice.post(f"/api/uploads/{identifier}/complete", headers=ah)
        assert complete.status_code == 201, complete.text
        assert complete.json()["owner_id"] == alice.get("/api/auth/session").json()["user"]["id"]
        assert bob.post(f"/api/uploads/{identifier}/complete", headers=bh).status_code in {404, 429}


def test_unfinished_session_storage_and_count_reservations(tmp_path):
    settings = Settings(data_dir=tmp_path, auth_enabled=True, max_duration_seconds=1,
                        max_storage_mb_per_user=16, max_active_uploads_per_user=1)
    app = create_app(settings)
    app.state.auth_store.create_user("alice", "EXPLICIT DEVELOPMENT PASSWORD 7654")
    with TestClient(app) as client:
        login = client.post("/api/auth/login", headers={"Origin": "http://localhost:5173"}, json={"username": "alice", "password": "EXPLICIT DEVELOPMENT PASSWORD 7654"}).json()
        headers = {"Origin": "http://localhost:5173", "X-CSRF-Token": login["csrf_token"]}
        response = client.post("/api/uploads", headers={**headers, "Idempotency-Key": "oversized-reservation"}, json={"filename": "fixture.wav", "size_bytes": 16 * 1024 * 1024})
        assert response.status_code == 413, "A normalized preview also reserves private storage"
        first = begin(client, wav_bytes(1), headers=headers, clip_end_seconds=1)
        assert first.status_code == 201
        assert begin(client, wav_bytes(1), key="second-pending", headers=headers, clip_end_seconds=1).status_code == 429
        assert client.delete('/api/uploads/' + first.json()["id"], headers=headers).status_code == 204
        assert begin(client, wav_bytes(1), key="after-discard", headers=headers, clip_end_seconds=1).status_code == 201


def test_streamed_preview_reuses_upload_and_accepts_final_clip_settings(setup):
    settings, app, client = setup
    binary = Path(__file__).resolve().parents[2] / ".runtime" / "bin" / "ffmpeg"
    if not binary.exists(): pytest.skip("Installed FFmpeg unavailable")
    settings = replace(settings, ffmpeg_binary=str(binary))
    app = create_app(settings)
    with TestClient(app) as client:
        data = wav_bytes(); state = begin(client, data).json()
        assert client.get(f'/api/uploads/{state["id"]}/preview').status_code == 409
        state = transfer(client, state, data)
        # An interrupted decoder's uncommitted cache is rebuilt; its bytes must
        # never be served as a complete preview after API restart.
        (settings.data_dir / "uploads" / state["id"] / "preview.pending.wav").write_bytes(b"TRUNCATED PREVIEW")
        preview = client.get(state["preview_url"])
        assert preview.status_code == 200 and preview.content.startswith(b"RIFF")
        assert client.get(state["preview_url"], headers={"Range": "bytes=0-63"}).status_code == 206
        with wave.open(io.BytesIO(preview.content), "rb") as audio:
            assert audio.getnframes() / audio.getframerate() == 2
        completed = client.post(f'/api/uploads/{state["id"]}/complete', json={"title": "FINAL CLIP FIXTURE", "clip_start_seconds": 0.5, "clip_end_seconds": 1.5})
        assert completed.status_code == 201, completed.text
        project = completed.json()
        assert project["clip_start_seconds"] == 0.5 and project["clip_end_seconds"] == 1.5
        assert (settings.data_dir / "projects" / project["id"] / "source").read_bytes() == data
        assert not (settings.data_dir / "uploads" / state["id"]).exists()

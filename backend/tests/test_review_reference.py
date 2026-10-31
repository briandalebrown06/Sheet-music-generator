"""Generated audio and disposable lineage fixtures verify private A/B playback."""
from __future__ import annotations

import io
import math
import struct
import uuid
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app


def fixture_audio():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        # Distinct per-second levels make an incorrect clip offset observable.
        audio.writeframes(b"".join(struct.pack("<h", round((1000 + 1000 * (i // 22050)) * math.sin(2 * math.pi * 440 * i / 22050)))
                                   for i in range(10 * 22050)))
    return data.getvalue()


@pytest.fixture
def family(tmp_path):
    settings = Settings(data_dir=tmp_path, ffmpeg_binary=str(Path(".runtime/bin/ffmpeg").resolve()))
    app = create_app(settings)
    db = app.state.db
    ids = [str(uuid.uuid4()) for _ in range(4)]
    audio = fixture_audio()
    for index, identifier in enumerate(ids):
        db.create_project(identifier, f"DEVELOPMENT FIXTURE reference {index}", "upload", "fixture.wav", "audio/wav")
        directory = tmp_path / "projects" / identifier
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(audio)
    with db.connect() as connection:
        connection.execute("UPDATE projects SET input_mode='mixed',duration_seconds=10,clip_start_seconds=2,clip_end_seconds=8,clip_duration_seconds=6 WHERE id=?", (ids[0],))
        connection.execute("UPDATE projects SET parent_project_id=?,stem_category='other',duration_seconds=6,source_origin_seconds=2,clip_start_seconds=1,clip_end_seconds=4,clip_duration_seconds=3 WHERE id=?", (ids[0], ids[1]))
        for index in (2, 3):
            connection.execute("UPDATE projects SET arrangement_source_id=?,duration_seconds=6,source_origin_seconds=2,clip_start_seconds=1,clip_end_seconds=4,clip_duration_seconds=3 WHERE id=?", (ids[index-1], ids[index]))
    with TestClient(app) as client:
        yield client, db, ids, tmp_path, audio


def reference_url(identifier, suffix=""):
    return f"/api/projects/{identifier}/playback/reference{suffix}"


def test_nested_reference_authorized_range_audio_and_exact_offset_waveform(family):
    client, db, ids, directory, audio = family
    metadata = client.get(reference_url(ids[-1])).json()
    assert metadata["available"] and metadata["title"] == "DEVELOPMENT FIXTURE reference 0"
    assert metadata["clip_start_seconds"] == 3 and metadata["clip_duration_seconds"] == 3
    assert metadata["audio_url"] == reference_url(ids[-1], "/audio")
    assert metadata["waveform_url"] == reference_url(ids[-1], "/waveform")
    assert client.get(metadata["audio_url"]).content == audio
    response = client.get(metadata["audio_url"], headers={"Range": "bytes=0-43"})
    assert response.status_code == 206 and response.content == audio[:44]
    assert response.headers["cache-control"] == "no-store"
    waveform = client.get(metadata["waveform_url"])
    assert waveform.status_code == 200, waveform.text
    result = waveform.json()
    assert result["duration"] == 3
    assert min(result["peaks"][:500]) == pytest.approx(4000 / 32768, abs=.002)
    assert max(result["peaks"][-500:]) == pytest.approx(6000 / 32768, abs=.002)
    assert client.get(metadata["waveform_url"]).json() == result
    assert list((directory / "projects" / ids[-1]).glob("reference-waveform-*.json"))
    # Cache must never bypass authorization or deletion on any chain node.
    with db.connect() as connection:
        connection.execute("UPDATE projects SET deleted_at='fixture-deleted' WHERE id=?", (ids[1],))
    assert client.get(metadata["waveform_url"]).status_code == 404
    assert client.get(metadata["audio_url"]).status_code == 404


@pytest.mark.parametrize("broken", ["cross_owner", "missing", "cycle", "deleted", "invalid_id", "missing_file", "escaping_symlink"])
def test_broken_or_private_ancestor_never_leaks_title_or_audio(family, broken):
    client, db, ids, directory, _ = family
    with db.connect() as connection:
        if broken == "cross_owner":
            connection.execute("UPDATE projects SET owner_id='another-user' WHERE id=?", (ids[1],))
        elif broken == "deleted":
            connection.execute("UPDATE projects SET deleted_at='fixture-deleted' WHERE id=?", (ids[1],))
        elif broken == "missing":
            connection.execute("UPDATE projects SET arrangement_source_id=? WHERE id=?", (str(uuid.uuid4()), ids[2]))
        elif broken == "cycle":
            connection.execute("UPDATE projects SET arrangement_source_id=? WHERE id=?", (ids[3], ids[2]))
        elif broken == "invalid_id":
            connection.execute("UPDATE projects SET arrangement_source_id='../private' WHERE id=?", (ids[2],))
    if broken == "missing_file":
        (directory / "projects" / ids[0] / "source").unlink()
    elif broken == "escaping_symlink":
        outside = directory / "private-unrelated-fixture.wav"
        outside.write_bytes(b"private test fixture")
        source = directory / "projects" / ids[0] / "source"
        source.unlink()
        source.symlink_to(outside)
    metadata = client.get(reference_url(ids[-1]))
    assert metadata.status_code == 200 and metadata.json()["available"] is False
    assert "title" not in metadata.json() and "audio_url" not in metadata.json()
    for suffix in ("/audio", "/waveform"):
        assert client.get(reference_url(ids[-1], suffix)).status_code == 404


def test_reference_current_project_still_requires_ownership(family):
    client, db, ids, _, _ = family
    with db.connect() as connection:
        connection.execute("UPDATE projects SET owner_id='another-user' WHERE id=?", (ids[-1],))
    for suffix in ("", "/audio", "/waveform"):
        assert client.get(reference_url(ids[-1], suffix)).status_code == 404


def test_original_project_has_no_duplicate_reference_option(family):
    client, _, ids, _, _ = family
    assert client.get(reference_url(ids[0])).json()["available"] is False
    assert client.get(reference_url(ids[0], "/audio")).status_code == 404


@pytest.mark.parametrize("start,duration", [(-1, 3), (9, 3), (3, 0)])
def test_invalid_reference_alignment_is_rejected(family, start, duration):
    client, db, ids, _, _ = family
    with db.connect() as connection:
        connection.execute("UPDATE projects SET source_origin_seconds=0,clip_start_seconds=?,clip_duration_seconds=? WHERE id=?", (start, duration, ids[-1]))
    assert client.get(reference_url(ids[-1])).json()["available"] is False
    assert client.get(reference_url(ids[-1], "/audio")).status_code == 404


def test_reference_chain_depth_is_bounded(family):
    client, db, ids, _, _ = family
    previous = ids[-1]
    for _ in range(16):
        identifier = str(uuid.uuid4())
        db.create_project(identifier, "DEVELOPMENT FIXTURE chain", "upload", "fixture.wav", "audio/wav")
        with db.connect() as connection:
            connection.execute("UPDATE projects SET arrangement_source_id=? WHERE id=?", (previous, identifier))
        previous = identifier
    assert client.get(reference_url(previous)).json()["available"] is False
    assert client.get(reference_url(previous, "/audio")).status_code == 404


def test_hosted_reference_endpoints_require_authentication(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, app_mode="hosted", public_origin="https://score.example.test"))
    with TestClient(app, base_url="https://score.example.test") as client:
        for suffix in ("", "/audio", "/waveform"):
            response = client.get(reference_url(str(uuid.uuid4()), suffix))
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"

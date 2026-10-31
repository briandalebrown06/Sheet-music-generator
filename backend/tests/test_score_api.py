"""Explicit stored-note fixtures test notation, not model accuracy."""
from __future__ import annotations

import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("music21")

from backend.app.config import Settings
from backend.app.main import create_app


@pytest.fixture
def stored_project(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    db = app.state.db
    project_id, note_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.create_project(project_id, "DEVELOPMENT FIXTURE — canonical notation API", "upload", "fixture.wav", "audio/wav",
                      tempo_hint=120, time_signature_hint="4/4")
    job = db.claim_job("test-notation-worker", 60)
    event = {"id": note_id, "start": 0.12, "end": 2.31, "pitch": 60, "velocity": 80,
             "part_id": "part-1", "model_score": 0.4, "provenance": "detected", "source_note_ids": [note_id],
             "original_timing": {"start": 0.12, "end": 2.31, "pitch": 60, "velocity": 80}, "score_timing": None}
    db.record_decoded(job, "test-notation-worker", 3, 0, 3)
    db.finish(job, "test-notation-worker", notes=[event], duration=3, exports=["midi", "raw-midi"])
    directory = tmp_path / "projects" / project_id / job["id"]
    directory.mkdir(parents=True)
    # Clearly labeled sentinel bytes verify storage isolation; they are not
    # represented as a working MIDI file or model result in any production path.
    (directory / "raw-performance.mid").write_bytes(b"TEST FIXTURE: immutable raw artifact")
    (directory / "raw-note-events.json").write_text(json.dumps([event]))
    with TestClient(app) as client:
        yield client, db, project_id, directory


def settings_for(client, project_id):
    response = client.get(f"/api/projects/{project_id}/score")
    assert response.status_code == 200
    return response.json()["defaults"]


def test_preview_save_reload_and_exports_share_canonical_score(stored_project, tmp_path):
    from music21 import converter
    client, db, project_id, directory = stored_project
    url = f"/api/projects/{project_id}"
    before = db.get_project(project_id)
    raw = {path.name: path.read_bytes() for path in directory.iterdir()}
    initial = client.get(url + "/score").json()
    assert initial["score"] is None and initial["revision"] == 0
    settings = settings_for(client, project_id)
    settings.update(tempo_bpm=120, quantization="eighth", transpose_semitones=2)
    body = {"notes_revision": 1, "settings": settings}
    preview = client.post(url + "/score/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["score"]["beat_unit"] == "quarter"
    assert "score-partwise" in result["musicxml"]
    assert db.get_score(project_id) is None
    assert db.get_project(project_id) == before
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == raw

    saved = client.put(url + "/score", json={**body, "revision": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert saved.json()["score"] == result["score"]
    assert client.get(url + "/score").json()["score"] == result["score"]
    assert client.get(url + "/score/musicxml").text == saved.json()["musicxml"]
    assert client.get(url + "/exports/musicxml").text == saved.json()["musicxml"]
    assert client.get(url + "/score/exports/json").json() == result["score"]
    assert db.get_project(project_id)["notes"] == before["notes"]
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == raw
    for format, suffix in (("musicxml", "musicxml"), ("midi", "mid")):
        download = client.get(url + f"/score/exports/{format}")
        assert download.status_code == 200, download.text
        path = tmp_path / f"roundtrip.{suffix}"
        path.write_bytes(download.content)
        score = converter.parse(path)
        assert {pitch.midi for n in score.recurse().notes for pitch in n.pitches} == {62}
    assert not list(tmp_path.glob("score-midi-*"))


def test_revision_conflicts_and_stale_scores(stored_project):
    client, db, project_id, _ = stored_project
    url = f"/api/projects/{project_id}"
    body = {"notes_revision": 1, "revision": 0, "settings": settings_for(client, project_id)}
    assert client.put(url + "/score", json=body).status_code == 200
    assert client.put(url + "/score", json=body).status_code == 409
    previous = copy.deepcopy(db.get_score(project_id))
    notes = copy.deepcopy(db.get_project(project_id)["notes"])
    notes[0]["pitch"] = 65
    assert db.save_notes(project_id, 1, notes, str(uuid.uuid4()))
    assert client.get(url + "/score").json()["stale"] is True
    assert db.get_score(project_id) == previous
    assert "musicxml" not in db.get_project(project_id)["exports"]
    for endpoint in ("/score/musicxml", "/score/exports/musicxml", "/score/exports/midi", "/score/exports/json", "/exports/musicxml"):
        assert client.get(url + endpoint).status_code == 409
    assert client.post(url + "/score/preview", json={key: value for key, value in body.items() if key != "revision"}).status_code == 409
    assert client.put(url + "/score", json={**body, "revision": 1}).status_code == 409
    fresh = client.put(url + "/score", json={**body, "revision": 1, "notes_revision": 2})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["revision"] == 2 and fresh.json()["stale"] is False


def test_compare_and_swap_blocks_note_edit_during_engraving(stored_project, monkeypatch):
    import backend.app.score_api as api
    client, db, project_id, _ = stored_project
    original = api.score_to_musicxml
    def concurrent_edit(model):
        xml = original(model)
        db.save_notes(project_id, 1, db.get_project(project_id)["notes"], str(uuid.uuid4()))
        return xml
    monkeypatch.setattr(api, "score_to_musicxml", concurrent_edit)
    response = client.put(f"/api/projects/{project_id}/score", json={"notes_revision": 1, "revision": 0,
                          "settings": settings_for(client, project_id)})
    assert response.status_code == 409
    assert db.get_score(project_id) is None


@pytest.mark.parametrize("values", [
    {"tempo_bpm": 0}, {"tempo_bpm": 1000}, {"key_signature": 8}, {"quantization": "random"},
    {"pickup_beats": "1/0"}, {"pickup_beats": "5"}, {"first_downbeat_seconds": -1},
    {"first_downbeat_seconds": 100}, {"transpose_semitones": 25}, {"clef": "invalid"},
    {"staff_overrides": {"not-a-note-id": "bass"}},
])
def test_invalid_notation_settings_do_not_mutate_projects(stored_project, values):
    client, db, project_id, _ = stored_project
    before = db.get_project(project_id)
    settings = {**settings_for(client, project_id), **values}
    response = client.post(f"/api/projects/{project_id}/score/preview", json={"notes_revision": 1, "settings": settings})
    assert response.status_code == 422, response.text
    assert db.get_project(project_id) == before
    assert db.get_score(project_id) is None


def test_no_legacy_midi_derived_musicxml_is_served(stored_project):
    client, db, project_id, directory = stored_project
    (directory / "score.musicxml").write_text("TEST FIXTURE legacy noncanonical XML")
    with db.connect() as connection:
        connection.execute("UPDATE projects SET exports_json=? WHERE id=?", (json.dumps(["musicxml"]), project_id))
    assert "musicxml" not in db.get_project(project_id)["exports"]
    assert client.get(f"/api/projects/{project_id}/exports/musicxml").status_code == 404

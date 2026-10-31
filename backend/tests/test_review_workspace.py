"""Explicit note/audio fixtures verify review persistence, not model accuracy."""
import copy
import io
import json
import uuid
import wave
from fractions import Fraction

import pytest

from backend.tests.test_score_api import stored_project, settings_for
from backend.app.score import build_score, fraction, score_to_musicxml, write_score_midi


def editable(note, **updates):
    return {**{key: note.get(key) for key in ("id", "pitch", "start", "end", "velocity", "part_id", "voice_id")}, **updates}


def test_atomic_review_preview_save_reopen_and_current_exports(stored_project, tmp_path):
    import pretty_midi
    from music21 import converter
    client, db, project_id, directory = stored_project
    base = f"/api/projects/{project_id}"
    original = db.get_project(project_id)
    note = original["notes"][0]
    raw = {path.name: path.read_bytes() for path in directory.iterdir()}
    body = {"notes_revision": 1, "score_revision": 0, "settings": settings_for(client, project_id),
            "notes": [editable(note, pitch=67, start=0.5, end=1.5, voice_id="3", part_id="melody")]}
    preview = client.post(base + "/review/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert db.get_project(project_id) == original
    assert db.get_score(project_id) is None
    saved = client.put(base + "/review", json=body)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["project"]["notes_revision"] == 2
    assert result["score_state"]["revision"] == 1
    assert result["score_state"]["source_notes_revision"] == 2
    assert result["project"]["notes"][0]["id"] == note["id"]
    assert result["project"]["notes"][0]["original_timing"] == note["original_timing"]
    assert result["project"]["notes"][0]["provenance"] == "edited"
    # Reopen through a separately constructed API sharing persistent SQLite.
    from fastapi.testclient import TestClient
    from backend.app.config import Settings
    from backend.app.main import create_app
    with TestClient(create_app(Settings(data_dir=tmp_path))) as reopened:
        assert reopened.get(base).json()["notes"] == result["project"]["notes"]
        assert reopened.get(base + "/score").json()["score"] == result["score_state"]["score"]
    xml = client.get(base + "/score/exports/musicxml")
    parsed = converter.parseData(xml.text)
    assert {p.midi for n in parsed.recurse().notes for p in n.pitches} == {67}
    for endpoint in ("/score/exports/midi", "/exports/midi"):
        response = client.get(base + endpoint)
        assert response.status_code == 200
        midi = pretty_midi.PrettyMIDI(io.BytesIO(response.content))
        notes = [n for part in midi.instruments for n in part.notes]
        assert len(notes) == 1 and notes[0].pitch == 67
        assert notes[0].start == pytest.approx(0.5, abs=.001)
        assert notes[0].end == pytest.approx(1.5, abs=.001)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == raw
    assert client.put(base + "/review", json=body).status_code == 409


def test_stable_identity_restore_after_delete_and_split_lineage(stored_project):
    client, db, project_id, _ = stored_project
    base = f"/api/projects/{project_id}"
    original = db.get_project(project_id)["notes"][0]
    child_id = str(uuid.uuid4())
    settings = settings_for(client, project_id)
    notes = [editable(original, end=1), editable(original, id=child_id, start=1, end=2,
             new_note=True, derived_from_ids=[original["id"]])]
    saved = client.put(base + "/review", json={"notes_revision":1,"score_revision":0,"notes":notes,"settings":settings})
    assert saved.status_code == 200, saved.text
    child = next(n for n in saved.json()["project"]["notes"] if n["id"] == child_id)
    assert child["provenance"] == "edited" and child["source_note_ids"] == original["source_note_ids"]
    assert child["original_timing"] == original["original_timing"]
    deleted = client.put(base + "/review", json={"notes_revision":2,"score_revision":1,"notes":[],"settings":settings})
    assert deleted.status_code == 200
    restored = client.put(base + "/review", json={"notes_revision":3,"score_revision":2,
                         "notes":[editable(original),editable(child)],"settings":settings})
    assert restored.status_code == 200, restored.text
    assert {n["id"] for n in restored.json()["project"]["notes"]} == {original["id"], child_id}
    assert all(n["provenance"] != "manually_added" for n in restored.json()["project"]["notes"])


def test_manual_note_ids_stay_stable_and_metadata_is_not_fabricated(stored_project):
    client, db, project_id, _ = stored_project
    identifier = str(uuid.uuid4())
    body = {"notes_revision":1,"score_revision":0,"settings":settings_for(client,project_id),
            "notes":[{"id":identifier,"new_note":True,"pitch":60,"start":0,"end":1,"part_id":"new-part"}]}
    saved = client.put(f"/api/projects/{project_id}/review",json=body)
    assert saved.status_code == 200, saved.text
    note = saved.json()["project"]["notes"][0]
    assert note["id"] == identifier and note["provenance"] == "manually_added"
    assert note["model_score"] is None and note["original_timing"] is None and note["source_note_ids"] == []


def test_review_save_fences_concurrent_score_revision(stored_project, monkeypatch):
    from backend.app import review_api
    client, db, project_id, _ = stored_project
    original = db.get_project(project_id)
    settings = settings_for(client,project_id)
    previous = review_api.score_to_musicxml
    def race(score):
        xml = previous(score)
        db.save_score(project_id,1,0,score,xml)
        return xml
    monkeypatch.setattr(review_api,"score_to_musicxml",race)
    response = client.put(f"/api/projects/{project_id}/review",json={"notes_revision":1,"score_revision":0,
                          "notes":[editable(original["notes"][0],pitch=68)],"settings":settings})
    assert response.status_code == 409
    assert db.get_project(project_id)["notes"] == original["notes"]


def test_review_response_keeps_committed_snapshot_if_another_save_follows(stored_project, monkeypatch):
    client, db, project_id, _ = stored_project
    original = db.get_project(project_id)
    settings = settings_for(client, project_id)
    save_notes = db.save_notes

    def concurrent_followup(*args, **kwargs):
        accepted = save_notes(*args, **kwargs)
        if accepted:
            # A separate tab commits after this request's transaction but before
            # its response is constructed. The first response must stay coherent.
            newer = copy.deepcopy(db.get_project(project_id)["notes"])
            newer[0]["pitch"] = 72
            score = build_score(newer, settings, title="TEST FIXTURE — competing review", source_notes_revision=3)
            assert save_notes(project_id, 2, newer, str(uuid.uuid4()),
                              score_bundle={"score": score, "musicxml": score_to_musicxml(score)},
                              score_revision=1)
        return accepted

    monkeypatch.setattr(db, "save_notes", concurrent_followup)
    response = client.put(f"/api/projects/{project_id}/review", json={
        "notes_revision": 1, "score_revision": 0, "settings": settings,
        "notes": [editable(original["notes"][0], pitch=68)],
    })
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["project"]["notes_revision"] == accepted["score_state"]["source_notes_revision"] == 2
    assert accepted["project"]["notes"][0]["pitch"] == 68
    assert accepted["score_state"]["score"]["source_notes"][0]["pitch"] == 68
    assert db.get_project(project_id)["notes_revision"] == 3
    assert db.get_project(project_id)["notes"][0]["pitch"] == 72
    assert client.get(f"/api/projects/{project_id}/score/exports/musicxml?revision=1").status_code == 409


def test_explicit_voice_conflicts_do_not_partially_save(stored_project):
    client, db, project_id, _ = stored_project
    original = db.get_project(project_id)
    note = original["notes"][0]
    body = {"notes_revision":1,"score_revision":0,"settings":settings_for(client,project_id),
            "notes":[editable(note,voice_id="2"),editable(note,id=str(uuid.uuid4()),new_note=True,
                     pitch=64,start=.5,end=1,voice_id="2")]}
    response = client.put(f"/api/projects/{project_id}/review",json=body)
    assert response.status_code == 422 and "overlapping" in response.text
    assert db.get_project(project_id) == original
    assert db.get_score(project_id) is None


def test_part_and_voice_assignments_survive_musicxml_and_midi(tmp_path):
    from music21 import converter
    import pretty_midi
    notes=[{"id":"a","pitch":60,"start":0,"end":1,"part_id":"melody","voice_id":"3"},
           {"id":"b","pitch":64,"start":0,"end":1,"part_id":"melody","voice_id":"3"},
           {"id":"c","pitch":48,"start":0,"end":2,"part_id":"bass","voice_id":"1"}]
    score=build_score(notes,{"tempo_bpm":120},title="TEST FIXTURE — parts and manual voices")
    assert {part["id"] for part in score["parts"]} == {"melody","bass"}
    for part in score["parts"]:
        for measure in part["staves"][0]["measures"]:
            for voice in measure["voices"]:
                assert voice["id"] == ("3" if part["id"] == "melody" else "1")
                assert sum((fraction(e["duration"]) for e in voice["events"]),Fraction(0)) == fraction(measure["duration"])
    parsed=converter.parseData(score_to_musicxml(score))
    assert len(parsed.parts) == 2
    write_score_midi(score,tmp_path/"parts.mid")
    midi=pretty_midi.PrettyMIDI(str(tmp_path/"parts.mid"))
    assert {i.name:{n.pitch for n in i.notes} for i in midi.instruments} == {"melody":{60,64},"bass":{48}}


def test_waveform_uses_actual_pcm_and_raw_playback_is_not_edited(stored_project):
    client, db, project_id, directory = stored_project
    # Real PCM amplitude with two distinct halves, explicitly a test fixture.
    import array
    with wave.open(str(directory/"normalized.wav"),"wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(22050)
        audio.writeframes(array.array("h",[1000]*22050+[10000]*22050).tobytes())
    (directory.parent/"source").write_bytes((directory/"normalized.wav").read_bytes())
    envelope=client.get(f"/api/projects/{project_id}/waveform")
    assert envelope.status_code == 200
    data=envelope.json()
    assert data["duration"] == 2 and len(data["peaks"]) <= 2000
    assert data["peaks"][0] == pytest.approx(1000/32768)
    assert data["peaks"][-1] == pytest.approx(10000/32768)
    (directory/"raw-note-events.json").write_text(json.dumps({"note_events":[[0.2,1.4,65,.5,[]]]}))
    raw=client.get(f"/api/projects/{project_id}/playback/raw").json()["notes"]
    assert raw[0]["pitch"] == 65 and raw[0]["start"] == .2
    db.save_notes(project_id,1,[],str(uuid.uuid4()))
    assert client.get(f"/api/projects/{project_id}/playback/raw").json()["notes"] == raw
    (directory/"raw-note-events.json").unlink()
    assert client.get(f"/api/projects/{project_id}/playback/raw").status_code == 404

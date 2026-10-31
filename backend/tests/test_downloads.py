"""Saved-score downloads use explicit generated fixtures, not accuracy examples."""
from __future__ import annotations

import io
import re
from urllib.parse import unquote

import pytest

from backend.app.downloads import attachment_header, safe_title, score_filename
from backend.tests.test_score_api import settings_for, stored_project


def test_explicit_melody_register_direction_survives_portable_filename():
    assert safe_title("Song · Melody +1 octave") == "Song-Melody-plus-1-octave"
    assert safe_title("Song · Melody -1 octave") == "Song-Melody-minus-1-octave"
    assert safe_title("Song-1") == "Song-1"


@pytest.mark.parametrize("title", [
    '../../My "Song"\r\nContent-Disposition: injected/\\<>:*?|',
    "CON", ".", "", "🎹", "My song" * 100, "夜の歌" * 100,
    "My\u202esong\x00title",
])
def test_title_is_safe_bounded_and_not_a_path(title):
    saved = {"revision": 17, "score": {"title": title, "settings": {"key_signature": -3}}}
    filename = score_filename(saved, "musicxml")
    assert filename.endswith("_E-flat-major-C-minor_score-r17.musicxml")
    assert len(filename.encode("utf-8")) < 255
    assert not any(character in filename for character in '/\\\r\n\x00:"<>?*|')
    assert ".." not in filename and not filename.startswith(".")
    header = attachment_header(filename)
    assert header.isascii() and "\r" not in header and "\n" not in header


def test_international_song_title_is_preserved_in_attachment_header():
    filename = "夜の歌-Café_D-major-B-minor_score-r2.pdf"
    header = attachment_header(filename)
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", header).group(1)
    assert unquote(encoded) == filename
    assert safe_title("夜の歌 / Café") == "夜の歌-Café"


def test_downloads_use_saved_corrections_transposition_key_and_revision(stored_project):
    import pretty_midi
    from music21 import converter
    from pypdf import PdfReader

    client, db, project_id, _ = stored_project
    base = f"/api/projects/{project_id}"
    project = db.get_project(project_id)
    initial = settings_for(client, project_id)
    assert client.put(base + "/score", json={"notes_revision": 1, "revision": 0,
                      "settings": initial}).status_code == 200
    raw_before = client.get(base + "/exports/raw-midi")
    note = project["notes"][0]
    edit = {key: note.get(key) for key in ("id", "pitch", "start", "end", "velocity", "part_id", "voice_id")}
    edit.update(pitch=65, start=.5, end=1.5)
    settings = {**initial, "tempo_bpm": 120, "key_signature": 2, "transpose_semitones": 2}
    accepted = client.put(base + "/review", json={"notes_revision": 1, "score_revision": 1,
                          "notes": [edit], "settings": settings})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["score_state"]["revision"] == 2

    xml = client.get(base + "/score/exports/musicxml?revision=2")
    midi = client.get(base + "/score/exports/midi?revision=2")
    pdf = client.get(base + "/score/exports/pdf?revision=2")
    for response, extension in ((xml, "musicxml"), (midi, "mid"), (pdf, "pdf")):
        assert response.status_code == 200, response.text[:200] if response.status_code != 200 else ""
        assert response.headers["content-disposition"] == (
            'attachment; filename="DEVELOPMENT-FIXTURE-canonical-notation-API'
            f'_D-major-B-minor_score-r2.{extension}"')
        assert response.headers["x-score-revision"] == "2"
        assert response.headers["x-notes-revision"] == "2"
        assert response.headers["cache-control"] == "no-store"
    parsed = converter.parseData(xml.text)
    assert {pitch.midi for event in parsed.recurse().notes for pitch in event.pitches} == {67}
    assert {signature.sharps for signature in parsed.recurse().getElementsByClass("KeySignature")} == {2}
    events = [note for part in pretty_midi.PrettyMIDI(io.BytesIO(midi.content)).instruments for note in part.notes]
    assert [(event.pitch, event.start, event.end) for event in events] == [(67, .5, 1.5)]
    pdf_text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Saved score revision 2" in pdf_text

    # A draft preview never changes the bytes or filename of the saved revision.
    preview = client.post(base + "/score/preview", json={"notes_revision": 2,
                          "settings": {**settings, "key_signature": -1, "transpose_semitones": -5}})
    assert preview.status_code == 200
    assert client.get(base + "/score/exports/musicxml?revision=2").content == xml.content
    for format in ("musicxml", "midi", "pdf"):
        assert client.get(base + f"/score/exports/{format}?revision=1").status_code == 409
    raw_after = client.get(base + "/exports/raw-midi")
    assert raw_after.content == raw_before.content
    assert raw_after.headers["content-disposition"] == raw_before.headers["content-disposition"]
    assert "original-key_raw-performance-r1-" in raw_after.headers["content-disposition"]
    assert "D-major" not in raw_after.headers["content-disposition"]

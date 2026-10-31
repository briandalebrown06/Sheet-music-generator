"""Real engraving fixtures, explicitly not inferred audio or accuracy examples."""
from __future__ import annotations

import copy
import io
import uuid

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("music21")
pytest.importorskip("verovio")
pytest.importorskip("svglib")
pytest.importorskip("reportlab")
pytest.importorskip("pypdf")
from pypdf import PdfReader

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.pdf_export import PdfExportError, engrave_pdf, validate_pdf
from backend.app.score import build_score, score_to_musicxml


def event(identifier, start, end, pitch, voice_id=None):
    note = {"id": identifier, "start": start, "end": end, "pitch": pitch, "velocity": 80,
            "part_id": "part-1", "provenance": "detected", "model_score": .5,
            "original_timing": {"start": start, "end": end, "pitch": pitch, "velocity": 80}}
    if voice_id:
        note["voice_id"] = voice_id
    return note


def pagination_fixture():
    """48 bars of generated triplets, rests, ties and two heuristic piano staves."""
    events = []
    for measure in range(48):
        for index in range(12):
            if index == 8:
                continue  # A deliberate, visible eighth-triplet rest.
            start = measure * 2 + index / 6
            events.append(event(f"upper-{measure}-{index}", start, start + 1 / 6,
                                [60, 64, 67, 72, 74, 76][index % 6]))
        events.append(event(f"bass-{measure}", measure * 2, measure * 2 + 2.5, 48 + measure % 3))
        events.append(event(f"inner-{measure}", measure * 2 + .5, measure * 2 + 1.5, 55))
    return build_score(events, {"tempo_bpm": 120, "quantization": "eighth_triplet", "piano_split": True},
                       title="DEVELOPMENT FIXTURE - piano pagination, triplets and ties")


@pytest.fixture(scope="module")
def real_pdf(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pdf-real")
    model = pagination_fixture()
    xml = score_to_musicxml(model)
    content = engrave_pdf(xml, Settings(data_dir=directory), 9)
    return content, xml, directory


def test_actual_paginated_vector_pdf_has_full_a4_pages(real_pdf):
    content, xml, directory = real_pdf
    pages = validate_pdf(content)
    assert pages >= 2
    reader = PdfReader(io.BytesIO(content), strict=True)
    assert "Verovio 6.3.0" in reader.metadata.creator
    assert "saved score revision 9" in reader.metadata.creator
    for number, page in enumerate(reader.pages, 1):
        assert "Saved score revision 9" in page.extract_text()
        assert f"{number} / {pages}" in page.extract_text()
        assert "DEVELOPMENT FIXTURE" in page.extract_text()
        assert len(page.get_contents().get_data()) > 1000
        # Notes are real vector paths, not embedded bitmap screenshots.
        assert not list(page.images)
    assert "<actual-notes>3</actual-notes>" in xml and "<staves>2</staves>" in xml
    assert '<tie type="start"' in xml
    assert not list(directory.glob("score-pdf-*"))


def test_actual_timeout_kills_child_and_cleans_isolated_files(tmp_path, real_pdf, monkeypatch):
    import backend.app.pdf_export as pdf
    _, xml, _ = real_pdf
    original_popen = pdf.subprocess.Popen
    children = []
    def slow_fixture(command, **kwargs):
        # A real, deliberately slow test process makes timeout deterministic even
        # on fast hosts. Real engraving is exercised independently above/below.
        child = original_popen([command[0], "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(pdf.subprocess, "Popen", slow_fixture)
    with pytest.raises(PdfExportError, match="time limit") as error:
        engrave_pdf(xml, Settings(data_dir=tmp_path, pdf_timeout_seconds=1), 1)
    assert error.value.status_code == 504
    assert children[0].poll() is not None
    assert not list(tmp_path.glob("score-pdf-*"))


def test_limits_and_invalid_output_fail_clearly(tmp_path, monkeypatch):
    import backend.app.pdf_export as pdf
    for content in (b"not a pdf", b"%PDF-1.4\n%%EOF"):
        with pytest.raises(Exception):
            validate_pdf(content)
    with pytest.raises(PdfExportError, match="input limit"):
        engrave_pdf(" " * (pdf.MAX_XML_BYTES + 1), Settings(data_dir=tmp_path), 1)
    monkeypatch.setattr(pdf, "pdf_capability", lambda: {"available": False, "detail": "Install engraving dependencies"})
    with pytest.raises(PdfExportError, match="Install engraving") as error:
        engrave_pdf("<xml/>", Settings(data_dir=tmp_path), 1)
    assert error.value.status_code == 503


def test_actual_corrupt_xml_is_rejected_and_temp_files_removed(tmp_path):
    with pytest.raises(PdfExportError, match="could not be engraved"):
        engrave_pdf("<score-partwise><invalid", Settings(data_dir=tmp_path), 1)
    assert not list(tmp_path.glob("score-pdf-*"))


@pytest.fixture
def saved_project(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    db = app.state.db
    project_id = str(uuid.uuid4())
    db.create_project(project_id, "DEVELOPMENT FIXTURE - saved PDF", "upload", "fixture.wav", "audio/wav")
    job = db.claim_job("pdf-test", 60)
    events = [event(str(uuid.uuid4()), .1, 1.1, 60)]
    db.record_decoded(job, "pdf-test", 2, 0, 2)
    db.finish(job, "pdf-test", notes=events, duration=2, exports=[])
    with TestClient(app) as client:
        url = f"/api/projects/{project_id}"
        settings = client.get(url + "/score").json()["defaults"]
        assert client.put(url + "/score", json={"notes_revision": 1, "revision": 0, "settings": settings}).status_code == 200
        yield client, db, project_id, url


def test_real_api_pdf_is_saved_revision_and_stale_exports_rejected(saved_project):
    client, db, project_id, url = saved_project
    response = client.get(url + "/score/exports/pdf")
    assert response.status_code == 200, response.text[:500]
    assert validate_pdf(response.content) == 1
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-score-revision"] == "1"
    assert response.headers["x-notes-revision"] == "1"
    assert "score-r1.pdf" in response.headers["content-disposition"]
    notes = copy.deepcopy(db.get_project(project_id)["notes"])
    notes[0]["pitch"] = 65
    assert db.save_notes(project_id, 1, notes, str(uuid.uuid4()))
    assert client.get(url + "/score/exports/pdf").status_code == 409


@pytest.mark.parametrize("format", ["musicxml", "midi", "json", "pdf"])
def test_requested_revision_must_match_before_export(saved_project, format):
    client, _, _, url = saved_project
    assert client.get(url + f"/score/exports/{format}?revision=0").status_code == 409
    assert client.get(url + f"/score/exports/{format}?revision=-1").status_code == 422


@pytest.mark.parametrize("change", ["notes", "score"])
def test_edit_during_engraving_rejects_result(saved_project, real_pdf, monkeypatch, change):
    import backend.app.score_api as api
    client, db, project_id, url = saved_project
    saved = db.get_score(project_id)
    def concurrent_edit(xml, settings, revision):
        assert xml == saved["musicxml"] and revision == 1
        if change == "notes":
            assert db.save_notes(project_id, 1, db.get_project(project_id)["notes"], str(uuid.uuid4()))
        else:
            assert db.save_score(project_id, 1, 1, saved["score"], saved["musicxml"])
        return real_pdf[0]  # A previously engraved, clearly labeled fixture.
    monkeypatch.setattr(api, "engrave_pdf", concurrent_edit)
    assert client.get(url + "/score/exports/pdf").status_code == 409

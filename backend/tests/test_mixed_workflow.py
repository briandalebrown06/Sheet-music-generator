"""Explicit injected separator fixtures test persistence, never separation quality."""
from __future__ import annotations

import io
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from backend.app.audio import AnalysisError
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.mixed_storage import family_bytes
from backend.app.worker import Worker


class FixtureSeparator:
    """Copies a generated mix to stems ONLY to exercise backend plumbing."""
    def __init__(self, *, fail=False, misaligned=False, silent_vocals=False, on_separate=None):
        self.fail, self.misaligned = fail, misaligned
        self.silent_vocals, self.on_separate = silent_vocals, on_separate
        self.calls = 0

    def separate(self, input_path, output_dir, stage_callback=None):
        self.calls += 1
        audio, rate = sf.read(input_path, always_2d=True)
        if self.on_separate:
            self.on_separate()
        stems = []
        for category in ("vocals", "bass", "drums", "other"):
            value = np.zeros_like(audio) if self.silent_vocals and category == "vocals" else audio
            if self.misaligned and category == "other":
                value = value[:-1]
            filename = f"fixture-{category}.wav"
            sf.write(output_dir / filename, value, rate, subtype="FLOAT")
            stems.append({"category": category, "filename": filename})
            if self.fail:
                raise AnalysisError("Explicit test separator failure after writing a partial stem.")
        return {"stems": stems, "manifest": {"engine": "explicit-test-copy-fixture", "model": "none",
                "settings": {"test_fixture": True}, "license": "generated test audio"}}


class FixturePitchedEngine:
    def __init__(self):
        self.calls = 0
    def transcribe(self, path):
        self.calls += 1
        # Explicit injected empty result, never a production/model fallback.
        return []


def generated_audio(seconds=2):
    rate = 44100
    t = np.arange(round(seconds * rate)) / rate
    left = .2 * np.sin(2 * np.pi * 220 * t)
    right = .1 * np.sin(2 * np.pi * 330 * t)
    data = io.BytesIO()
    sf.write(data, np.stack([left, right], axis=1), rate, format="WAV", subtype="PCM_16")
    return data.getvalue()


@pytest.fixture
def settings(tmp_path):
    # repository root is parents[2] from backend/tests; host FFmpeg is also fine.
    binary = Path(__file__).resolve().parents[2] / ".runtime/bin/ffmpeg"
    ffmpeg = str(binary) if binary.is_file() else shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("Real FFmpeg is required for mixed-input plumbing checks")
    return Settings(data_dir=tmp_path / "data", ffmpeg_binary=ffmpeg)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def upload(client, **extra):
    response = client.post("/api/projects", data={"input_mode": "mixed", "title": "DEVELOPMENT FIXTURE - mixture",
        "clip_start_seconds": .25, "clip_end_seconds": 1.75, "instrument_hint": "piano", **extra},
        files={"file": ("generated-test-mixture.wav", generated_audio(), "audio/wav")})
    assert response.status_code == 201, response.text
    return response.json()


def separated(client, settings, **separator_options):
    project = upload(client)
    separator = FixtureSeparator(**separator_options)
    pitched = FixturePitchedEngine()
    worker = Worker(settings, engine=pitched, separator=separator)
    assert worker.run_once()
    return client.get(f"/api/projects/{project['id']}").json(), separator, pitched


def test_solo_default_is_preserved_and_mixed_is_explicit(client):
    response = client.post("/api/projects", files={"file": ("fixture.wav", generated_audio(), "audio/wav")})
    assert response.status_code == 201
    assert response.json()["input_mode"] == "solo"
    assert response.json()["jobs"][0]["kind"] == "transcription"
    mixed = upload(client)
    assert mixed["jobs"][0]["kind"] == "separation" and mixed["notes"] is None
    assert client.post("/api/projects", data={"input_mode": "invented"},
        files={"file": ("fixture.wav", generated_audio())}).status_code == 422


def test_real_stereo_decode_stems_timing_audition_and_reopen(client, settings):
    project, separator, pitched = separated(client, settings)
    assert project["status"] == "completed" and project["notes"] is None
    assert project["exports"] == [] and project["separation_available"]
    assert separator.calls == 1 and pitched.calls == 0
    base = f"/api/projects/{project['id']}"
    result = client.get(base + "/stems").json()
    assert {stem["category"] for stem in result["stems"]} == {"vocals", "bass", "drums", "other"}
    assert result["manifest"]["arrangement"] is False
    assert result["manifest"]["original_recording_offset_seconds"] == .25
    import hashlib
    assert result["manifest"]["original_source_sha256"] == hashlib.sha256(client.get(base + "/audio").content).hexdigest()
    for stem in result["stems"]:
        audio = client.get(stem["audio_url"])
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
        samples, rate = sf.read(io.BytesIO(audio.content), always_2d=True)
        assert samples.shape == (66150, 2) and rate == 44100
        assert not np.array_equal(samples[:, 0], samples[:, 1])
        assert stem["duration_seconds"] == 1.5 and stem["source_origin_seconds"] == .25
        assert stem["transcription_supported"] is (stem["category"] != "drums")
    assert "not an isolated guitar or piano" in next(s for s in result["stems"] if s["category"] == "other")["detail"]
    assert client.get(base + "/separation-manifest").json() == result["manifest"]
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(base + "/stems").json() == result


def test_selection_creates_only_one_persistent_child_and_preserves_origin(client, settings):
    parent, _, _ = separated(client, settings)
    base = f"/api/projects/{parent['id']}"
    def select(_):
        return client.post(base + "/stems/vocals/transcribe")
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(select, range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 201]
    child = replies[0].json()
    assert child["id"] == replies[1].json()["id"]
    assert child["parent_project_id"] == parent["id"] and child["input_mode"] == "solo"
    assert child["source_origin_seconds"] == .25 and child["clip_start_seconds"] == 0
    assert child["clip_end_seconds"] == 1.5 and child["transcription_profile"] == "vocals"
    assert child["engine"] == "pyin-vocals" and child["instrument_hint"] == "voice"
    assert client.get(f"/api/projects/{child['id']}/audio").content == client.get(base + "/stems/vocals/audio").content
    worker = Worker(settings, engine=FixturePitchedEngine())
    assert worker.run_once()
    saved = client.get(f"/api/projects/{child['id']}").json()
    assert saved["status"] == "completed" and saved["source_origin_seconds"] == .25
    assert saved["duration_seconds"] == 1.5
    assert client.get(base + "/stems").json()["children"][0]["id"] == child["id"]
    assert len(list((settings.data_dir / "projects").iterdir())) == 2


def test_drums_never_get_a_pitched_job_and_unready_selection_fails(client, settings):
    parent = upload(client)
    base = f"/api/projects/{parent['id']}"
    assert client.post(base + "/stems/bass/transcribe").status_code == 409
    Worker(settings, separator=FixtureSeparator()).run_once()
    assert client.post(base + "/stems/drums/transcribe").status_code == 422
    assert client.post(base + "/stems/percussion/transcribe").status_code == 422
    assert client.get(base + "/stems/drums/audio").status_code == 200
    assert client.get(base + "/stems").json()["children"] == []


@pytest.mark.parametrize("options,expected", [({"fail": True}, "test separator failure"), ({"misaligned": True}, "sample alignment")])
def test_bad_attempts_clean_outputs_keep_original_and_retry_bound(client, settings, options, expected):
    parent, _, _ = separated(client, settings, **options)
    assert parent["status"] == "failed" and expected in parent["jobs"][0]["error"]
    directory = settings.data_dir / "projects" / parent["id"]
    assert (directory / "source").is_file()
    assert not (directory / parent["jobs"][0]["id"]).exists()
    assert not parent["analysis_available"] and not parent["separation_available"]
    base = f"/api/projects/{parent['id']}"
    for _ in range(2):
        assert client.post(base + "/retry").status_code == 200
        Worker(settings, separator=FixtureSeparator(**options)).run_once()
    assert client.post(base + "/retry").status_code == 409
    jobs = client.get(base).json()["jobs"]
    assert len(jobs) == 3 and all(job["kind"] == "separation" for job in jobs)


def test_cancellation_fences_publication_and_cleans_partial_files(client, settings):
    project = upload(client)
    base = f"/api/projects/{project['id']}"
    separator = FixtureSeparator(on_separate=lambda: client.post(base + "/cancel"))
    assert Worker(settings, separator=separator).run_once()
    canceled = client.get(base).json()
    assert canceled["status"] == "canceled" and not canceled["separation_available"]
    assert not (settings.data_dir / "projects" / project["id"] / canceled["jobs"][0]["id"]).exists()
    assert client.post(base + "/retry").status_code == 200
    assert Worker(settings, separator=FixtureSeparator()).run_once()
    assert client.get(base).json()["status"] == "completed"


def test_worker_restart_recovers_mixed_job_without_publishing_old_stems(client, settings):
    parent = upload(client)
    db = Database(settings.database_path)
    old = db.claim_job("dead-worker", lease_seconds=-1)
    directory = settings.data_dir / "projects" / parent["id"] / old["id"]
    directory.mkdir()
    (directory / "partial.wav").write_bytes(b"explicit incomplete test output")
    assert Worker(settings, separator=FixtureSeparator()).run_once()
    result = client.get(f"/api/projects/{parent['id']}").json()
    assert result["status"] == "completed" and len(result["jobs"]) == 2
    assert not directory.exists()
    assert not db.finish(old, "dead-worker", separation_bundle={"stems": [], "manifest": {}})


def test_silent_stem_can_be_auditioned_but_not_fabricated_into_notes(client, settings):
    parent, _, _ = separated(client, settings, silent_vocals=True)
    child = client.post(f"/api/projects/{parent['id']}/stems/vocals/transcribe").json()
    pitched = FixturePitchedEngine()
    Worker(settings, engine=pitched).run_once()
    result = client.get(f"/api/projects/{child['id']}").json()
    assert result["status"] == "failed" and "silent" in result["jobs"][0]["error"]
    assert result["notes"] is None and pitched.calls == 0
    assert client.get(f"/api/projects/{parent['id']}/stems/vocals/audio").status_code == 200


def test_storage_limit_rejects_selection_without_orphan_child(client, settings):
    parent, _, _ = separated(client, settings)
    db = Database(settings.database_path)
    current = family_bytes(settings, db, parent)
    limited = replace(settings, max_separation_storage_bytes=current + 1)
    with TestClient(create_app(limited)) as limited_client:
        response = limited_client.post(f"/api/projects/{parent['id']}/stems/bass/transcribe")
    assert response.status_code == 413 and "MAX_SEPARATION_STORAGE_MB" in response.text
    assert db.list_children(parent["id"]) == []
    assert len(list((settings.data_dir / "projects").iterdir())) == 1


def test_copy_failure_reports_local_storage_problem_and_rolls_back(client, settings, monkeypatch):
    from backend.app import mixed_api
    parent, _, _ = separated(client, settings)
    def full_disk(*args, **kwargs):
        raise OSError("Explicit test fixture: full disk during stem copy")
    monkeypatch.setattr(mixed_api.shutil, "copyfile", full_disk)
    response = client.post(f"/api/projects/{parent['id']}/stems/bass/transcribe")
    assert response.status_code == 507 and "disk space" in response.text
    assert Database(settings.database_path).list_children(parent["id"]) == []
    assert len(list((settings.data_dir / "projects").iterdir())) == 1

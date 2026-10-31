"""Atomic review saves and real audio data for synchronized local playback."""
from __future__ import annotations

import array
import hashlib
import json
import math
import shutil
import sys
import tempfile
import uuid
import wave
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .audio import AnalysisError, prepare_analysis_audio
from .engine import atomic_json
from .note_edits import NoteEdit, normalize_edits
from .notes import detected_notes
from .performance import write_performance_midi
from .score import ScoreSettings, build_score, default_score_settings, score_to_musicxml


class ReviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes_revision: int = Field(ge=0)
    score_revision: int = Field(ge=0)
    notes: list[NoteEdit] = Field(max_length=10000)
    settings: ScoreSettings


def waveform_peaks(path: Path, bins: int = 2000) -> dict:
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("Waveform needs normalized mono PCM audio.")
        frames, rate = audio.getnframes(), audio.getframerate()
        per_bin = max(1, math.ceil(frames / bins))
        peaks = []
        try:
            import numpy as np
        except ImportError:
            np = None
        while data := audio.readframes(per_bin):
            if np is not None:
                samples = np.frombuffer(data, dtype="<i2").astype(np.int32)
                peaks.append(float(np.max(np.abs(samples), initial=0)) / 32768)
            else:
                samples = array.array("h", data)
                if sys.byteorder != "little":
                    samples.byteswap()
                peaks.append(max((abs(sample) for sample in samples), default=0) / 32768)
        return {"duration": frames / rate, "peaks": peaks, "time_basis": "clip_seconds", "sample_rate": rate}


def install_review_routes(app, db, settings, project_or_404):
    def song_reference(project_id):
        """Resolve only authorized persisted ancestry, never client-supplied URLs.

        Derived editions copy their instrumental source. Follow that chain to
        the uploaded mix, checking every ancestor even when the final file is
        cached. Offsets are absolute original-file seconds, not score beats.
        """
        project = project_or_404(project_id)
        current, seen = project, set()
        for _ in range(16):
            if current["id"] in seen:
                raise HTTPException(404, "The full original song reference is unavailable.")
            seen.add(current["id"])
            parent_id = current.get("arrangement_source_id") or current.get("parent_project_id")
            if not parent_id:
                break
            current = project_or_404(parent_id)
            if current["owner_id"] != project["owner_id"]:
                raise HTTPException(404, "The full original song reference is unavailable.")
        else:
            raise HTTPException(404, "The full original song reference is unavailable.")
        if current["id"] == project["id"]:
            raise HTTPException(404, "This project already uses its original recording.")
        start = (project.get("source_origin_seconds") or 0) + (project.get("clip_start_seconds") or 0)
        duration = project.get("clip_duration_seconds")
        if duration is None and project.get("clip_end_seconds") is not None:
            duration = project["clip_end_seconds"] - (project.get("clip_start_seconds") or 0)
        total = current.get("original_duration_seconds") or current.get("duration_seconds")
        if (duration is None or total is None or not all(math.isfinite(value) for value in (start, duration, total))
                or start < 0 or duration <= 0 or start + duration > total + .001):
            raise HTTPException(404, "The original-song clip alignment is unavailable.")
        storage_root = (settings.data_dir / "projects").resolve()
        directory = (storage_root / current["id"]).resolve()
        source = (directory / "source").resolve()
        if not directory.is_relative_to(storage_root) or not source.is_relative_to(directory) or not source.is_file():
            raise HTTPException(404, "The full original song file is unavailable.")
        return project, current, source, float(start), float(duration)

    @app.get("/api/projects/{project_id}/playback/reference")
    def reference_metadata(project_id: str):
        project_or_404(project_id)
        try:
            _, original, _, start, duration = song_reference(project_id)
        except HTTPException:
            # Do not expose the identity or title of a missing/private ancestor.
            return {"available": False, "reason": "Full original song is unavailable; the current source audio remains selectable."}
        return {"available": True, "title": original["title"],
                "clip_start_seconds": start, "clip_duration_seconds": duration,
                "time_basis": "Audio position equals review clip position plus clip_start_seconds.",
                "audio_url": f"/api/projects/{project_id}/playback/reference/audio",
                "waveform_url": f"/api/projects/{project_id}/playback/reference/waveform"}

    @app.get("/api/projects/{project_id}/playback/reference/audio")
    def reference_audio(project_id: str):
        _, original, source, _, _ = song_reference(project_id)
        return FileResponse(source, media_type=db.media_type(original["id"]),
                            filename=original["original_filename"], content_disposition_type="inline",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/projects/{project_id}/playback/reference/waveform")
    def reference_waveform(project_id: str):
        _, original, source, start, duration = song_reference(project_id)
        stat = source.stat()
        signature = hashlib.sha256(json.dumps([original["id"], stat.st_size, stat.st_mtime_ns, start, duration]).encode()).hexdigest()[:20]
        cache = settings.data_dir / "projects" / project_id / f"reference-waveform-{signature}.json"
        if cache.is_file():
            return json.loads(cache.read_text())
        try:
            with tempfile.TemporaryDirectory(prefix="reference-waveform-", dir=settings.data_dir) as temporary:
                selected = Path(temporary) / "selected.wav"
                prepare_analysis_audio(source, selected, settings, start, start + duration)
                result = waveform_peaks(selected)
        except (AnalysisError, ValueError, wave.Error) as exc:
            raise HTTPException(422, str(exc)) from exc
        atomic_json(cache, result)
        return result

    def compile_review(project: dict, edit: ReviewEdit):
        if project["status"] != "completed" or project["notes"] is None:
            raise HTTPException(409, "Complete transcription before reviewing notes.")
        if project["notes_revision"] != edit.notes_revision:
            raise HTTPException(409, "The performance notes changed elsewhere. Reload the saved project before saving this draft.")
        saved = db.get_score(project["id"])
        if (saved["revision"] if saved else 0) != edit.score_revision:
            raise HTTPException(409, "The musical settings changed elsewhere. Reload the saved score before saving this review.")
        notes = normalize_edits(project, edit.notes, db.note_registry(project["id"]))
        # Removing a note also removes its no-longer-applicable staff override.
        score_settings = edit.settings.model_copy(deep=True)
        ids = {note["id"] for note in notes}
        score_settings.staff_overrides = {key: value for key, value in score_settings.staff_overrides.items() if key in ids}
        try:
            score = build_score(notes, score_settings, title=project["title"],
                                clip_duration_seconds=project["clip_duration_seconds"] or project["duration_seconds"],
                                source_notes_revision=edit.notes_revision + 1)
            from .piano_api import attach_playability
            attach_playability(project, score, notes)
            xml = score_to_musicxml(score)
        except ImportError as exc:
            raise HTTPException(503, "Review notation requires the installed music21 analysis environment.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return notes, score, xml

    @app.post("/api/projects/{project_id}/review/preview")
    def preview_review(project_id: str, edit: ReviewEdit):
        project = project_or_404(project_id)
        _, score, xml = compile_review(project, edit)
        return {"score": score, "musicxml": xml, "source_notes_revision": edit.notes_revision}

    @app.put("/api/projects/{project_id}/review")
    def save_review(project_id: str, edit: ReviewEdit):
        project = project_or_404(project_id)
        notes, score, xml = compile_review(project, edit)
        edit_id = str(uuid.uuid4())
        directory = settings.data_dir / "projects" / project_id / "edits" / edit_id
        directory.mkdir(parents=True, mode=0o700)
        try:
            performance = [note for note in notes if note.get("role") != "vocal_cue"] if project.get("output_mode") == "accompaniment" else notes
            try:
                write_performance_midi(performance, directory / "score.mid")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            atomic_json(directory / "notes.json", {"schema_version": 2, "revision": edit.notes_revision + 1,
                        "time_basis": "seconds relative to selected clip; unquantized", "notes": notes})
            db.check_storage_usage(project["owner_id"])
            accepted_project = db.save_notes(project_id, edit.notes_revision, notes, edit_id,
                                            score_bundle={"score": score, "musicxml": xml},
                                            score_revision=edit.score_revision, return_project=True)
            if not accepted_project:
                raise HTTPException(409, "A newer review was saved while this score was built. Reload before saving again.")
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return {"project": accepted_project, "score_state": {
            "score": score, "musicxml": xml, "revision": edit.score_revision + 1,
            "source_notes_revision": edit.notes_revision + 1, "stale": False,
            "defaults": default_score_settings(accepted_project)}}

    @app.get("/api/projects/{project_id}/waveform")
    def waveform(project_id: str):
        project = project_or_404(project_id)
        directory = settings.data_dir / "projects" / project_id
        original = directory / "source"
        if not original.is_file():
            raise HTTPException(404, "The original audio file is unavailable.")
        signature = hashlib.sha256(json.dumps([original.stat().st_size, original.stat().st_mtime_ns,
                    project["clip_start_seconds"], project["clip_end_seconds"]]).encode()).hexdigest()[:20]
        cache = directory / f"waveform-{signature}.json"
        if cache.is_file():
            return json.loads(cache.read_text())
        job_id = db.analysis_job_id(project_id)
        normalized = directory / job_id / "normalized.wav" if job_id else None
        try:
            if normalized and normalized.is_file():
                result = waveform_peaks(normalized)
            else:
                with tempfile.TemporaryDirectory(prefix="review-waveform-", dir=settings.data_dir) as temporary:
                    selected = Path(temporary) / "selected.wav"
                    prepare_analysis_audio(original, selected, settings, project["clip_start_seconds"], project["clip_end_seconds"])
                    result = waveform_peaks(selected)
        except (AnalysisError, ValueError, wave.Error) as exc:
            raise HTTPException(422, str(exc)) from exc
        atomic_json(cache, result)
        return result

    @app.get("/api/projects/{project_id}/playback/raw")
    def raw_playback(project_id: str):
        project_or_404(project_id)
        storage = db.storage(project_id)
        job_id = storage["result_job_id"]
        path = settings.data_dir / "projects" / project_id / str(job_id) / "raw-note-events.json"
        if not job_id or not path.is_file():
            raise HTTPException(404, "Raw model events were not preserved for this older project. Edited performance playback remains available.")
        raw = json.loads(path.read_text())
        project = project_or_404(project_id)
        namespace = (project.get("arrangement") or {}).get("raw_event_namespace") or job_id
        return {"notes": detected_notes(raw["note_events"], namespace), "time_basis": "clip_seconds",
                "detail": "Original detected timing and discrete pitches; this simple synthesizer does not reproduce pitch bends or instrument timbre."}

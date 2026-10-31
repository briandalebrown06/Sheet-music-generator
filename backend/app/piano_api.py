"""Explicit, reversible accompaniment derivation and saved-score checks.

These routes never run audio inference or pretend editorial notes were detected.
A derived project has its own revisions, original-audio copy and canonical exports.
"""
from __future__ import annotations

import hashlib
import copy
import json
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .engine import atomic_json
from .database import IdempotencyConflict, QuotaError
from .note_edits import NoteEdit, normalize_edits
from .performance import write_performance_midi
from .piano import ENGINE_VERSION, PianoSettings, arrange_piano, assess_playability
from .readability import prepare_readable_notes
from .score import ScoreSettings, build_score, default_score_settings, score_to_musicxml


class AccompanimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes_revision: int = Field(ge=0)
    score_revision: int = Field(ge=0)
    piano_settings: PianoSettings = Field(default_factory=PianoSettings)
    settings: ScoreSettings | None = None
    edition_mode: Literal["faithful", "accompaniment", "solo_piano"] | None = None
    melody_project_id: uuid.UUID | None = None
    melody_notes_revision: int | None = Field(default=None, ge=1)
    melody_octave_shift: Literal[-12, 0, 12] = 0


class PlayabilityPreview(AccompanimentRequest):
    notes: list[NoteEdit] | None = Field(default=None, max_length=10000)


class PianoEditionRequest(AccompanimentRequest):
    edition_mode: Literal["faithful", "accompaniment", "solo_piano"] = Field(...)


def saved_piano_settings(project):
    return PianoSettings.model_validate((project.get("arrangement") or {}).get("settings") or {})


def attach_playability(project, score, notes):
    """Check exact rendered transposition; assessment never revoices it."""
    edition = (project.get("arrangement") or {}).get("edition_mode")
    if edition:
        score["edition_mode"] = edition
        score["edition_label"] = EDITION_LABELS[edition] + " · review required"
        shift = (project.get("arrangement") or {}).get("melody_octave_shift", 0)
        if shift:
            score["edition_label"] += f" · melody {shift // 12:+d} octave"
    if project.get("output_mode") == "accompaniment" or score["settings"].get("piano_split"):
        score["playability"] = assess_playability(notes, saved_piano_settings(project), score["settings"])
    return score


EDITION_LABELS = {"faithful": "Faithful instrumental transcription", "accompaniment": "Singer's piano accompaniment", "solo_piano": "Solo piano arrangement"}


def install_piano_routes(app, db, settings, project_or_404):
    def snapshot(project_id, request=None):
        project = project_or_404(project_id)
        if project["status"] != "completed" or project["notes"] is None or project["input_mode"] == "mixed":
            raise HTTPException(409, "Complete a pitched-part transcription before preparing a piano accompaniment.")
        if len(project["notes"]) > 10000:
            raise HTTPException(422, "Piano arrangement currently supports at most 10,000 notes. Select a shorter clip; the complete raw transcription is retained.")
        saved = db.get_score(project_id)
        revision = saved["revision"] if saved else 0
        if request and (request.notes_revision != project["notes_revision"] or request.score_revision != revision):
            raise HTTPException(409, "The saved notes or score changed. Reload before checking or arranging.")
        musical = (request.settings if request and request.settings else
                   ScoreSettings.model_validate(saved["score"]["settings"] if saved else default_score_settings(project)))
        return project, saved, revision, musical

    @app.get("/api/projects/{project_id}/playability")
    def check_saved(project_id: str):
        project, saved, revision, musical = snapshot(project_id)
        piano = saved_piano_settings(project)
        return {"settings": piano.model_dump(), "report": assess_playability(project["notes"], piano, musical),
                "notes_revision": project["notes_revision"], "score_revision": revision,
                "score_stale": bool(saved and saved["source_notes_revision"] != project["notes_revision"])}

    @app.post("/api/projects/{project_id}/playability/preview")
    def check_preview(project_id: str, request: PlayabilityPreview):
        project, _, revision, musical = snapshot(project_id, request)
        notes = normalize_edits(project, request.notes, db.note_registry(project_id)) if request.notes is not None else project["notes"]
        return {"settings": request.piano_settings.model_dump(),
                "report": assess_playability(notes, request.piano_settings, musical),
                "notes_revision": project["notes_revision"], "score_revision": revision}

    @app.get("/api/projects/{project_id}/musical-analysis")
    def musical_analysis(project_id: str):
        project, _, _, _ = snapshot(project_id)
        from .musical_analysis import recommend_musical_settings
        return {**recommend_musical_settings(project["notes"]), "notes_revision": project["notes_revision"]}

    @app.get("/api/projects/{project_id}/piano-editions")
    def list_editions(project_id: str):
        project_or_404(project_id)
        with db.connect() as connection:
            rows = connection.execute("SELECT id,title,arrangement_json FROM projects WHERE arrangement_source_id=? AND deleted_at IS NULL ORDER BY created_at DESC", (project_id,)).fetchall()
        return {"editions": [{"id": row["id"], "title": row["title"],
                              "edition_mode": json.loads(row["arrangement_json"] or "{}").get("edition_mode")}
                             for row in rows if json.loads(row["arrangement_json"] or "{}").get("edition_mode")]}

    @app.post("/api/projects/{project_id}/piano-editions", status_code=201)
    def create_edition(project_id: str, request: PianoEditionRequest):
        return create_accompaniment(project_id, request)

    @app.post("/api/projects/{project_id}/accompaniment", status_code=201)
    def create_accompaniment(project_id: str, request: AccompanimentRequest):
        project, saved, revision, musical = snapshot(project_id, request)
        inherited_mode = (project.get("arrangement") or {}).get("edition_mode")
        edition_label_mode = request.edition_mode or (inherited_mode if inherited_mode != "faithful" else None)
        if saved and saved["source_notes_revision"] != project["notes_revision"]:
            raise HTTPException(409, "Save notation for the current notes before deriving an accompaniment.")
        melody = None
        if request.melody_project_id:
            melody = project_or_404(str(request.melody_project_id))
            if not request.edition_mode or request.edition_mode == "faithful":
                raise HTTPException(422, "Choose an accompaniment or solo-piano edition to include a vocal source.")
            if melody["status"] != "completed" or not melody.get("notes") or melody.get("transcription_profile") != "vocals":
                raise HTTPException(409, "Complete the vocal-stem transcription before including its melody.")
            if melody["notes_revision"] != request.melody_notes_revision:
                raise HTTPException(409, "The vocal notes changed. Reload before preparing this edition.")
            parent = project.get("parent_project_id")
            if not parent or melody.get("parent_project_id") != parent:
                raise HTTPException(422, "Choose the vocal transcription from this same mixed recording; unrelated performances cannot be aligned automatically.")
            if abs((melody.get("source_origin_seconds") or 0) - (project.get("source_origin_seconds") or 0)) > .001 or abs((melody.get("clip_duration_seconds") or 0) - (project.get("clip_duration_seconds") or 0)) > .05:
                raise HTTPException(422, "The vocal and instrumental clips must have matching origins and durations.")
        if request.edition_mode == "solo_piano" and melody is None:
            raise HTTPException(422, "Solo piano requires the aligned vocal transcription so its melody can lead the arrangement.")
        if request.melody_octave_shift and request.edition_mode != "solo_piano":
            raise HTTPException(422, "Melody octave placement is an explicit solo-piano arrangement choice.")
        directory = settings.data_dir / "projects" / project_id
        if not (directory / "source").is_file():
            raise HTTPException(409, "This project's original recording is missing; restore it before deriving an accompaniment.")
        musical = musical.model_copy(deep=True)
        musical.piano_split = True
        musical.readability_mode = "accompaniment"
        if request.edition_mode in {"accompaniment", "solo_piano"} and melody and "triplet" not in musical.quantization:
            musical.melody_quantization = "sixteenth"
        # An old staff override remains a visual instruction, independent of hands.
        from .piano_editions import ENGINE_VERSION as EDITIONS_ENGINE
        fingerprint = hashlib.sha256(json.dumps({"pipeline": "piano-accompaniment-v2", "engine": EDITIONS_ENGINE if request.edition_mode else ENGINE_VERSION, "source": project_id, "notes_revision": project["notes_revision"],
            "score_revision": revision, "edition_mode": request.edition_mode, "melody_octave_shift": request.melody_octave_shift, "melody": (melody["id"], melody["notes_revision"]) if melody else None, "piano": request.piano_settings.model_dump(), "score": musical.model_dump()}, sort_keys=True).encode()).hexdigest()
        with db.connect() as connection:
            duplicate = connection.execute("SELECT id FROM projects WHERE arrangement_source_id=? AND arrangement_fingerprint=? AND deleted_at IS NULL", (project_id, fingerprint)).fetchone()
        if duplicate:
            return project_or_404(duplicate["id"])
        try:
            source_notes = copy.deepcopy(project["notes"])
            if project.get("transcription_profile") == "vocals" or project.get("instrument_hint") == "voice":
                for note in source_notes:
                    if not note.get("role"):
                        note["role"] = "vocal_cue"
                if all(note.get("role") == "vocal_cue" for note in source_notes):
                    raise ValueError("This source contains only a vocal melody. Choose a transcribed instrumental part for accompaniment; sung notes are not automatically added to the pianist's hands.")
            if request.edition_mode:
                from .piano_editions import prepare_edition
                prepared = prepare_edition(source_notes, copy.deepcopy(melody["notes"]) if melody else [], request.edition_mode, request.piano_settings, musical, melody_octave_shift=request.melody_octave_shift)
            elif project.get("output_mode") == "accompaniment":
                from .piano import refine_piano_texture
                prepared = refine_piano_texture(source_notes, request.piano_settings, musical)
            else:
                prepared = arrange_piano(source_notes, request.piano_settings, musical)
                from .piano import refine_piano_texture
                lighter = refine_piano_texture(prepared["notes"], request.piano_settings, musical)
                prepared.update(notes=lighter["notes"], report=lighter["report"],
                                changes=prepared["changes"] + lighter["changes"],
                                density_before=lighter["density_before"], density_after=lighter["density_after"],
                                texture_refinement=lighter.get("refinement"))
            # Refinement already records its explicit timing decisions. Do not
            # rerun general rhythmic cleanup on an already saved arrangement.
            instrumental = [n for n in prepared["notes"] if n.get("role") not in {"melody", "vocal_cue"}]
            vocal_events = [n for n in prepared["notes"] if n.get("role") in {"melody", "vocal_cue"}]
            readable = ({"notes": prepared["notes"], "changes": [],
                         "limitations": ["General rhythmic cleanup is skipped for a saved arrangement. Any explicit secondary-note release or register changes are recorded in the arrangement change log; original timing stays preserved."]}
                        if prepared.get("preserve_timing") else prepare_readable_notes(instrumental if request.edition_mode else prepared["notes"], musical))
            if request.edition_mode and not prepared.get("preserve_timing"):
                readable["notes"].extend(vocal_events)
                readable["notes"].sort(key=lambda n: (n["start"], n["pitch"], n["id"]))
            melodic_readability = None
            if request.edition_mode in {"accompaniment", "solo_piano"}:
                from .edition_readability import prepare_edition_notes
                melodic_readability = prepare_edition_notes(readable["notes"], musical, mode=request.edition_mode)
                before_errors = assess_playability(readable["notes"], request.piano_settings, musical)["summary"]["error_count"]
                after_errors = assess_playability(melodic_readability["notes"], request.piano_settings, musical)["summary"]["error_count"]
                if after_errors <= before_errors:
                    readable["notes"] = melodic_readability["notes"]
                    readable["changes"].extend(melodic_readability["changes"])
                else:
                    melodic_readability["settings"]["applied"] = False
                    melodic_readability["limitations"].append("Melodic duration changes were not applied because they introduced additional physical hand conflicts.")
            notes = readable["notes"]
            for note in notes:
                if request.edition_mode:
                    note["source_project_id"] = melody["id"] if melody and note.get("edition_source") == "vocal" else project_id
            # A removed note cannot leave an invalid visual override behind.
            ids = {note["id"] for note in notes}
            musical.staff_overrides = {key: value for key, value in musical.staff_overrides.items() if key in ids}
            previous = project.get("arrangement") or {}
            generation = int(previous.get("generation", 1)) + 1 if project.get("output_mode") == "accompaniment" else 1
            title_root = previous.get("title_root") or (project["title"] if project.get("output_mode") == "accompaniment" else project["title"][:125] + " · Piano accompaniment")
            # Separate projects each begin at score revision 1. Make the
            # revoice generation visible in their titles/download filenames.
            title = title_root if generation == 1 else title_root[:125] + f" · Revoice {generation}"
            if request.edition_mode:
                title_root = project["title"][:100] + " · " + EDITION_LABELS[request.edition_mode]
                if request.melody_octave_shift:
                    title_root += f" · Melody {request.melody_octave_shift // 12:+d} octave"
                title, generation = title_root, 1
            score = build_score(notes, musical, title=title, source_notes_revision=1,
                                clip_duration_seconds=project["clip_duration_seconds"] or project["duration_seconds"])
            report = assess_playability(notes, request.piano_settings, musical)
            score["playability"] = report
            if edition_label_mode:
                score["edition_mode"] = edition_label_mode
                score["edition_label"] = EDITION_LABELS[edition_label_mode] + " · review required"
                register = request.melody_octave_shift if request.edition_mode else previous.get("melody_octave_shift", 0)
                if register:
                    score["edition_label"] += f" · melody {register // 12:+d} octave"
            xml = score_to_musicxml(score)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        identifier, result_id = str(uuid.uuid4()), str(uuid.uuid4())
        destination = settings.data_dir / "projects" / identifier
        output = destination / result_id
        storage = db.storage(project_id)
        source_output = directory / str(storage["result_job_id"])
        analysis = directory / str(storage["analysis_job_id"]) / "normalized.wav"
        raw_midi = source_output / "raw-performance.mid"
        metadata = {"schema_version": 1, "kind": "derived_accompaniment", "source_project_id": project_id,
                    "generation": generation, "title_root": title_root,
                    "edition_mode": edition_label_mode,
                    "melody_octave_shift": request.melody_octave_shift if request.edition_mode else previous.get("melody_octave_shift", 0),
                    "output_mode": "transcription" if request.edition_mode == "faithful" else "accompaniment",
                    "edition_limitations": prepared.get("limitations", []),
                    "source_counts": prepared.get("source_counts") or previous.get("source_counts"),
                    "melody_selection": prepared.get("melody_selection") or previous.get("melody_selection"),
                    "melody_source_project_id": melody["id"] if melody else None,
                    "melody_source_notes_revision": melody["notes_revision"] if melody else None,
                    "engine": prepared["engine"], "method": prepared["method"],
                    "refinement": prepared.get("refinement"),
                    "texture_refinement": prepared.get("texture_refinement"),
                    "density_before": prepared.get("density_before"),
                    "density_after": report.get("density", prepared.get("density_after")),
                    "readability_version": 1, "locked_note_ids": prepared.get("locked_note_ids", []),
                    "source_notes_revision": project["notes_revision"], "source_score_revision": revision,
                    "settings": request.piano_settings.model_dump(), "before_report": prepared["before_report"],
                    "report": report, "changes": prepared["changes"], "readability_changes": readable["changes"],
                    "readability_limitations": readable.get("limitations", []),
                    "melodic_readability": {"settings": melodic_readability["settings"], "limitations": melodic_readability["limitations"]} if melodic_readability else None,
                    "raw_midi_available": raw_midi.is_file(), "analysis_available": analysis.is_file(),
                    "raw_event_namespace": (project.get("arrangement") or {}).get("raw_event_namespace", storage["result_job_id"]),
                    "detail": ("Instrumental detected pitches and performance timing retained; notation is an estimated reading of that transcription." if request.edition_mode == "faithful" else "Editorial piano arrangement derived from saved detected notes, not newly detected performance. Original transcription remains unchanged. No human validation is claimed.")}
        def prepare_files():
            output.mkdir(parents=True, mode=0o700)
            shutil.copyfile(directory / "source", destination / "source")
            if analysis.is_file():
                shutil.copyfile(analysis, output / "normalized.wav")
            for name in ("raw-performance.mid", "raw-note-events.json"):
                source = source_output / name
                if source.is_file():
                    shutil.copyfile(source, output / name)
            write_performance_midi([note for note in notes if note.get("role") != "vocal_cue"], output / "score.mid")
            atomic_json(output / "notes.json", {"schema_version": 2, "revision": 1, "notes": notes,
                        "time_basis": "clip seconds; editorial accompaniment"})
            if melody:
                atomic_json(output / "melody-source-notes.json", {"project_id": melody["id"], "notes_revision": melody["notes_revision"], "notes": melody["notes"]})
            atomic_json(output / "manifest.json", metadata)
        reserved = (directory / "source").stat().st_size + sum(path.stat().st_size for path in (analysis, raw_midi, source_output / "raw-note-events.json") if path.is_file()) + 32 * 1024**2
        try:
            result = db.create_arrangement(project_id, identifier, result_id, project["notes_revision"], revision,
                notes, score, xml, metadata, fingerprint, prepare_files, reserved)
        except (IdempotencyConflict, QuotaError):
            shutil.rmtree(destination, ignore_errors=True)
            raise
        except ValueError as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(422, str(exc)) from exc
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        if result["id"] != identifier:
            shutil.rmtree(destination, ignore_errors=True)
        return result

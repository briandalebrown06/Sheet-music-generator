from __future__ import annotations

import math
import errno
import logging
import hashlib
import re
import json
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .audio import AnalysisError, SUPPORTED_EXTENSIONS, normalize_audio, sniff_audio
from .config import Settings
from .database import Database, IdempotencyConflict, QuotaError
from .engine import atomic_json, capabilities
from .performance import write_performance_midi
from .pipeline import ARTIFACTS
from .security import LocalOnlyMiddleware
from .auth import current_user_id, current_session, install_auth_routes
from .project_lifecycle import ProjectLeaseMiddleware
from .score_api import install_score_routes
from .note_edits import NoteEdit, normalize_edits
from .review_api import install_review_routes
from .pdf_export import pdf_capability
from .mixed_api import install_mixed_routes
from .downloads import performance_filename
from .uploads import install_upload_routes
from .piano_api import install_piano_routes


class NotesEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    notes: list[NoteEdit] = Field(max_length=10000)


class PreviewResponse(FileResponse):
    """Remove transient audio even if response streaming is cancelled."""

    def __init__(self, path: Path, directory: Path):
        self.directory = directory
        super().__init__(path, media_type="audio/wav", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)


async def save_audio(file: UploadFile, directory: Path, settings: Settings) -> tuple[str, str]:
    """Preserve bytes under a generated directory, never use user paths to store."""
    filename = Path((file.filename or "recording.webm").replace("\\", "/")).name[:255]
    try:
        if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, "Choose WAV, MP3, M4A, MP4, FLAC, OGG, or WebM audio.")
        header = await file.read(64)
        media_type = sniff_audio(header)
        if not header:
            raise HTTPException(415, "The audio file is empty. Choose a recording with audio.")
        if not media_type:
            raise HTTPException(415, "The file does not contain a recognized audio container.")
        await file.seek(0)
        size = 0
        with (directory / "source").open("wb") as destination:
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "Audio file exceeds the configured size limit.")
                destination.write(chunk)
        return filename, media_type
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HTTPException(507, "Private audio storage is full. Ask the operator to free storage, then retry this submission.") from exc
        raise
    finally:
        await file.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    db = Database(settings.database_path, settings.max_job_attempts, settings=settings)
    db.initialize()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Score Studio local API", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.db = settings, db
    app.add_middleware(ProjectLeaseMiddleware, settings=settings, db=db)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "X-Chunk-SHA256"])
    app.add_middleware(LocalOnlyMiddleware, settings=settings)
    install_auth_routes(app, settings, settings.database_path)
    install_upload_routes(app, db, settings)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        from fastapi.responses import JSONResponse
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            return JSONResponse({"detail": "Private storage is full. Ask the operator to free storage, then retry."}, status_code=507)
        # Filesystem paths may identify private content; log only the category.
        logging.getLogger("score_api").error("Private storage operation failed (errno=%s)", exc.errno)
        return JSONResponse({"detail": "A private storage operation failed. Ask the operator to check storage permissions and health."}, status_code=500)

    @app.exception_handler(QuotaError)
    async def quota_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def project_or_404(project_id: str) -> dict:
        try:
            uuid.UUID(project_id)
        except ValueError:
            raise HTTPException(404, "Project not found.")
        project = db.get_project(project_id)
        if project is None or project["owner_id"] != current_user_id():
            raise HTTPException(404, "Project not found.")
        return project

    @app.get("/api/health")
    def health():
        if settings.authentication_required and current_session() is None:
            return {"status": "ok"}
        worker = db.worker_status()
        reported = worker.pop("capabilities")
        available = reported if worker["online"] and reported else capabilities(settings.ffmpeg_binary, settings.basic_pitch_runtime)
        pdf = pdf_capability()
        from .separation import separation_capability
        separation = separation_capability(settings)
        available["pdf"] = pdf["available"]
        available["separation"] = separation["available"]
        available["resumable_upload"] = True
        return {"status": "ok", "engine": "basic-pitch", "mode": "authenticated" if settings.authentication_required else "local-single-user",
                "capabilities": available, "pdf_export": pdf, "separation": separation,
                "capabilities_source": "worker" if worker["online"] and reported else "api-environment",
                "worker": worker,
                "limits": {"max_upload_bytes": settings.max_upload_bytes,
                           "max_duration_seconds": settings.max_duration_seconds,
                           "max_job_attempts": settings.max_job_attempts},
                "cleanup_defaults": {"minimum_note_seconds": settings.minimum_note_seconds,
                                     "merge_gap_seconds": settings.merge_gap_seconds}}

    @app.get("/api/projects")
    def list_projects():
        return db.list_projects(current_user_id())

    @app.post("/api/audio-preview")
    async def audio_preview(file: Annotated[UploadFile, File()]):
        # This endpoint never queues inference or creates a project. Decode in
        # the threadpool so CPU/file work doesn't block the API event loop.
        directory = Path(tempfile.mkdtemp(prefix="audio-preview-", dir=settings.data_dir))
        try:
            await save_audio(file, directory, settings)
            target = directory / "preview.wav"
            await run_in_threadpool(normalize_audio, directory / "source", target, settings)
        except BaseException as exc:
            shutil.rmtree(directory, ignore_errors=True)
            if isinstance(exc, AnalysisError):
                raise HTTPException(422, str(exc)) from exc
            raise
        return PreviewResponse(target, directory)

    @app.post("/api/projects", status_code=201)
    async def create_project(file: Annotated[UploadFile, File()],
                             title: Annotated[str, Form(max_length=160)] = "",
                             source: Annotated[Literal["upload", "microphone"], Form()] = "upload",
                             input_mode: Annotated[Literal["solo", "mixed"], Form()] = "solo",
                             clip_start_seconds: Annotated[float, Form()] = 0,
                             clip_end_seconds: Annotated[float | None, Form()] = None,
                             instrument_hint: Annotated[Literal["voice", "piano", "guitar", "bass", "violin", "flute", "other"] | None, Form()] = None,
                             tempo_hint: Annotated[int | None, Form(ge=30, le=300)] = None,
                             time_signature_hint: Annotated[Literal["2/4", "3/4", "4/4", "6/8", "9/8", "12/8"] | None, Form()] = None,
                             minimum_note_seconds: Annotated[float | None, Form()] = None,
                             merge_gap_seconds: Annotated[float | None, Form()] = None,
                             idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise HTTPException(422, "Submission key must contain 8–128 letters, digits, underscores or hyphens.")
        cleanup = {"minimum_note_seconds": settings.minimum_note_seconds if minimum_note_seconds is None else minimum_note_seconds,
                   "merge_gap_seconds": settings.merge_gap_seconds if merge_gap_seconds is None else merge_gap_seconds}
        for key, maximum in (("minimum_note_seconds", 1), ("merge_gap_seconds", 0.2)):
            if not math.isfinite(cleanup[key]) or not 0 <= cleanup[key] <= maximum:
                raise HTTPException(422, f"{key} must be a finite value between 0 and {maximum} seconds.")
        if not math.isfinite(clip_start_seconds) or not 0 <= clip_start_seconds < settings.max_duration_seconds:
            raise HTTPException(422, "Clip start must be a finite, non-negative time within the recording limit.")
        if clip_end_seconds is not None and (not math.isfinite(clip_end_seconds)
                or clip_end_seconds - clip_start_seconds < 0.1 - 1e-9
                or clip_end_seconds > settings.max_duration_seconds + 0.05):
            raise HTTPException(422, "Clip end must follow the start by at least 0.1 seconds and stay within the recording limit.")
        project_id = str(uuid.uuid4())
        directory = settings.data_dir / "projects" / project_id
        directory.mkdir(mode=0o700)
        try:
            filename, media_type = await save_audio(file, directory, settings)
            clean_title = title.strip() or Path(filename).stem or "Untitled recording"
            digest = hashlib.sha256()
            with (directory / "source").open("rb") as saved_audio:
                for chunk in iter(lambda: saved_audio.read(1024 * 1024), b""):
                    digest.update(chunk)
            submission = {"audio_sha256": digest.hexdigest(), "title": clean_title, "filename": filename,
                          "source": source, "input_mode": input_mode, "clip_start_seconds": clip_start_seconds,
                          "clip_end_seconds": clip_end_seconds, "instrument_hint": instrument_hint,
                          "tempo_hint": tempo_hint, "time_signature_hint": time_signature_hint, "cleanup": cleanup}
            fingerprint = hashlib.sha256(json.dumps(submission, sort_keys=True).encode()).hexdigest()
            reserve_duration = (clip_end_seconds or settings.max_duration_seconds) - clip_start_seconds
            reserved = db.storage_reservation(reserve_duration, input_mode == "mixed", (directory / "source").stat().st_size)
            result = db.create_project(project_id, clean_title, source, filename, media_type,
                                       clip_start_seconds=clip_start_seconds, clip_end_seconds=clip_end_seconds,
                                       instrument_hint=instrument_hint, tempo_hint=tempo_hint,
                                       time_signature_hint=time_signature_hint, cleanup=cleanup, input_mode=input_mode,
                                       owner_id=current_user_id(), reserved_bytes=reserved,
                                       idempotency_key=idempotency_key, fingerprint=fingerprint)
            if result["id"] != project_id:
                shutil.rmtree(directory, ignore_errors=True)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await file.close()
        return result

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return project_or_404(project_id)

    @app.delete("/api/projects/{project_id}", status_code=202)
    def delete_project(project_id: str):
        project_or_404(project_id)
        if not db.request_deletion(project_id, current_user_id()):
            raise HTTPException(404, "Project not found.")
        return {"id": project_id, "status": "deleting"}

    @app.post("/api/projects/{project_id}/retry")
    def retry_project(project_id: str):
        project_or_404(project_id)
        try:
            return db.retry(project_id)
        except QuotaError:
            raise
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/projects/{project_id}/cancel")
    def cancel_project(project_id: str):
        project_or_404(project_id)
        try:
            return db.cancel(project_id)
        except QuotaError:
            raise
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.put("/api/projects/{project_id}/notes")
    def save_notes(project_id: str, edit: NotesEdit):
        project = project_or_404(project_id)
        if project["status"] != "completed":
            raise HTTPException(409, "Notes can be edited only after transcription completes.")
        if project["notes_revision"] != edit.revision:
            raise HTTPException(409, "This project's notes changed in another request. Reload the latest notes before saving.")
        notes = normalize_edits(project, edit.notes, db.note_registry(project_id))
        edit_id = str(uuid.uuid4())
        directory = settings.data_dir / "projects" / project_id / "edits" / edit_id
        directory.mkdir(parents=True, mode=0o700)
        try:
            performance = [note for note in notes if note.get("role") != "vocal_cue"] if project.get("output_mode") == "accompaniment" else notes
            try:
                write_performance_midi(performance, directory / "score.mid")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            atomic_json(directory / "notes.json", {"schema_version": 2, "revision": edit.revision + 1,
                        "time_basis": "seconds relative to selected clip; unquantized", "notes": notes})
            db.check_storage_usage(project["owner_id"])
            accepted_project = db.save_notes(project_id, edit.revision, notes, edit_id, return_project=True)
            if not accepted_project:
                raise HTTPException(409, "Another edit was saved first. Reload the latest notes before saving.")
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return accepted_project

    @app.get("/api/projects/{project_id}/audio")
    def audio(project_id: str):
        p = project_or_404(project_id)
        return FileResponse(settings.data_dir / "projects" / project_id / "source",
                            media_type=db.media_type(project_id),
                            filename=p["original_filename"], content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/projects/{project_id}/analysis-audio")
    def analysis_audio(project_id: str):
        project_or_404(project_id)
        job_id = db.analysis_job_id(project_id)
        if not job_id:
            raise HTTPException(404, "The selected analysis clip is not ready yet.")
        path = settings.data_dir / "projects" / project_id / job_id / "normalized.wav"
        if not path.is_file():
            raise HTTPException(404, "The selected audio file is missing from local storage.")
        return FileResponse(path, media_type="audio/wav",
                            headers={"X-Content-Type-Options": "nosniff"})

    install_review_routes(app, db, settings, project_or_404)
    install_piano_routes(app, db, settings, project_or_404)
    install_mixed_routes(app, db, settings, project_or_404)
    canonical_export = install_score_routes(app, db, settings, project_or_404)

    @app.get("/api/projects/{project_id}/exports/{format}")
    def export(project_id: str, format: Literal["musicxml", "midi", "raw-midi"]):
        if format == "musicxml":
            return canonical_export(project_id, "musicxml")
        p = project_or_404(project_id)
        if format not in p["exports"] or p["status"] != "completed":
            raise HTTPException(404, "This export is not available yet.")
        storage = db.storage(project_id)
        job_id = storage["result_job_id"] or next(j["id"] for j in p["jobs"] if j["status"] == "completed")
        suffix, media_type = ("musicxml", "application/vnd.recordare.musicxml+xml") if format == "musicxml" else ("mid", "audio/midi")
        directory = settings.data_dir / "projects" / project_id
        if format == "raw-midi":
            path = directory / job_id / "raw-performance.mid"
        elif storage["edit_id"] and format == "midi":
            path = directory / "edits" / storage["edit_id"] / "score.mid"
        else:
            path = directory / job_id / f"score.{suffix}"
        if not path.is_file():
            raise HTTPException(404, "The export file is missing. Check local project storage.")
        return FileResponse(path, media_type=media_type, filename=performance_filename(
            p["title"], raw=format == "raw-midi", revision=p["notes_revision"], job_id=job_id),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/projects/{project_id}/artifacts/{artifact}")
    def artifact(project_id: str, artifact: str):
        project = project_or_404(project_id)
        if artifact not in ARTIFACTS or artifact not in project["artifacts"]:
            raise HTTPException(404, "This raw or normalized artifact is unavailable for this project.")
        storage = db.storage(project_id)
        filename, media_type = ARTIFACTS[artifact]
        directory = settings.data_dir / "projects" / project_id / storage["result_job_id"]
        if artifact == "normalized-notes" and storage["edit_id"]:
            directory = settings.data_dir / "projects" / project_id / "edits" / storage["edit_id"]
        path = directory / filename
        if not path.is_file():
            raise HTTPException(404, "The artifact file is missing from local storage.")
        return FileResponse(path, media_type=media_type, filename=filename,
                            headers={"X-Content-Type-Options": "nosniff"})

    return app


app = create_app()

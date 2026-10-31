"""Owner-scoped resumable transfers on private, single-host POSIX storage.

Only verified, contiguous chunks advance the durable offset. A crashed request
can leave extra bytes on disk, but the next request truncates them to the last
committed offset. Finalization moves the original without copying 500 MB and
publishes one project/job transaction. It never performs model inference.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .audio import AnalysisError, SUPPORTED_EXTENSIONS, normalize_audio, sniff_audio
from .auth import current_user_id
from .database import IdempotencyConflict, QuotaError
from .engine import atomic_json


class UploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(default="", max_length=160)
    source: Literal["upload", "microphone"] = "upload"
    input_mode: Literal["solo", "mixed"] = "solo"
    clip_start_seconds: float = Field(default=0, ge=0)
    clip_end_seconds: float | None = None
    instrument_hint: Literal["voice", "piano", "guitar", "bass", "violin", "flute", "other"] | None = None
    tempo_hint: int | None = Field(default=None, ge=30, le=300)
    time_signature_hint: Literal["2/4", "3/4", "4/4", "6/8", "9/8", "12/8"] | None = None
    minimum_note_seconds: float | None = Field(default=None, ge=0, le=1)
    merge_gap_seconds: float | None = Field(default=None, ge=0, le=0.2)


class UploadCreate(UploadMetadata):
    filename: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0, strict=True)


def _root(settings) -> Path:
    directory = settings.data_dir / "uploads"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


@contextmanager
def _lease(settings, identifier: str, *, shared=False):
    import fcntl
    directory = settings.data_dir / ".upload-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / f"{identifier}.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(503, "This upload is busy. Retry from its saved offset shortly.", headers={"Retry-After": "1"}) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _record(db, identifier: str, *, active=True) -> dict:
    try:
        uuid.UUID(identifier)
    except ValueError as exc:
        raise HTTPException(404, "Upload not found.") from exc
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM upload_sessions WHERE id=? AND owner_id=?", (identifier, current_user_id())).fetchone()
    if row is None:
        raise HTTPException(404, "Upload not found.")
    row = dict(row)
    if active and (row["status"] in {"canceled", "expired"} or row["expires_at"] < time.time()):
        raise HTTPException(410, "This upload expired or was discarded. Start a new upload.")
    return row


def _view(settings, db, row: dict) -> dict:
    with db.connect() as connection:
        chunks = [dict(chunk) for chunk in connection.execute(
            "SELECT offset,size_bytes,sha256 FROM upload_chunks WHERE upload_id=? ORDER BY offset", (row["id"],))]
    return {"id": row["id"], "status": row["status"], "offset": row["received_bytes"],
            "size_bytes": row["size_bytes"], "chunk_size": settings.upload_chunk_bytes,
            "expires_at": row["expires_at"], "project_id": row["project_id"] if row["status"] == "completed" else None,
            "sha256": row["sha256"], "chunks": chunks,
            "preview_url": f'/api/uploads/{row["id"]}/preview' if row["status"] == "uploading" and row["received_bytes"] == row["size_bytes"] else None}


def _metadata(value: UploadMetadata, settings) -> dict:
    result = value.model_dump()
    start, end = result["clip_start_seconds"], result["clip_end_seconds"]
    if start >= settings.max_duration_seconds:
        raise HTTPException(422, "Clip start must be within the configured recording duration limit.")
    if end is not None and (not math.isfinite(end) or end - start < 0.1 - 1e-9 or end > settings.max_duration_seconds + 0.05):
        raise HTTPException(422, "Clip end must follow the start by at least 0.1 seconds and stay within the recording limit.")
    result["minimum_note_seconds"] = settings.minimum_note_seconds if value.minimum_note_seconds is None else value.minimum_note_seconds
    result["merge_gap_seconds"] = settings.merge_gap_seconds if value.merge_gap_seconds is None else value.merge_gap_seconds
    return result


def cleanup_uploads(settings, db):
    """Worker/API sweeper; interrupted streams keep leases until they release.

    Completed original recordings are project-owned and never deleted here.
    Canceled/expired upload markers remain for seven days to prevent accidental
    resurrection from a delayed client replay, without retaining private data.
    """
    clock = time.time()
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT id,project_id,status,expires_at FROM upload_sessions WHERE expires_at<? OR status IN ('canceled','expired')", (clock,))]
    for row in rows:
        try:
            with _lease(settings, row["id"]):
                directory = _root(settings) / row["id"]
                shutil.rmtree(directory, ignore_errors=False) if directory.exists() else None
                with db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    # A crash after moving source but before project admission
                    # can leave an uncommitted, generated project directory.
                    project = connection.execute("SELECT 1 FROM projects WHERE id=?", (row["project_id"],)).fetchone()
                    if project is None:
                        orphan = settings.data_dir / "projects" / row["project_id"]
                        if orphan.exists():
                            shutil.rmtree(orphan)
                    connection.execute("DELETE FROM upload_chunks WHERE upload_id=?", (row["id"],))
                    if row["expires_at"] < clock - 7 * 86400:
                        connection.execute("DELETE FROM upload_sessions WHERE id=?", (row["id"],))
                    else:
                        connection.execute("UPDATE upload_sessions SET status='expired',metadata_json='{}',fingerprint='',sha256=NULL WHERE id=?", (row["id"],))
        except (HTTPException, OSError):
            # Busy/failed cleanup remains durable for the next sweep.
            continue


class UploadPreviewResponse(FileResponse):
    def __init__(self, path: Path, settings, identifier):
        self.settings, self.identifier = settings, identifier
        super().__init__(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def __call__(self, scope, receive, send):
        # Native audio can use ranged requests. Keep the preview alive until
        # each response finishes, including browser disconnects.
        with _lease(self.settings, self.identifier, shared=True):
            if not Path(self.path).is_file():
                raise HTTPException(410, "This upload preview expired or was discarded.")
            await super().__call__(scope, receive, send)


def install_upload_routes(app, db, settings):
    @app.post("/api/uploads", status_code=201)
    def create_upload(value: UploadCreate, idempotency_key: Annotated[str, Header(alias="Idempotency-Key")]):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise HTTPException(422, "Submission key must contain 8–128 letters, digits, underscores or hyphens.")
        if value.size_bytes > settings.max_upload_bytes:
            raise HTTPException(413, f"Audio file exceeds the {settings.max_upload_bytes}-byte upload limit.")
        filename = Path(value.filename.replace("\\", "/")).name[:255]
        if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, "Choose WAV, MP3, M4A, MP4, FLAC, OGG, or WebM audio.")
        metadata = _metadata(UploadMetadata(**value.model_dump(exclude={"filename", "size_bytes"})), settings)
        metadata.update(filename=filename, size_bytes=value.size_bytes)
        fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        cleanup_uploads(settings, db)
        identifier, project_id, clock, owner = str(uuid.uuid4()), str(uuid.uuid4()), time.time(), current_user_id()
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM upload_sessions WHERE owner_id=? AND submission_key=?", (owner, idempotency_key)).fetchone()
            if existing:
                if existing["status"] in {"canceled", "expired"} or existing["expires_at"] < clock:
                    raise HTTPException(410, "This upload key expired or was discarded. Start a new upload with a new key.")
                if existing["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("This upload key was already used with different file details or settings.")
                row = dict(existing)
            else:
                pending = connection.execute("SELECT COUNT(*) FROM upload_sessions WHERE owner_id=? AND status='uploading'", (owner,)).fetchone()[0]
                if pending >= settings.max_active_uploads_per_user:
                    raise QuotaError("Unfinished upload quota reached. Resume or discard an existing upload before starting another.")
                # A browser-incompatible source can also need one full mono
                # WAV preview. Reserve its maximum controlled decoded size.
                reserved = value.size_bytes + (settings.max_duration_seconds + 1) * 22050 * 2 + 65536
                db._check_project_quota(connection, owner, reserved)
                connection.execute("""INSERT INTO upload_sessions(id,owner_id,submission_key,metadata_json,fingerprint,size_bytes,reserved_bytes,created_at,updated_at,expires_at,project_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (identifier, owner, idempotency_key, json.dumps(metadata), fingerprint,
                    value.size_bytes, reserved, clock, clock, clock + settings.upload_ttl_seconds, project_id))
                row = dict(connection.execute("SELECT * FROM upload_sessions WHERE id=?", (identifier,)).fetchone())
        return _view(settings, db, row)

    @app.get("/api/uploads/{upload_id}")
    def get_upload(upload_id: str):
        return _view(settings, db, _record(db, upload_id))

    @app.put("/api/uploads/{upload_id}/chunks/{offset}")
    async def upload_chunk(upload_id: str, offset: int, request: Request,
                           x_chunk_sha256: Annotated[str, Header(alias="X-Chunk-SHA256")]):
        row = _record(db, upload_id)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", x_chunk_sha256):
            raise HTTPException(422, "A SHA-256 digest is required for each upload chunk.")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/octet-stream":
            raise HTTPException(415, "Upload chunks must use application/octet-stream.")
        digest_expected = x_chunk_sha256.lower()
        with _lease(settings, upload_id):
            row = _record(db, upload_id)
            if row["status"] != "uploading":
                raise HTTPException(409, "This upload has already been submitted.")
            with db.connect() as connection:
                duplicate = connection.execute("SELECT * FROM upload_chunks WHERE upload_id=? AND offset=?", (upload_id, offset)).fetchone()
            if duplicate and duplicate["sha256"] != digest_expected:
                raise HTTPException(409, "The repeated chunk does not match the saved upload. Reselect the same file or start a new upload.")
            if not duplicate and (offset != row["received_bytes"] or offset >= row["size_bytes"]):
                raise HTTPException(409, f"Resume this upload at byte {row['received_bytes']}.")
            directory = _root(settings) / upload_id
            directory.mkdir(mode=0o700, exist_ok=True)
            part = directory / "source.part"
            # Duplicate chunks are still hashed so a forged digest header cannot
            # acknowledge different bytes after a lost response.
            destination = None
            if not duplicate:
                destination = part.open("r+b" if part.exists() else "w+b")
                destination.truncate(row["received_bytes"])
                destination.seek(offset)
            received, digest = 0, hashlib.sha256()
            try:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > settings.upload_chunk_bytes or offset + received > row["size_bytes"]:
                        raise HTTPException(413, "Chunk exceeds its bounded size or the declared file size.")
                    digest.update(chunk)
                    if destination:
                        destination.write(chunk)
                if not received:
                    raise HTTPException(422, "The upload chunk is empty.")
                if digest.hexdigest() != digest_expected:
                    raise HTTPException(422, "Chunk integrity check failed. Retry this chunk from its saved offset.")
                if duplicate:
                    if duplicate["size_bytes"] != received:
                        raise HTTPException(409, "Repeated chunk length differs from the saved upload.")
                else:
                    destination.flush()
                    os.fsync(destination.fileno())
                    if offset == 0:
                        with part.open("rb") as source:
                            if not sniff_audio(source.read(64)):
                                raise HTTPException(415, "The file does not contain a recognized audio container.")
                    if received != min(settings.upload_chunk_bytes, row["size_bytes"] - offset):
                        raise HTTPException(422, "Send one complete configured chunk; only the final chunk may be shorter.")
                    with db.connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute("INSERT INTO upload_chunks VALUES(?,?,?,?)", (upload_id, offset, received, digest_expected))
                        clock = time.time()
                        connection.execute("UPDATE upload_sessions SET received_bytes=?,updated_at=?,expires_at=? WHERE id=?", (offset + received, clock, clock + settings.upload_ttl_seconds, upload_id))
            except BaseException:
                if destination:
                    destination.truncate(row["received_bytes"])
                raise
            finally:
                if destination:
                    destination.close()
        return _view(settings, db, _record(db, upload_id))

    @app.get("/api/uploads/{upload_id}/preview")
    def upload_preview(upload_id: str):
        row = _record(db, upload_id)
        with _lease(settings, upload_id):
            row = _record(db, upload_id)
            if row["received_bytes"] != row["size_bytes"]:
                raise HTTPException(409, "Finish uploading the audio before preparing its preview.")
            if row["status"] == "completed":
                raise HTTPException(409, "This upload was submitted. Use its project audio player now.")
            source = _root(settings) / upload_id / "source.part"
            if not source.exists():
                # Recover a crash between moving the source and publishing the
                # project; the session still owns this generated directory.
                source = settings.data_dir / "projects" / row["project_id"] / "source"
            preview = _root(settings) / upload_id / "preview.wav"
            if not preview.is_file():
                pending = preview.with_name("preview.pending.wav")
                pending.unlink(missing_ok=True)
                try:
                    normalize_audio(source, pending, settings)
                    os.replace(pending, preview)
                except AnalysisError as exc:
                    raise HTTPException(422, str(exc)) from exc
        return UploadPreviewResponse(preview, settings, upload_id)

    @app.post("/api/uploads/{upload_id}/complete", status_code=201)
    def complete_upload(upload_id: str, options: UploadMetadata | None = Body(default=None)):
        row = _record(db, upload_id)
        with _lease(settings, upload_id):
            row = _record(db, upload_id)
            stored = json.loads(row["metadata_json"])
            initial = {key: value for key, value in stored.items() if key not in {"filename", "size_bytes"}}
            values = _metadata(UploadMetadata(**{**initial, **options.model_dump(exclude_unset=True)}), settings) if options is not None else initial
            metadata = {**values, "filename": stored["filename"], "size_bytes": row["size_bytes"]}
            if row["status"] == "completed":
                if metadata != stored:
                    raise IdempotencyConflict("This upload was submitted with different clip or musical settings. Open its saved project or start a new upload.")
                project = db.get_project(row["project_id"])
                if not project or project["owner_id"] != current_user_id():
                    raise HTTPException(410, "The submitted project was deleted. Start a new upload.")
                return project
            if row["received_bytes"] != row["size_bytes"]:
                raise HTTPException(409, f"Upload is incomplete. Resume at byte {row['received_bytes']}.")
            directory = _root(settings) / upload_id
            project_directory = settings.data_dir / "projects" / row["project_id"]
            source = directory / "source.part"
            if not source.exists():
                source = project_directory / "source"
            if not source.is_file() or source.stat().st_size != row["size_bytes"]:
                raise HTTPException(409, "The saved upload length does not match its verified chunks. Start a new upload.")
            digest = hashlib.sha256()
            with db.connect() as connection:
                chunks = [dict(chunk) for chunk in connection.execute("SELECT offset,size_bytes,sha256 FROM upload_chunks WHERE upload_id=? ORDER BY offset", (upload_id,))]
            with source.open("rb") as audio:
                media_type = sniff_audio(audio.read(64))
                if not media_type:
                    raise HTTPException(415, "The file does not contain a recognized audio container.")
                audio.seek(0)
                position = 0
                for chunk in chunks:
                    if chunk["offset"] != position:
                        raise HTTPException(409, "The verified upload has a gap. Start a new upload.")
                    remaining, piece = chunk["size_bytes"], hashlib.sha256()
                    while remaining:
                        data = audio.read(min(65536, remaining))
                        if not data:
                            raise HTTPException(409, "The saved upload ended unexpectedly. Start a new upload.")
                        piece.update(data); digest.update(data)
                        remaining -= len(data); position += len(data)
                    if piece.hexdigest() != chunk["sha256"]:
                        raise HTTPException(422, "Saved upload integrity check failed. Start a new upload; no project was submitted.")
                if position != row["size_bytes"]:
                    raise HTTPException(409, "The verified chunks do not cover the complete file.")
            cleanup = {key: values[key] for key in ("minimum_note_seconds", "merge_gap_seconds")}
            title = values["title"].strip() or Path(stored["filename"]).stem or "Untitled recording"
            fingerprint = hashlib.sha256(json.dumps({**metadata, "sha256": digest.hexdigest()}, sort_keys=True).encode()).hexdigest()
            reserve_duration = (values["clip_end_seconds"] or settings.max_duration_seconds) - values["clip_start_seconds"]
            reserved = db.storage_reservation(reserve_duration, values["input_mode"] == "mixed", row["size_bytes"])
            # Persist intent before the atomic move. A retry after an API crash
            # can find either source location without receiving any bytes again.
            with db.connect() as connection:
                connection.execute("UPDATE upload_sessions SET metadata_json=?,sha256=? WHERE id=?", (json.dumps(metadata), digest.hexdigest(), upload_id))
            project_directory.mkdir(mode=0o700, exist_ok=True)
            target = project_directory / "source"
            if source != target:
                os.replace(source, target)
            atomic_json(project_directory / "upload-manifest.json", {"schema_version": 1, "size_bytes": row["size_bytes"],
                        "sha256": digest.hexdigest(), "chunk_size": settings.upload_chunk_bytes, "chunks": chunks})
            try:
                result = db.create_project(row["project_id"], title, values["source"], stored["filename"], media_type,
                    clip_start_seconds=values["clip_start_seconds"], clip_end_seconds=values["clip_end_seconds"],
                    instrument_hint=values["instrument_hint"], tempo_hint=values["tempo_hint"],
                    time_signature_hint=values["time_signature_hint"], cleanup=cleanup, input_mode=values["input_mode"],
                    owner_id=current_user_id(), reserved_bytes=reserved, idempotency_key="resumable-" + upload_id,
                    fingerprint=fingerprint, upload_id=upload_id)
            except BaseException:
                if target.exists() and not db.get_project(row["project_id"]):
                    os.replace(target, directory / "source.part")
                    shutil.rmtree(project_directory)
                raise
            shutil.rmtree(directory, ignore_errors=True)
            return result

    @app.delete("/api/uploads/{upload_id}", status_code=204)
    def discard_upload(upload_id: str):
        row = _record(db, upload_id, active=False)
        with _lease(settings, upload_id):
            row = _record(db, upload_id, active=False)
            if row["status"] == "completed":
                raise HTTPException(409, "This upload is already a project. Delete the project to remove its recording.")
            with db.connect() as connection:
                connection.execute("UPDATE upload_sessions SET status='canceled',updated_at=? WHERE id=?", (time.time(), upload_id))
        cleanup_uploads(settings, db)
        return None

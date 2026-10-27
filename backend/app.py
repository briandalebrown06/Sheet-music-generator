import shutil
import uuid
from pathlib import Path
from tempfile import SpooledTemporaryFile

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.config import Settings
from backend.db import Store

FORMATS = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".webm": "audio/webm", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}


class UploadLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        # Bound multipart data before parsing, including requests without Content-Length.
        with SpooledTemporaryFile(max_size=1024 * 1024) as body:
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_bytes:
                    response = JSONResponse({"detail": "Upload is too large."}, status_code=413)
                    return await response(scope, receive, send)
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            replayed = False

            async def replay():
                nonlocal replayed
                if replayed:
                    return await receive()
                chunk = body.read(65536)
                more = body.tell() < size
                replayed = not more
                return {"type": "http.request", "body": chunk, "more_body": more}

            await self.app(scope, replay, send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    app = FastAPI(title="Score Studio", docs_url=None, redoc_url=None)
    app.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_mb * 1024 * 1024 + 65536)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.client and request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"detail": "This service is local-only."}, status_code=403)
        origin = request.headers.get("origin")
        if (origin and origin not in {
            settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8000", "http://127.0.0.1:8000",
        }) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Untrusted browser origin."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def get_project(project_id: str):
        project = store.project(project_id)
        if project is None:
            raise HTTPException(404, "Project not found.")
        return project

    @app.get("/api/health")
    def health():
        return {
            "status": "ok", "mode": "local-single-user",
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "engine": settings.engine,
        }

    @app.get("/api/projects")
    def projects():
        with store.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT projects.*, (SELECT status FROM jobs WHERE project_id=projects.id
                ORDER BY created_at DESC, rowid DESC LIMIT 1) AS status
                FROM projects ORDER BY created_at DESC, rowid DESC
            """)]

    @app.post("/api/projects", status_code=201)
    async def upload(audio: UploadFile = File(...)):
        filename = Path((audio.filename or "recording.webm").replace("\\", "/")).name
        extension = Path(filename).suffix.lower()
        if extension not in FORMATS:
            raise HTTPException(415, "Choose WAV, MP3, FLAC, OGG, WebM, M4A, or MP4 audio.")
        project_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
        directory = settings.data_dir / project_id
        directory.mkdir(mode=0o700)
        try:
            size = 0
            with (directory / f"original{extension}").open("wb") as target:
                while chunk := await audio.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise HTTPException(413, "Upload is too large.")
                    target.write(chunk)
            if not size:
                raise HTTPException(422, "The audio file is empty.")
            with store.connect() as db:
                db.execute("INSERT INTO projects(id,title,filename,extension) VALUES(?,?,?,?)",
                           (project_id, Path(filename).stem[:120], filename[:255], extension))
                db.execute("INSERT INTO jobs(id,project_id,status) VALUES(?,?,'queued')",
                           (job_id, project_id))
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            await audio.close()
        return get_project(project_id)

    @app.get("/api/projects/{project_id}")
    def detail(project_id: str):
        return get_project(project_id)

    @app.get("/api/projects/{project_id}/audio")
    def audio(project_id: str):
        project = get_project(project_id)
        return FileResponse(
            settings.data_dir / project["id"] / f"original{project['extension']}",
            media_type=FORMATS[project["extension"]],
        )

    @app.post("/api/projects/{project_id}/retry", status_code=202)
    def retry(project_id: str):
        get_project(project_id)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT status FROM jobs WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if latest["status"] != "failed":
                raise HTTPException(409, "Only failed jobs can be retried.")
            db.execute("INSERT INTO jobs(id,project_id,status) VALUES(?,?,'queued')",
                       (uuid.uuid4().hex, project_id))
        return get_project(project_id)

    return app

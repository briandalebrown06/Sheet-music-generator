"""Audition actual separated categories and create persistent selected-part jobs."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .audio import AnalysisError
from .database import QuotaError
from .mixed_storage import check_storage

PROFILES = {
    "vocals": ("Vocals", "Vocal melody estimated with monophonic pitch tracking and vibrato-aware cleanup. Backing vocals and leakage can still confuse it."),
    "bass": ("Bass", "Bass-line transcription with monophonic pitch tracking. Review octaves, note boundaries and separation leakage."),
    "other": ("Other", "Experimental pitched-note transcription of the model's combined other category. This is not an isolated guitar or piano."),
}


def stem_path(settings, project_id: str, job_id: str, filename: str) -> Path:
    root = (settings.data_dir / "projects" / project_id / job_id).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".wav" or not path.is_file():
        raise HTTPException(404, "The selected stem file is unavailable in local storage.")
    return path


def install_mixed_routes(app, db, settings, project_or_404):
    def separated(project_id):
        project = project_or_404(project_id)
        if project["input_mode"] != "mixed":
            raise HTTPException(409, "This is a solo recording. Upload in full-song mode to separate stems.")
        return project, db.separation(project_id)

    @app.get("/api/projects/{project_id}/stems")
    def list_stems(project_id: str):
        project, result = separated(project_id)
        children = db.list_children(project_id)
        child_ids = {child["stem_category"]: child["id"] for child in children}
        stems = []
        for stem in result["stems"]:
            category = stem["category"]
            supported = category in PROFILES
            label, detail = PROFILES.get(category, (category.replace("_", " ").title(),
                "Audition only. Drum/percussion notation is unsupported; this stem is never sent to a pitched-note model."))
            stems.append({"id": category, "category": category, "label": label, "detail": detail,
                          "duration_seconds": stem["duration_seconds"], "sample_rate": stem["sample_rate"],
                          "channels": stem["channels"], "source_origin_seconds": project["clip_start_seconds"],
                          "audio_url": f"/api/projects/{project_id}/stems/{category}/audio",
                          "transcription_supported": supported, "profile": category if supported else None,
                          "child_project_id": child_ids.get(category)})
        return {"stems": stems, "manifest": result["manifest"], "children": children}

    @app.get("/api/projects/{project_id}/stems/{category}/audio")
    def stem_audio(project_id: str, category: str):
        _, result = separated(project_id)
        stem = next((item for item in result["stems"] if item["category"] == category), None)
        if stem is None or not result["job_id"]:
            raise HTTPException(404, "This stem category is unavailable.")
        return FileResponse(stem_path(settings, project_id, result["job_id"], stem["filename"]),
                            media_type="audio/wav", filename=f"{category}.wav", content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @app.get("/api/projects/{project_id}/separation-manifest")
    def separation_manifest(project_id: str):
        _, result = separated(project_id)
        if result["manifest"] is None:
            raise HTTPException(404, "Complete separation before downloading its processing manifest.")
        return JSONResponse(result["manifest"], headers={"Content-Disposition": 'attachment; filename="separation-manifest.json"',
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/projects/{project_id}/stems/{category}/transcribe")
    def transcribe_stem(project_id: str, category: str):
        project, _ = separated(project_id)
        if category not in PROFILES:
            raise HTTPException(422, "Drum/percussion notation is unsupported. Choose vocals, bass, or experimental other; no drum notes will be fabricated.")
        child_id = str(uuid.uuid4())
        directory = settings.data_dir / "projects" / child_id
        directory.mkdir(mode=0o700)
        def prepare_source(stem, job_id):
            source = stem_path(settings, project_id, job_id, stem["filename"])
            check_storage(settings, db, project, source.stat().st_size)
            if shutil.disk_usage(settings.data_dir).free < source.stat().st_size + 16 * 1024 * 1024:
                raise OSError("Insufficient free disk space for the selected stem copy.")
            shutil.copyfile(source, directory / "source")
        try:
            child, created = db.create_stem_child(project_id, category, child_id, prepare_source)
        except AnalysisError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(413, str(exc)) from exc
        except QuotaError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        except ValueError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(404, "This stem category is unavailable.") from exc
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(507, "Local storage could not copy this stem. Check available disk space and project-directory permissions, then try again.") from exc
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        if not created:
            shutil.rmtree(directory, ignore_errors=True)
        return JSONResponse(child, status_code=201 if created else 200)

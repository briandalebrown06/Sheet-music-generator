"""Non-destructive notation preview and revisioned canonical score storage."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .score import ScoreSettings, build_score, default_score_settings, score_to_musicxml, write_score_midi
from .pdf_export import PdfExportError, engrave_pdf
from .downloads import attachment_header, score_filename

logger = logging.getLogger(__name__)


class ScorePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes_revision: int = Field(ge=0)
    settings: ScoreSettings


class ScoreSave(ScorePreview):
    revision: int = Field(ge=0)


def install_score_routes(app, db, settings, project_or_404):
    def view(project: dict, saved: dict | None, include_xml: bool = False) -> dict:
        result = {
            "score": saved["score"] if saved else None,
            "revision": saved["revision"] if saved else 0,
            "source_notes_revision": saved["source_notes_revision"] if saved else None,
            "stale": bool(saved and saved["source_notes_revision"] != project["notes_revision"]),
            "defaults": default_score_settings(project),
        }
        if include_xml and saved:
            result["musicxml"] = saved["musicxml"]
        return result

    def construct(project: dict, request: ScorePreview) -> tuple[dict, str]:
        if project["status"] != "completed" or project["notes"] is None:
            raise HTTPException(409, "Complete transcription before constructing musical notation.")
        if request.notes_revision != project["notes_revision"]:
            raise HTTPException(409, "Performance notes changed. Reload the project before previewing or saving notation.")
        if len(project["notes"]) > 10000:
            raise HTTPException(422, "This score has more than 10,000 detected notes. Use a shorter clip before constructing notation.")
        duration = project["clip_duration_seconds"] or project["duration_seconds"]
        try:
            model = build_score(project["notes"], request.settings, title=project["title"],
                                clip_duration_seconds=duration, source_notes_revision=project["notes_revision"])
            from .piano_api import attach_playability
            attach_playability(project, model, project["notes"])
            xml = score_to_musicxml(model)
        except ImportError as exc:
            raise HTTPException(503, "Notation requires music21 in the API environment. Run backend/install_analysis.py with Python 3.11, then restart the API.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            logger.exception("Notation construction failed for project %s", project["id"])
            raise HTTPException(422, "These settings could not produce valid notation. Try a coarser grid or simpler alignment; see the API log for details.") from exc
        return model, xml

    def current_saved(project_id: str) -> dict:
        project = project_or_404(project_id)
        saved = db.get_score(project_id)
        if project["status"] != "completed" or saved is None:
            raise HTTPException(404, "Generate and save a score before downloading notation.")
        if saved["source_notes_revision"] != project["notes_revision"]:
            raise HTTPException(409, "This score predates the latest performance-note edits. Preview and save notation again before exporting.")
        return saved

    @app.get("/api/projects/{project_id}/score")
    def get_score(project_id: str):
        project = project_or_404(project_id)
        return view(project, db.get_score(project_id))

    @app.post("/api/projects/{project_id}/score/preview")
    def preview_score(project_id: str, request: ScorePreview):
        project = project_or_404(project_id)
        model, xml = construct(project, request)
        # Preview has no database or project-artifact writes.
        return {"score": model, "musicxml": xml, "source_notes_revision": project["notes_revision"]}

    @app.put("/api/projects/{project_id}/score")
    def save_score(project_id: str, request: ScoreSave):
        project = project_or_404(project_id)
        saved = db.get_score(project_id)
        if (saved["revision"] if saved else 0) != request.revision:
            raise HTTPException(409, "A newer score was saved elsewhere. Reload its settings before saving.")
        model, xml = construct(project, request)
        if not db.save_score(project_id, request.notes_revision, request.revision, model, xml):
            raise HTTPException(409, "The notes or score changed while notation was being built. Reload before saving.")
        # Return this exact accepted revision, even if another request follows it.
        return view(project, {"score": model, "musicxml": xml, "revision": request.revision + 1,
                              "source_notes_revision": request.notes_revision}, include_xml=True)

    @app.get("/api/projects/{project_id}/score/musicxml")
    def saved_musicxml(project_id: str):
        saved = current_saved(project_id)
        return Response(saved["musicxml"], media_type="application/vnd.recordare.musicxml+xml",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/projects/{project_id}/score/exports/{format}")
    def export_score(project_id: str, format: Literal["musicxml", "midi", "json", "pdf"], revision: int | None = None):
        saved = current_saved(project_id)
        if revision is not None and revision < 0:
            raise HTTPException(422, "Score revision must be non-negative.")
        if revision is not None and revision != saved["revision"]:
            raise HTTPException(409, "The saved score changed. Reload before downloading this revision.")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "X-Score-Revision": str(saved["revision"]),
                   "X-Notes-Revision": str(saved["source_notes_revision"]),
                   "Content-Disposition": attachment_header(score_filename(saved, format))}

        def verify_revision():
            latest = current_saved(project_id)
            if (latest["revision"], latest["source_notes_revision"]) != (saved["revision"], saved["source_notes_revision"]):
                raise HTTPException(409, "The saved score changed during export. Download the latest revision again.")

        if format == "json":
            return JSONResponse(saved["score"], headers=headers)
        if format == "musicxml":
            return Response(saved["musicxml"], media_type="application/vnd.recordare.musicxml+xml",
                            headers=headers)
        if format == "pdf":
            try:
                content = engrave_pdf(saved["musicxml"], settings, saved["revision"])
            except PdfExportError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            verify_revision()
            return Response(content, media_type="application/pdf",
                            headers=headers)
        try:
            # This export is derived only from the saved rational score model.
            with tempfile.TemporaryDirectory(prefix="score-midi-", dir=settings.data_dir) as temporary:
                path = Path(temporary) / "score.mid"
                write_score_midi(saved["score"], path)
                content = path.read_bytes()
        except ImportError as exc:
            raise HTTPException(503, "Notation MIDI requires music21 in the API environment.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            logger.exception("Canonical MIDI export failed for project %s", project_id)
            raise HTTPException(422, "The saved score could not be exported as MIDI. See the API log for details.") from exc
        verify_revision()
        return Response(content, media_type="audio/midi", headers=headers)

    return export_score

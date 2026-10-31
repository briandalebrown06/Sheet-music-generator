"""Storage accounting and cleanup for isolated mixed-recording job attempts."""
from __future__ import annotations

import shutil
from pathlib import Path

from .audio import AnalysisError


def directory_bytes(path: Path) -> int:
    total = 0
    if path.is_dir():
        for file in path.rglob("*"):
            try:
                if file.is_file() and not file.is_symlink():
                    total += file.stat().st_size
            except FileNotFoundError:
                pass  # Another completed cleanup may remove a transient file.
    return total


def family_bytes(settings, db, project: dict) -> int:
    parent_id = project.get("parent_project_id") or project["id"]
    identifiers = [parent_id, *(child["id"] for child in db.list_children(parent_id))]
    return sum(directory_bytes(settings.data_dir / "projects" / identifier) for identifier in identifiers)


def check_storage(settings, db, project: dict, additional_bytes: int = 0) -> None:
    if family_bytes(settings, db, project) + additional_bytes > settings.max_separation_storage_bytes:
        raise AnalysisError("This mixed project exceeds MAX_SEPARATION_STORAGE_MB, including stems and selected-part analysis. Use a shorter clip or raise the local storage limit before retrying.")


def cleanup_discarded_attempts(settings, db) -> None:
    # Never remove originals, saved edits, published stems, or completed results.
    # Canceled/failed attempt IDs are server-generated UUIDs with isolated paths.
    from .project_lifecycle import artifact_lease
    for job in db.discarded_mixed_attempts():
        directory = settings.data_dir / "projects" / job["project_id"] / job["id"]
        if not directory.exists():
            db.clear_discarded_analysis(job["id"])
            continue
        try:
            with artifact_lease(settings, job["project_id"], exclusive=True, blocking=False):
                shutil.rmtree(directory, ignore_errors=True)
                db.clear_discarded_analysis(job["id"])
        except BlockingIOError:
            continue  # An old canceled process or a streamed response still owns these files.

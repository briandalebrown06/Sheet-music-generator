"""Private, content-addressed inference checkpoints shared across job attempts.

Checkpoints contain real model outputs only. They are never published as a
complete result; the current leased attempt must assemble and validate every
window before publication. Atomic files make interrupted writes disposable.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
import zlib
from pathlib import Path


def audio_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


class CheckpointStore:
    def __init__(self, root: Path, audio: Path, identity: dict):
        self.audio_sha256 = audio_sha256(audio)
        self.identity = {"schema_version": 1, "audio_sha256": self.audio_sha256, **identity}
        key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.directory = root / key
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.hits = 0

    def load(self, index: int, shapes: dict):
        import numpy as np
        path = self.directory / f"window-{index:07d}.npz"
        if not path.is_file():
            return None
        try:
            with np.load(path, allow_pickle=False) as archive:
                values = {name: archive[name] for name in shapes}
            if any(value.shape != shapes[name] or value.dtype.kind not in "fbiu"
                   for name, value in values.items()):
                return None
            self.hits += 1
            return values
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, zlib.error):
            return None

    def save(self, index: int, values: dict) -> None:
        import numpy as np
        path = self.directory / f"window-{index:07d}.npz"
        temporary = self.directory / f".window-{index:07d}-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as stream:
                np.savez_compressed(stream, **values)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.utime(self.directory, None)
        finally:
            temporary.unlink(missing_ok=True)


def cleanup_analysis_checkpoints(settings, db) -> None:
    """Reclaim unpublished work after cancel/completion or 24h failed retention."""
    from .project_lifecycle import artifact_lease
    for project in db.list_projects():
        root = settings.data_dir / "projects" / project["id"] / ".analysis-checkpoints"
        if project["status"] in {"queued", "processing"}:
            continue
        try:
            with artifact_lease(settings, project["id"], exclusive=True, blocking=False):
                # Disk-backed scratch arrays are reproducible from checkpoint
                # windows; raw-model-output.npz remains the preserved artifact.
                directory = settings.data_dir / "projects" / project["id"]
                for work in directory.glob("*/.activation-work"):
                    shutil.rmtree(work, ignore_errors=True)
                if root.is_dir():
                    expired = time.time() - max((p.stat().st_mtime for p in root.iterdir()), default=root.stat().st_mtime) > 86400
                    if project["status"] in {"completed", "canceled"} or expired:
                        shutil.rmtree(root, ignore_errors=True)
        except (BlockingIOError, FileNotFoundError):
            continue

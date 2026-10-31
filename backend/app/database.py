from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuotaError(ValueError):
    def __init__(self, message, status_code=429):
        self.status_code = status_code
        super().__init__(message)


class IdempotencyConflict(ValueError):
    pass


class Database:
    def __init__(self, path: Path, max_attempts: int = 3, settings=None):
        self.path = path
        self.max_attempts = max_attempts
        self.settings = settings

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
                    original_filename TEXT NOT NULL, media_type TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, duration_seconds REAL, note_count INTEGER,
                    notes_json TEXT, exports_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    worker_id TEXT, lease_until REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job
                    ON jobs(project_id) WHERE status IN ('queued', 'processing');
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY, last_seen REAL NOT NULL, last_seen_at TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS score_records (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    revision INTEGER NOT NULL, source_notes_revision INTEGER NOT NULL,
                    score_json TEXT NOT NULL, musicxml TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_registry (
                    project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
                    note_json TEXT NOT NULL, PRIMARY KEY(project_id,id)
                );
            """)
            c.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in c.execute("PRAGMA table_info(projects)")}
            additions = {
                "clip_start_seconds": "REAL NOT NULL DEFAULT 0", "clip_end_seconds": "REAL",
                "clip_duration_seconds": "REAL", "instrument_hint": "TEXT", "tempo_hint": "INTEGER",
                "time_signature_hint": "TEXT", "analysis_job_id": "TEXT",
                "result_job_id": "TEXT", "edit_id": "TEXT", "notes_revision": "INTEGER NOT NULL DEFAULT 0",
                "result_schema_version": "INTEGER NOT NULL DEFAULT 1",
                "artifacts_json": "TEXT NOT NULL DEFAULT '[]'",
                "cleanup_json": "TEXT NOT NULL DEFAULT '{\"minimum_note_seconds\":0.08,\"merge_gap_seconds\":0.03}'",
                "max_attempts": "INTEGER NOT NULL DEFAULT 3",
                "input_mode": "TEXT NOT NULL DEFAULT 'solo'",
                "parent_project_id": "TEXT REFERENCES projects(id)", "stem_category": "TEXT",
                "transcription_profile": "TEXT NOT NULL DEFAULT 'solo'",
                "source_origin_seconds": "REAL NOT NULL DEFAULT 0",
                "stems_json": "TEXT NOT NULL DEFAULT '[]'",
                "separation_manifest_json": "TEXT",
                "owner_id": "TEXT NOT NULL DEFAULT 'local'",
                "deleted_at": "TEXT", "reserved_bytes": "INTEGER NOT NULL DEFAULT 0",
                "output_mode": "TEXT NOT NULL DEFAULT 'transcription'",
                "arrangement_source_id": "TEXT",
                "melody_source_id": "TEXT",
                "arrangement_json": "TEXT",
                "arrangement_fingerprint": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    c.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
            # Earlier two-source editions recorded the vocal dependency only
            # in JSON. Restore an explicit owner-checked edge so deleting that
            # source also removes private copies in editions and revoices.
            for row in c.execute("SELECT id,owner_id,arrangement_json FROM projects WHERE melody_source_id IS NULL AND arrangement_json IS NOT NULL").fetchall():
                try:
                    metadata = json.loads(row["arrangement_json"])
                    melody_id = metadata.get("melody_source_project_id") if isinstance(metadata, dict) else None
                    if not isinstance(melody_id, str) or str(uuid.UUID(melody_id)) != melody_id or melody_id == row["id"]:
                        continue
                except (ValueError, TypeError):
                    continue
                if c.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=?", (melody_id, row["owner_id"])).fetchone():
                    c.execute("UPDATE projects SET melody_source_id=? WHERE id=?", (melody_id, row["id"]))
            # UNION deduplicates (project, melody) pairs even if legacy data has
            # a cyclic source chain. Never cross owners or replace an explicit
            # secondary dependency. Each project has only one primary parent.
            c.execute("""WITH RECURSIVE inherited(project_id,melody_id,owner_id) AS (
                SELECT p.id,p.melody_source_id,p.owner_id FROM projects p
                JOIN projects m ON m.id=p.melody_source_id AND m.owner_id=p.owner_id
                UNION
                SELECT child.id,prior.melody_id,child.owner_id FROM projects child
                JOIN inherited prior ON child.arrangement_source_id=prior.project_id AND child.owner_id=prior.owner_id
                WHERE child.melody_source_id IS NULL
            ) UPDATE projects SET melody_source_id=(SELECT MIN(melody_id) FROM inherited WHERE project_id=projects.id)
              WHERE melody_source_id IS NULL AND id IN (SELECT project_id FROM inherited)""")
            c.execute("CREATE INDEX IF NOT EXISTS project_melody_source ON projects(melody_source_id)")
            # Recover a deletion requested before the dependency migration. A
            # tombstoned source must not leave newly discovered private copies
            # live or permanently prevent its own artifact purge.
            deletion_stamp = now()
            c.execute("""WITH RECURSIVE deleted_family(id,owner_id) AS (
                SELECT id,owner_id FROM projects WHERE deleted_at IS NOT NULL
                UNION
                SELECT child.id,child.owner_id FROM projects child JOIN deleted_family parent
                ON child.owner_id=parent.owner_id AND (child.parent_project_id=parent.id
                    OR child.arrangement_source_id=parent.id OR child.melody_source_id=parent.id)
            ) UPDATE projects SET deleted_at=?,status='canceled',updated_at=?
              WHERE deleted_at IS NULL AND id IN (SELECT id FROM deleted_family)""", (deletion_stamp, deletion_stamp))
            c.execute("""UPDATE jobs SET status='canceled',stage='Project deleted',error=NULL,lease_until=NULL,updated_at=?
                WHERE status IN ('queued','processing') AND project_id IN (SELECT id FROM projects WHERE deleted_at IS NOT NULL)""", (deletion_stamp,))
            job_columns = {row["name"] for row in c.execute("PRAGMA table_info(jobs)")}
            if "kind" not in job_columns:
                c.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'transcription'")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_child_per_stem ON projects(parent_project_id,stem_category) WHERE parent_project_id IS NOT NULL")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_arrangement_request ON projects(arrangement_source_id,arrangement_fingerprint) WHERE arrangement_source_id IS NOT NULL AND deleted_at IS NULL")
            # Preserve legacy performance timings. Missing raw activations cannot
            # be reconstructed, so no model scores or raw artifacts are invented.
            for project in c.execute("SELECT * FROM projects WHERE result_schema_version < 2 AND notes_json IS NOT NULL").fetchall():
                notes = json.loads(project["notes_json"])
                for index, note in enumerate(notes):
                    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{project['id']}:{index}"))
                    note.setdefault("id", identifier)
                    note.setdefault("part_id", "part-1")
                    note.setdefault("model_score", None)
                    note.setdefault("provenance", "detected")
                    note.setdefault("source_note_ids", [identifier])
                    note.setdefault("original_timing", {key: note.get(key) for key in ("start", "end", "pitch", "velocity")})
                    note.setdefault("score_timing", None)
                job = c.execute("SELECT id FROM jobs WHERE project_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (project["id"],)).fetchone()
                c.execute("UPDATE projects SET notes_json=?,result_schema_version=2,notes_revision=1,result_job_id=? WHERE id=?",
                          (json.dumps(notes), job["id"] if job else None, project["id"]))
            for project in c.execute("SELECT id,notes_json FROM projects WHERE notes_json IS NOT NULL").fetchall():
                self._register_notes(c, project["id"], json.loads(project["notes_json"]))
            c.executescript("""
                CREATE INDEX IF NOT EXISTS project_owner ON projects(owner_id,deleted_at);
                CREATE TABLE IF NOT EXISTS processing_usage (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS processing_usage_owner ON processing_usage(owner_id,created_at);
                CREATE TABLE IF NOT EXISTS submission_keys (
                    owner_id TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    project_id TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(owner_id,key)
                );
                CREATE TABLE IF NOT EXISTS upload_sessions (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, submission_key TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, reserved_bytes INTEGER NOT NULL DEFAULT 0,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'uploading', created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                    project_id TEXT NOT NULL, sha256 TEXT,
                    UNIQUE(owner_id,submission_key)
                );
                CREATE INDEX IF NOT EXISTS upload_expiry ON upload_sessions(status,expires_at);
                CREATE TABLE IF NOT EXISTS upload_chunks (
                    upload_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
                    offset INTEGER NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(upload_id,offset)
                );
            """)
            upload_columns = {row["name"] for row in c.execute("PRAGMA table_info(upload_sessions)")}
            if "reserved_bytes" not in upload_columns:
                c.execute("ALTER TABLE upload_sessions ADD COLUMN reserved_bytes INTEGER NOT NULL DEFAULT 0")
            c.execute("PRAGMA user_version = 9")

    @staticmethod
    def _register_notes(connection, project_id: str, notes: list) -> None:
        connection.executemany("INSERT INTO note_registry VALUES(?,?,?) ON CONFLICT(project_id,id) DO UPDATE SET note_json=excluded.note_json",
                               [(project_id, note["id"], json.dumps(note)) for note in notes if note.get("id")])

    def note_registry(self, project_id: str) -> dict:
        with self.connect() as c:
            return {row["id"]: json.loads(row["note_json"]) for row in c.execute("SELECT id,note_json FROM note_registry WHERE project_id=?", (project_id,))}

    @staticmethod
    def public_project(row: sqlite3.Row) -> dict:
        p = dict(row)
        p["original_duration_seconds"] = p["duration_seconds"]
        p["analysis_available"] = bool(p.get("analysis_job_id"))
        p["cleanup"] = json.loads(p.pop("cleanup_json"))
        p["separation_available"] = bool(json.loads(p.pop("stems_json")))
        p.pop("separation_manifest_json", None)
        p.pop("arrangement_json", None)
        p.pop("arrangement_fingerprint", None)
        for key in ("notes_json", "exports_json", "media_type", "analysis_job_id", "result_job_id", "edit_id", "artifacts_json", "deleted_at", "reserved_bytes"):
            p.pop(key, None)
        return p

    def list_projects(self, owner_id: str | None = None) -> list[dict]:
        with self.connect() as c:
            result = []
            for row in c.execute("SELECT p.*, (SELECT COUNT(*) FROM jobs j WHERE j.project_id=p.id) AS attempt_count FROM projects p WHERE p.deleted_at IS NULL AND (? IS NULL OR p.owner_id=?) ORDER BY created_at DESC", (owner_id, owner_id)):
                p = self.public_project(row)
                p["retries_remaining"] = max(0, p["max_attempts"] - p.pop("attempt_count"))
                result.append(p)
            return result

    def get_project(self, project_id: str) -> dict | None:
        with self.connect() as c:
            c.execute("BEGIN")
            return self._project_snapshot(c, project_id)

    def _project_snapshot(self, c: sqlite3.Connection, project_id: str) -> dict | None:
        """Read all project fields inside the caller's read/write transaction."""
        p = c.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
        if p is None:
            return None
        result = self.public_project(p)
        result["arrangement"] = json.loads(p["arrangement_json"]) if p["arrangement_json"] else None
        result.update(notes=json.loads(p["notes_json"]) if p["notes_json"] else None,
                      exports=json.loads(p["exports_json"]), artifacts=json.loads(p["artifacts_json"]),
                      engine="piano-arrangement" if p["output_mode"] == "accompaniment" else "source-separation" if p["input_mode"] == "mixed" else
                      f"pyin-{p['transcription_profile']}" if p["transcription_profile"] in {"vocals", "bass"} else "basic-pitch")
        # Legacy MIDI-derived XML is never advertised as canonical notation.
        current_score = c.execute("SELECT source_notes_revision FROM score_records WHERE project_id=?", (project_id,)).fetchone()
        if not current_score or current_score["source_notes_revision"] != p["notes_revision"]:
            result["exports"] = [item for item in result["exports"] if item != "musicxml"]
        result["jobs"] = [dict(j) for j in c.execute(
            "SELECT id,status,kind,stage,error,created_at,updated_at FROM jobs WHERE project_id = ? ORDER BY created_at DESC", (project_id,))]
        result["retries_remaining"] = max(0, p["max_attempts"] - len(result["jobs"]))
        return result

    def storage(self, project_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT media_type,analysis_job_id,result_job_id,edit_id FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row)

    def create_arrangement(self, source_id, identifier, result_id, notes_revision, score_revision,
                           notes, score, musicxml, metadata, fingerprint, prepare_files, reserved_bytes):
        """Publish a separate derived project; never overwrite its transcription.

        Revision fences, quota admission, duplicate detection and publication
        share one transaction. The caller removes unpublished files on error.
        """
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            source = c.execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (source_id,)).fetchone()
            saved = c.execute("SELECT revision FROM score_records WHERE project_id=?", (source_id,)).fetchone()
            if (not source or source["status"] != "completed" or source["notes_revision"] != notes_revision
                    or (saved["revision"] if saved else 0) != score_revision):
                raise IdempotencyConflict("The source notes or score changed. Reload before arranging.")
            melody_id = metadata.get("melody_source_project_id")
            if melody_id:
                melody = c.execute("SELECT notes_revision,deleted_at,owner_id FROM projects WHERE id=?", (melody_id,)).fetchone()
                if not melody or melody["deleted_at"] or melody["owner_id"] != source["owner_id"] or melody["notes_revision"] != metadata["melody_source_notes_revision"]:
                    raise IdempotencyConflict("The vocal source changed while preparing the edition. Reload before arranging.")
            duplicate = c.execute("SELECT id FROM projects WHERE arrangement_source_id=? AND arrangement_fingerprint=? AND deleted_at IS NULL", (source_id, fingerprint)).fetchone()
            if duplicate:
                return self._project_snapshot(c, duplicate["id"])
            self._check_project_quota(c, source["owner_id"], reserved_bytes)
            self._check_job_quota(c, source["owner_id"])
            prepare_files()
            row = dict(source)
            stamp = now()
            row.update(id=identifier, title=score["title"], created_at=stamp, updated_at=stamp,
                       status="completed", notes_json=json.dumps(notes), note_count=len(notes), notes_revision=1,
                       exports_json=json.dumps(["midi", "musicxml"] + (["raw-midi"] if metadata.get("raw_midi_available") else [])),
                       artifacts_json=json.dumps(["normalized-notes", "manifest"]), result_schema_version=2,
                       result_job_id=result_id, analysis_job_id=result_id if metadata.get("analysis_available") else None,
                       edit_id=None, input_mode="solo", parent_project_id=None, stem_category=None,
                       transcription_profile="solo", instrument_hint="piano", stems_json="[]", separation_manifest_json=None,
                       output_mode=metadata.get("output_mode", "accompaniment"), arrangement_source_id=source_id,
                       melody_source_id=metadata.get("melody_source_project_id") or source["melody_source_id"],
                       arrangement_json=json.dumps(metadata), arrangement_fingerprint=fingerprint,
                       reserved_bytes=reserved_bytes)
            columns = list(row)
            c.execute(f"INSERT INTO projects({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [row[k] for k in columns])
            c.execute("INSERT INTO jobs(id,project_id,status,stage,created_at,updated_at,kind) VALUES(?,?,'completed','Accompaniment arranged; review required',?,?,'arrangement')", (result_id, identifier, stamp, stamp))
            c.execute("INSERT INTO processing_usage VALUES(?,?,?)", (result_id, source["owner_id"], time.time()))
            self._register_notes(c, identifier, notes)
            c.execute("INSERT INTO score_records VALUES(?,1,1,?,?,?)", (identifier, json.dumps(score), musicxml, stamp))
            return self._project_snapshot(c, identifier)

    def media_type(self, project_id: str) -> str:
        return self.storage(project_id)["media_type"]

    def analysis_job_id(self, project_id: str) -> str | None:
        return self.storage(project_id)["analysis_job_id"]

    def separation(self, project_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT stems_json,separation_manifest_json,result_job_id FROM projects WHERE id=?", (project_id,)).fetchone()
            return {"stems": json.loads(row["stems_json"]),
                    "manifest": json.loads(row["separation_manifest_json"]) if row["separation_manifest_json"] else None,
                    "job_id": row["result_job_id"]}

    def list_children(self, project_id: str) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM projects WHERE parent_project_id=? AND deleted_at IS NULL ORDER BY created_at", (project_id,)).fetchall()
            return [self.public_project(row) for row in rows]

    def create_stem_child(self, project_id: str, category: str, child_id: str, prepare_source) -> tuple[dict, bool]:
        """Fence double clicks and copies under one write transaction."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            parent = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not parent or parent["deleted_at"] or parent["input_mode"] != "mixed" or parent["status"] != "completed":
                raise ValueError("Finish source separation before selecting a part.")
            existing = c.execute("SELECT id FROM projects WHERE parent_project_id=? AND stem_category=?", (project_id, category)).fetchone()
            if existing:
                value = self._project_snapshot(c, existing["id"])
                if value is None:
                    raise ValueError("This part is being deleted. Wait for storage cleanup before selecting it again.")
                return value, False
            stems = json.loads(parent["stems_json"])
            stem = next((stem for stem in stems if stem["category"] == category), None)
            if stem is None:
                raise KeyError(category)
            if category not in {"vocals", "bass", "other"}:
                raise ValueError("Drum/percussion notation is unsupported. Choose vocals, bass, or an experimental other stem.")
            reserved = self.storage_reservation(stem["duration_seconds"], False, stem.get("bytes", 0))
            self._check_project_quota(c, parent["owner_id"], reserved)
            self._check_job_quota(c, parent["owner_id"])
            prepare_source(stem, parent["result_job_id"])
            stamp = now()
            c.execute("""INSERT INTO projects(id,title,source,original_filename,media_type,created_at,updated_at,status,
                max_attempts,result_schema_version,input_mode,parent_project_id,stem_category,transcription_profile,
                source_origin_seconds,clip_start_seconds,clip_end_seconds,instrument_hint,tempo_hint,time_signature_hint,cleanup_json)
                VALUES(?,?,?,?,?,?,?,'queued',?,2,'solo',?,?,?,?,0,?,?,?,?,?)""",
                (child_id, f"{parent['title']} · {category}", "upload", f"{category}.wav", "audio/wav", stamp, stamp,
                 self.max_attempts, project_id, category, category, parent["clip_start_seconds"],
                 stem["duration_seconds"], "voice" if category == "vocals" else "bass" if category == "bass" else "other",
                 parent["tempo_hint"], parent["time_signature_hint"], parent["cleanup_json"]))
            c.execute("UPDATE projects SET owner_id=?,reserved_bytes=? WHERE id=?", (parent["owner_id"], reserved, child_id))
            self._insert_job(c, child_id, stamp)
            return self._project_snapshot(c, child_id), True

    def discarded_mixed_attempts(self) -> list[dict]:
        with self.connect() as c:
            return [dict(row) for row in c.execute("""SELECT j.id,j.project_id FROM jobs j JOIN projects p ON p.id=j.project_id
                WHERE j.status IN ('failed','canceled') AND (p.input_mode='mixed' OR p.parent_project_id IS NOT NULL)
                AND (p.result_job_id IS NULL OR p.result_job_id<>j.id)""")]

    def clear_discarded_analysis(self, job_id: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE projects SET analysis_job_id=NULL WHERE analysis_job_id=? AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND status IN ('failed','canceled'))", (job_id, job_id))

    def create_project(self, project_id: str, title: str, source: str, filename: str, media_type: str,
                       *, clip_start_seconds: float = 0, clip_end_seconds: float | None = None,
                       instrument_hint: str | None = None, tempo_hint: int | None = None,
                       time_signature_hint: str | None = None, cleanup: dict | None = None,
                       input_mode: str = "solo", owner_id: str = "local", reserved_bytes: int = 0,
                       idempotency_key: str | None = None, fingerprint: str = "",
                       upload_id: str | None = None) -> dict:
        stamp = now()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM submission_keys WHERE created_at<?", (time.time()-7*86400,))
            if idempotency_key:
                existing = c.execute("SELECT * FROM submission_keys WHERE owner_id=? AND key=?", (owner_id, idempotency_key)).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise IdempotencyConflict("This submission key was already used with different audio or settings.")
                    project = self._project_snapshot(c, existing["project_id"])
                    if project is None:
                        raise IdempotencyConflict("This submission was deleted. Start a new upload with a new submission key.")
                    return project
            self._check_project_quota(c, owner_id, reserved_bytes, exclude_upload_id=upload_id)
            c.execute("INSERT INTO projects(id,title,source,original_filename,media_type,created_at,updated_at,status,max_attempts,result_schema_version) VALUES(?,?,?,?,?,?,?,'queued',?,2)",
                      (project_id, title, source, filename, media_type, stamp, stamp, self.max_attempts))
            c.execute("UPDATE projects SET clip_start_seconds=?,clip_end_seconds=?,instrument_hint=?,tempo_hint=?,time_signature_hint=?,cleanup_json=?,input_mode=? WHERE id=?",
                      (clip_start_seconds, clip_end_seconds, instrument_hint, tempo_hint, time_signature_hint,
                       json.dumps(cleanup or {"minimum_note_seconds": 0.08, "merge_gap_seconds": 0.03}), input_mode, project_id))
            c.execute("UPDATE projects SET owner_id=?,reserved_bytes=? WHERE id=?", (owner_id, reserved_bytes, project_id))
            self._insert_job(c, project_id, stamp)
            if idempotency_key:
                c.execute("INSERT INTO submission_keys VALUES(?,?,?,?,?)", (owner_id, idempotency_key, fingerprint, project_id, time.time()))
            if upload_id:
                updated = c.execute("UPDATE upload_sessions SET status='completed',updated_at=? WHERE id=? AND owner_id=? AND project_id=? AND status='uploading'",
                                    (time.time(), upload_id, owner_id, project_id)).rowcount
                if not updated:
                    raise IdempotencyConflict("This upload is no longer available for submission.")
            return self._project_snapshot(c, project_id)

    def record_decoded(self, job: dict, worker_id: str, duration: float, start: float, end: float) -> bool:
        with self.connect() as c:
            updated = c.execute("""UPDATE projects SET duration_seconds=?,clip_start_seconds=?,
                clip_end_seconds=?,clip_duration_seconds=?,analysis_job_id=?,updated_at=?
                WHERE id=? AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND worker_id=? AND status='processing' AND lease_until>?)""",
                (duration, start, end, end - start, job["id"], now(), job["project_id"], job["id"], worker_id, time.time())).rowcount
            return bool(updated)

    def _insert_job(self, c: sqlite3.Connection, project_id: str, stamp: str) -> None:
        project = c.execute("SELECT input_mode,owner_id,deleted_at FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project or project["deleted_at"]:
            raise ValueError("Project was deleted.")
        self._check_job_quota(c, project["owner_id"])
        c.execute("DELETE FROM processing_usage WHERE created_at<?", (time.time()-86400,))
        c.execute("INSERT INTO processing_usage VALUES(?,?,?)", (str(uuid.uuid4()), project["owner_id"], time.time()))
        mode = project["input_mode"]
        c.execute("INSERT INTO jobs(id,project_id,status,stage,created_at,updated_at,kind) VALUES(?,?,'queued','Waiting for worker',?,?,?)",
                  (str(uuid.uuid4()), project_id, stamp, stamp, "separation" if mode == "mixed" else "transcription"))

    def retry(self, project_id: str) -> dict:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            p = c.execute("SELECT status,max_attempts FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            if p is None:
                raise KeyError(project_id)
            if p["status"] not in {"failed", "canceled"}:
                raise ValueError("Only failed or canceled projects can be retried.")
            count = c.execute("SELECT COUNT(*) FROM jobs WHERE project_id=?", (project_id,)).fetchone()[0]
            if count >= p["max_attempts"]:
                raise ValueError(f"This project has reached its {p['max_attempts']}-attempt limit. Resolve the problem before creating a new project.")
            stamp = now()
            self._insert_job(c, project_id, stamp)
            c.execute("UPDATE projects SET status='queued',updated_at=? WHERE id=?", (stamp, project_id))
        return self.get_project(project_id)

    def cancel(self, project_id: str) -> dict:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            p = c.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
            if p["status"] == "canceled":
                return self.get_project(project_id)
            if p["status"] not in {"queued", "processing"}:
                raise ValueError("Only queued or processing projects can be canceled.")
            stamp = now()
            c.execute("UPDATE jobs SET status='canceled',stage='Canceled',error=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND status IN ('queued','processing')", (stamp, project_id))
            c.execute("UPDATE projects SET status='canceled',updated_at=? WHERE id=?", (stamp, project_id))
        return self.get_project(project_id)

    def heartbeat(self, worker_id: str, job_id: str | None = None, lease_seconds: int = 60, capabilities: dict | None = None) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,last_seen_at=excluded.last_seen_at,capabilities_json=excluded.capabilities_json",
                      (worker_id, time.time(), now(), json.dumps(capabilities or {})))
            if job_id:
                c.execute("UPDATE jobs SET lease_until=? WHERE id=? AND worker_id=? AND status='processing' AND lease_until>?",
                          (time.time() + lease_seconds, job_id, worker_id, time.time()))

    def worker_status(self) -> dict:
        with self.connect() as c:
            w = c.execute("SELECT * FROM workers ORDER BY last_seen DESC LIMIT 1").fetchone()
            return {"online": bool(w and time.time() - w["last_seen"] < 20),
                    "last_seen_at": w["last_seen_at"] if w else None,
                    "capabilities": json.loads(w["capabilities_json"]) if w else {}}

    def _interrupt(self, c: sqlite3.Connection, job: sqlite3.Row, message: str, requeue: bool) -> None:
        stamp = now()
        c.execute("UPDATE jobs SET status='failed',stage='Interrupted',error=?,updated_at=?,lease_until=NULL WHERE id=?", (message, stamp, job["id"]))
        p = c.execute("SELECT max_attempts FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM jobs WHERE project_id=?", (job["project_id"],)).fetchone()[0]
        retry = requeue and count < p["max_attempts"]
        if retry:
            try:
                self._insert_job(c, job["project_id"], stamp)
            except QuotaError as exc:
                retry = False
                c.execute("UPDATE jobs SET error=? WHERE id=?", (str(exc), job["id"]))
        c.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("queued" if retry else "failed", stamp, job["project_id"]))

    def interrupted(self, job_id: str, worker_id: str, message: str, requeue: bool = True) -> None:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            job = c.execute("SELECT * FROM jobs WHERE id=? AND worker_id=? AND status='processing'", (job_id, worker_id)).fetchone()
            if job:
                self._interrupt(c, job, message, requeue)

    def claim_job(self, worker_id: str, lease_seconds: int = 60) -> dict | None:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for job in c.execute("SELECT * FROM jobs WHERE status='processing' AND lease_until < ?", (time.time(),)).fetchall():
                self._interrupt(c, job, "The analysis worker stopped responding. A new attempt is queued if the retry budget permits.", True)
            row = c.execute("SELECT j.* FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.status='queued' AND p.deleted_at IS NULL ORDER BY j.created_at LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status='processing',stage='Validating and decoding selected audio',worker_id=?,lease_until=?,updated_at=? WHERE id=?",
                      (worker_id, time.time() + lease_seconds, now(), row["id"]))
            c.execute("UPDATE projects SET status='processing',updated_at=? WHERE id=?", (now(), row["project_id"]))
            return dict(row)

    def owns_job(self, job_id: str, worker_id: str) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM jobs WHERE id=? AND worker_id=? AND status='processing' AND lease_until>?", (job_id, worker_id, time.time())).fetchone() is not None

    def stage(self, job_id: str, worker_id: str, stage: str) -> bool:
        with self.connect() as c:
            return bool(c.execute("UPDATE jobs SET stage=?,updated_at=? WHERE id=? AND worker_id=? AND status='processing' AND lease_until>?", (stage, now(), job_id, worker_id, time.time())).rowcount)

    def finish(self, job: dict, worker_id: str, *, notes: list | None = None,
               duration: float | None = None, exports: list[str] | None = None,
               artifacts: list[str] | None = None, error: str | None = None,
               score_bundle: dict | None = None, separation_bundle: dict | None = None) -> bool:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            stamp, status = now(), "failed" if error else "completed"
            updated = c.execute("UPDATE jobs SET status=?,stage=?,error=?,updated_at=?,lease_until=NULL WHERE id=? AND worker_id=? AND status='processing' AND lease_until>?",
                                (status, "Analysis failed" if error else "Stems ready for selection" if separation_bundle else "Ready for review", error, stamp, job["id"], worker_id, time.time())).rowcount
            if not updated:
                return False
            c.execute("""UPDATE projects SET status=?,updated_at=?,duration_seconds=?,notes_json=?,note_count=?,exports_json=?,
                artifacts_json=?,result_job_id=?,edit_id=NULL,notes_revision=?,result_schema_version=2 WHERE id=?""",
                (status, stamp, duration, json.dumps(notes) if notes is not None else None,
                 len(notes) if notes is not None else None, json.dumps(exports or []), json.dumps(artifacts or []),
                 job["id"] if not error else None, 1 if notes is not None else 0, job["project_id"]))
            if not error:
                if separation_bundle is not None:
                    c.execute("UPDATE projects SET stems_json=?,separation_manifest_json=? WHERE id=?",
                              (json.dumps(separation_bundle["stems"]), json.dumps(separation_bundle["manifest"]), job["project_id"]))
                self._register_notes(c, job["project_id"], notes or [])
                c.execute("DELETE FROM score_records WHERE project_id=?", (job["project_id"],))
                if score_bundle is not None:
                    c.execute("INSERT INTO score_records VALUES(?,1,1,?,?,?)",
                              (job["project_id"], json.dumps(score_bundle["score"]), score_bundle["musicxml"], stamp))
            return True

    def get_score(self, project_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM score_records WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                return None
            return {"revision": row["revision"], "source_notes_revision": row["source_notes_revision"],
                    "score": json.loads(row["score_json"]), "musicxml": row["musicxml"]}

    def save_score(self, project_id: str, notes_revision: int, revision: int,
                   score: dict, musicxml: str) -> bool:
        """Publish one canonical score and its rendering in the same transaction."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            project = c.execute("SELECT status,notes_revision,exports_json FROM projects WHERE id=?", (project_id,)).fetchone()
            existing = c.execute("SELECT revision FROM score_records WHERE project_id=?", (project_id,)).fetchone()
            if (not project or project["status"] != "completed" or project["notes_revision"] != notes_revision
                    or (existing["revision"] if existing else 0) != revision):
                return False
            stamp = now()
            c.execute("""INSERT INTO score_records VALUES(?,?,?,?,?,?) ON CONFLICT(project_id)
                DO UPDATE SET revision=excluded.revision,source_notes_revision=excluded.source_notes_revision,
                score_json=excluded.score_json,musicxml=excluded.musicxml,updated_at=excluded.updated_at""",
                (project_id, revision + 1, notes_revision, json.dumps(score), musicxml, stamp))
            exports = json.loads(project["exports_json"])
            if "musicxml" not in exports:
                exports.append("musicxml")
            c.execute("UPDATE projects SET exports_json=?,updated_at=? WHERE id=?", (json.dumps(exports), stamp, project_id))
            return True

    def save_notes(self, project_id: str, revision: int, notes: list, edit_id: str,
                   *, score_bundle: dict | None = None, score_revision: int | None = None,
                   return_project: bool = False) -> bool | dict:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if score_bundle is not None:
                record = c.execute("SELECT revision FROM score_records WHERE project_id=?", (project_id,)).fetchone()
                if (record["revision"] if record else 0) != score_revision:
                    return False
            # Edited performance MIDI is regenerated before the pointer swap.
            # Draft MusicXML is invalidated instead of exporting stale notation.
            row = c.execute("SELECT exports_json,artifacts_json FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            if row is None:
                return False
            exports = [item for item in json.loads(row["exports_json"]) if item == "raw-midi"] + ["midi"]
            if score_bundle is not None:
                exports.append("musicxml")
            artifacts = json.loads(row["artifacts_json"])
            if "normalized-notes" not in artifacts:
                artifacts.append("normalized-notes")
            updated = bool(c.execute("""UPDATE projects SET notes_json=?,note_count=?,notes_revision=notes_revision+1,
                edit_id=?,exports_json=?,artifacts_json=?,updated_at=? WHERE id=? AND deleted_at IS NULL AND status='completed' AND notes_revision=?""",
                (json.dumps(notes), len(notes), edit_id, json.dumps(exports), json.dumps(artifacts), now(), project_id, revision)).rowcount)
            if updated:
                self._register_notes(c, project_id, notes)
                if score_bundle is not None:
                    c.execute("""INSERT INTO score_records VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                        revision=excluded.revision,source_notes_revision=excluded.source_notes_revision,
                        score_json=excluded.score_json,musicxml=excluded.musicxml,updated_at=excluded.updated_at""",
                        (project_id, score_revision + 1, revision + 1, json.dumps(score_bundle["score"]), score_bundle["musicxml"], now()))
                if return_project:
                    return self._project_snapshot(c, project_id)
            return updated


    @staticmethod
    def storage_reservation(duration: float, mixed: bool, source_bytes: int) -> int:
        # Conservative storage admission budget, not a claim about actual size.
        per_second = 44100 * 2 * 4 * 6 if mixed else 512000
        return int(source_bytes + max(0, duration) * per_second + 32 * 1024 * 1024)

    def _check_project_quota(self, c, owner_id, reserved_bytes, exclude_upload_id=None):
        if self.settings is None or not self.settings.authentication_required:
            return
        count, reserved = c.execute("SELECT COUNT(*),COALESCE(SUM(reserved_bytes),0) FROM projects WHERE owner_id=?", (owner_id,)).fetchone()
        pending_count, pending = c.execute("SELECT COUNT(*),COALESCE(SUM(MAX(reserved_bytes,size_bytes)),0) FROM upload_sessions WHERE owner_id=? AND status='uploading' AND (? IS NULL OR id<>?)",
                            (owner_id, exclude_upload_id, exclude_upload_id)).fetchone()
        if count + pending_count >= self.settings.max_projects_per_user:
            raise QuotaError("Project quota reached. Delete projects and wait for cleanup before uploading again.")
        if reserved + pending + reserved_bytes > self.settings.max_storage_mb_per_user * 1024 * 1024:
            raise QuotaError("Storage quota reached. Shorten the selected clip or delete projects and wait for cleanup. Storage is reserved conservatively before analysis.", 413)

    def _check_job_quota(self, c, owner_id):
        if self.settings is None or not self.settings.authentication_required:
            return
        active = c.execute("SELECT COUNT(*) FROM jobs j JOIN projects p ON p.id=j.project_id WHERE p.owner_id=? AND j.status IN ('queued','processing')", (owner_id,)).fetchone()[0]
        if active >= self.settings.max_active_jobs_per_user:
            raise QuotaError("Concurrent processing quota reached. Wait for an active job to finish or cancel it.")
        recent = c.execute("SELECT COUNT(*) FROM processing_usage WHERE owner_id=? AND created_at>?", (owner_id, time.time()-86400)).fetchone()[0]
        if recent >= self.settings.max_jobs_per_day:
            raise QuotaError("Daily processing quota reached. Attempts, retries, selected parts and restart recovery count toward the rolling 24-hour limit.")

    def request_deletion(self, project_id, owner_id):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            project = c.execute("SELECT id FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL", (project_id, owner_id)).fetchone()
            if project is None:
                return False
            identifiers = [row[0] for row in c.execute("WITH RECURSIVE family(id) AS (SELECT id FROM projects WHERE id=? UNION SELECT p.id FROM projects p JOIN family f ON p.parent_project_id=f.id OR p.arrangement_source_id=f.id OR p.melody_source_id=f.id) SELECT id FROM family", (project_id,))]
            stamp = now()
            for identifier in identifiers:
                c.execute("UPDATE projects SET deleted_at=?,status='canceled',updated_at=? WHERE id=?", (stamp, stamp, identifier))
                c.execute("UPDATE jobs SET status='canceled',stage='Project deleted',error=NULL,lease_until=NULL,updated_at=? WHERE project_id=? AND status IN ('queued','processing')", (stamp, identifier))
            return True

    def pending_deletions(self):
        with self.connect() as c:
            return [dict(row) for row in c.execute("SELECT id FROM projects WHERE deleted_at IS NOT NULL ORDER BY parent_project_id IS NULL,created_at DESC")]

    def purge_deleted_project(self, project_id):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM projects WHERE id=? AND deleted_at IS NOT NULL", (project_id,)).fetchone():
                return False
            if c.execute("SELECT 1 FROM projects WHERE parent_project_id=? OR arrangement_source_id=? OR melody_source_id=?", (project_id, project_id, project_id)).fetchone():
                return False
            # Retain only an opaque replay marker for seven days, never the
            # audio/settings fingerprint of a deleted project.
            c.execute("UPDATE submission_keys SET fingerprint='' WHERE project_id=?", (project_id,))
            # Keep the opaque upload replay marker, but do not retain deleted
            # project names, musical settings or original-content hashes.
            c.execute("UPDATE upload_sessions SET status='canceled',metadata_json='{}',fingerprint='',sha256=NULL WHERE project_id=?", (project_id,))
            for table in ('score_records','note_registry','jobs'):
                c.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            c.execute("DELETE FROM projects WHERE id=?", (project_id,))
            return True


    def check_storage_usage(self, owner_id):
        if self.settings is None or not self.settings.authentication_required:
            return
        from .mixed_storage import directory_bytes
        with self.connect() as c:
            identifiers = [row[0] for row in c.execute("SELECT id FROM projects WHERE owner_id=?", (owner_id,))]
            transfers = [row[0] for row in c.execute("SELECT id FROM upload_sessions WHERE owner_id=? AND status='uploading'", (owner_id,))]
        used = sum(directory_bytes(self.settings.data_dir / 'projects' / identifier) for identifier in identifiers)
        used += sum(directory_bytes(self.settings.data_dir / 'uploads' / identifier) for identifier in transfers)
        if used > self.settings.max_storage_mb_per_user * 1024 * 1024:
            raise QuotaError("Private artifact storage quota exceeded. Delete projects and wait for cleanup before processing or saving again.", 413)


    def authorizes(self, project_id, owner_id):
        with self.connect() as c:
            return c.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=? AND deleted_at IS NULL", (project_id, owner_id)).fetchone() is not None

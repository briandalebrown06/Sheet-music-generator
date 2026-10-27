import json
import sqlite3
from contextlib import contextmanager

from backend.config import Settings


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                    extension TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    status TEXT NOT NULL CHECK(status IN ('queued','processing','completed','failed')),
                    error TEXT, started_at TEXT, finished_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.settings.data_dir / "projects.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def project(self, project_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                return None
            project = dict(row)
            project["jobs"] = [dict(job) for job in db.execute(
                "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC, rowid DESC",
                (project_id,),
            )]
        result = self.settings.data_dir / project_id / "notes.json"
        project["notes"] = json.loads(result.read_text()) if (
            project["jobs"][0]["status"] == "completed" and result.exists()
        ) else None
        return project

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Each worker task is bounded to 12 minutes; recover abandoned tasks after 20.
            db.execute("""
                UPDATE jobs SET status='failed', error='Worker interrupted. Retry this project.',
                finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE status='processing' AND started_at < strftime('%Y-%m-%dT%H:%M:%fZ','now','-20 minutes')
            """)
            row = db.execute("""
                SELECT jobs.id, project_id, extension FROM jobs JOIN projects
                ON projects.id=jobs.project_id WHERE status='queued'
                ORDER BY jobs.created_at, jobs.rowid LIMIT 1
            """).fetchone()
            if row:
                db.execute("""
                    UPDATE jobs SET status='processing',
                    started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?
                """, (row["id"],))
                return dict(row)
        return None

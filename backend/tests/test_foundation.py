import io
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import Store
from backend.engines import get_engine
from backend.worker import process_job


def silent_wav():
    """Test-only silence, never a stand-in for an actual transcription."""
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\0\0" * 2205)
    return data.getvalue()


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(Path(self.temporary.name), max_upload_mb=1)
        self.store = Store(self.settings)
        self.client = TestClient(
            create_app(self.settings), base_url="http://localhost", client=("127.0.0.1", 50000),
        )
        self.addCleanup(self.client.close)

    def upload(self):
        response = self.client.post("/api/projects", files={"audio": ("solo.wav", silent_wav())})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "local-single-user")

    def test_upload_persists_project_job_and_original(self):
        project = self.upload()
        self.assertEqual(project["jobs"][0]["status"], "queued")
        self.assertIsNone(project["notes"])
        self.assertEqual(Store(self.settings).project(project["id"]), project)
        self.assertEqual(len(self.client.get("/api/projects").json()), 1)
        response = self.client.get(f"/api/projects/{project['id']}/audio")
        self.assertEqual(response.content, silent_wav())
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_invalid_uploads_leave_no_records(self):
        for name, data, status in [
            ("song.txt", b"not audio", 415), ("empty.wav", b"", 422),
            ("large.wav", b"x" * (1024 * 1024 + 1), 413),
            ("huge.wav", b"x" * (1024 * 1024 + 100000), 413),
        ]:
            with self.subTest(name=name):
                result = self.client.post("/api/projects", files={"audio": (name, data)})
                self.assertEqual(result.status_code, status, result.text)
        self.assertEqual(self.client.get("/api/projects").json(), [])
        self.assertEqual([p for p in self.settings.data_dir.iterdir() if p.is_dir()], [])

    def test_generated_paths_do_not_use_client_filename(self):
        response = self.client.post("/api/projects", files={"audio": ("../../escape.wav", silent_wav())})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["filename"], "escape.wav")
        self.assertFalse((self.settings.data_dir.parent / "escape.wav").exists())

    def test_unknown_project(self):
        self.assertEqual(self.client.get("/api/projects/missing").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/missing/retry").status_code, 404)

    def test_rejects_nonlocal_clients_and_untrusted_origins(self):
        for headers in [
            {"Origin": "https://evil.example"}, {"Origin": "null"},
            {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"},
        ]:
            with self.subTest(headers=headers):
                self.assertIn(self.client.get("/api/health", headers=headers).status_code, (400, 403))
        with TestClient(create_app(self.settings), base_url="http://localhost",
                        client=("192.0.2.1", 1234)) as remote:
            self.assertEqual(remote.get("/api/health").status_code, 403)
        for origin in ["http://localhost:5173", "http://127.0.0.1:5173"]:
            self.assertEqual(self.client.get("/api/health", headers={"Origin": origin}).status_code, 200)

    def test_claim_is_exclusive_and_retry_preserves_history(self):
        project = self.upload()
        path = f"/api/projects/{project['id']}/retry"
        self.assertEqual(self.client.post(path).status_code, 409)
        job = self.store.claim()
        self.assertIsNotNone(job)
        self.assertIsNone(Store(self.settings).claim())
        with patch("backend.worker.shutil.which", return_value=None):
            process_job(self.store, job)
        failed = self.store.project(project["id"])
        self.assertEqual(failed["jobs"][0]["status"], "failed")
        self.assertIn("FFmpeg", failed["jobs"][0]["error"])
        self.assertIsNone(failed["notes"])
        response = self.client.post(path)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["jobs"]), 2)
        self.assertEqual(response.json()["jobs"][0]["status"], "queued")
        self.assertEqual(self.client.post(path).status_code, 409)

    def test_interrupted_job_recovers_without_invented_results(self):
        project = self.upload()
        self.store.claim()
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET started_at='2000-01-01T00:00:00.000Z'")
        self.assertIsNone(self.store.claim())
        recovered = self.store.project(project["id"])
        self.assertEqual(recovered["jobs"][0]["status"], "failed")
        self.assertIn("interrupted", recovered["jobs"][0]["error"])
        self.assertIsNone(recovered["notes"])

    def test_worker_stores_only_engine_output(self):
        project = self.upload()
        job = self.store.claim()
        directory = self.settings.data_dir / project["id"]

        def fake_process(command, **_kwargs):
            # Explicit adapter test fixture: no fabricated musical notes.
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(silent_wav())
            else:
                Path(command[-2]).write_text(json.dumps([]))
            return subprocess.CompletedProcess(command, 0)

        with patch("backend.worker.shutil.which", return_value="/usr/bin/ffmpeg"), \
                patch("backend.worker.subprocess.run", side_effect=fake_process):
            process_job(self.store, job)
        completed = self.store.project(project["id"])
        self.assertEqual(completed["jobs"][0]["status"], "completed")
        self.assertEqual(completed["notes"], [])
        self.assertFalse((directory / "normalized.wav").exists())

    def test_invalid_audio_and_timeout_fail_jobs(self):
        for outcome in [subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired("ffmpeg", 120)]:
            with self.subTest(outcome=outcome):
                project = self.upload()
                job = self.store.claim()
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("backend.worker.shutil.which", return_value="ffmpeg"), \
                        patch("backend.worker.subprocess.run", **kwargs):
                    process_job(self.store, job)
                result = self.store.project(project["id"])
                self.assertEqual(result["jobs"][0]["status"], "failed")
                self.assertIsNone(result["notes"])

    def test_unknown_engine_is_not_a_fake_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported transcription engine"):
            get_engine("fake")


if __name__ == "__main__":
    unittest.main()

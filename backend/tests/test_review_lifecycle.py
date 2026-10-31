"""Focused independent review regressions for lifecycle and editing races."""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from backend.app.database import Database
from backend.app.worker import terminate_job


def _exit_group_leader_with_descendant(pid_file: str, heartbeat_file: str, ignore_term: bool = False) -> None:
    os.setsid()
    # Explicit test-only child emits heartbeat bytes until its process group is
    # terminated. No application or user process is targeted by this fixture.
    script = (
        "import pathlib,time,signal; " + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
        + "p=pathlib.Path(" + repr(heartbeat_file) + "); "
        "\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(0.05)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(pid_file).write_text(str(child.pid))
    # Wait until the descendant actually runs before the group leader exits.
    until = time.monotonic() + 3
    while not Path(heartbeat_file).exists() and time.monotonic() < until:
        time.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_terminate_job_stops_descendants_after_group_leader_exits(tmp_path, ignore_term):
    pid_file, heartbeat_file = tmp_path / "descendant.pid", tmp_path / "heartbeat"
    process = multiprocessing.get_context("spawn").Process(
        target=_exit_group_leader_with_descendant, args=(str(pid_file), str(heartbeat_file), ignore_term))
    process.start()
    try:
        process.join(timeout=5)
        assert not process.is_alive(), "Test leader should already have exited"
        assert process.exitcode == 0 and heartbeat_file.exists()
        first = heartbeat_file.read_text()
        time.sleep(0.15)
        assert heartbeat_file.read_text() != first, "Descendant survives the leader before cleanup"
        terminate_job(process)
        time.sleep(0.2)
        last = heartbeat_file.read_text()
        time.sleep(0.2)
        assert heartbeat_file.read_text() == last, "Descendant must stop after cancellation cleanup"
    finally:
        # Always clean exactly this generated group, including on failed asserts.
        if process.pid:
            terminate_job(process)


def test_macos_teardown_permission_race_retries_until_group_is_gone(monkeypatch):
    from types import SimpleNamespace
    import backend.app.worker as worker
    sent, pauses = [], []
    def group_signal(pid, signum):
        sent.append(signum)
        if len(sent) == 2:
            raise PermissionError(1, "Explicit test fixture: transient group teardown")
        if len(sent) == 3:
            raise ProcessLookupError(3, "Explicit test fixture: group no longer exists")
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker.os, "killpg", group_signal)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=time.monotonic, sleep=pauses.append))
    child = SimpleNamespace(pid=12345, is_alive=lambda: False, join=lambda timeout: None)
    worker.terminate_job(child)
    assert sent == [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL]
    assert pauses == [.01]


def test_persistent_group_permission_failure_is_never_suppressed(monkeypatch):
    from types import SimpleNamespace
    import backend.app.worker as worker
    calls = []
    def denied(pid, signum):
        calls.append((pid, signum))
        raise PermissionError(1, "Explicit test fixture: living process permission denied")
    clock = iter([0.0, .1, .25])
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker.os, "killpg", denied)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    child = SimpleNamespace(pid=12345, is_alive=lambda: True, join=lambda timeout: None)
    with pytest.raises(PermissionError, match="permission denied"):
        worker.terminate_job(child)
    assert calls == [(12345, signal.SIGTERM), (12345, signal.SIGTERM)]


def _database(tmp_path):
    db = Database(tmp_path / "review.sqlite3", max_attempts=2)
    db.initialize()
    project_id = str(uuid.uuid4())
    db.create_project(project_id, "TEST FIXTURE", "upload", "fixture.wav", "audio/wav")
    return db, project_id


def test_canceled_attempt_cannot_publish_after_retry_claims_new_attempt(tmp_path):
    db, project_id = _database(tmp_path)
    stale = db.claim_job("old-worker")
    db.cancel(project_id)
    db.retry(project_id)
    current = db.claim_job("new-worker")
    assert current["id"] != stale["id"]
    assert not db.record_decoded(stale, "old-worker", 10, 0, 10)
    assert not db.stage(stale["id"], "old-worker", "Stale stage")
    assert not db.finish(stale, "old-worker", notes=[{"pitch": 99}], exports=["midi"])
    assert db.finish(current, "new-worker", notes=[], duration=1, exports=[])
    assert db.get_project(project_id)["notes"] == []
    assert db.get_project(project_id)["status"] == "completed"


def test_revision_swap_rejects_stale_editor_without_changing_raw_result_pointer(tmp_path):
    db, project_id = _database(tmp_path)
    job = db.claim_job("worker")
    detected_id = str(uuid.uuid4())
    notes = [{"id": detected_id, "pitch": 60, "start": 0.1, "end": 0.8, "velocity": 90,
              "original_timing": {"pitch": 60, "start": 0.1, "end": 0.8, "velocity": 90},
              "provenance": "detected"}]
    assert db.finish(job, "worker", notes=notes, duration=1,
                     exports=["midi", "raw-midi", "musicxml"], artifacts=["raw-note-events"])
    revision = db.get_project(project_id)["notes_revision"]
    accepted = json.loads(json.dumps(notes))
    accepted[0]["pitch"] = 62
    accepted[0]["provenance"] = "edited"
    assert db.save_notes(project_id, revision, accepted, "edit-a")
    stale = json.loads(json.dumps(notes))
    stale[0]["pitch"] = 72
    assert not db.save_notes(project_id, revision, stale, "edit-b")
    project = db.get_project(project_id)
    assert project["notes"][0]["pitch"] == 62
    assert project["notes"][0]["original_timing"]["pitch"] == 60
    assert project["notes_revision"] == revision + 1
    assert db.storage(project_id)["result_job_id"] == job["id"]
    assert db.storage(project_id)["edit_id"] == "edit-a"
    assert project["exports"] == ["raw-midi", "midi"]


def test_supervisor_cleans_group_if_cancellation_and_child_exit_race(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.app.config import Settings
    from backend.app.worker import Worker
    settings = Settings(data_dir=tmp_path / "data")
    worker = Worker(settings)
    project_id = str(uuid.uuid4())
    worker.db.create_project(project_id, "TEST FIXTURE", "upload", "fixture.wav", "audio/wav")
    cleaned = []
    class ExitedDuringCancel:
        """Test-only process control race; real group cleanup is tested above."""
        def start(self):
            worker.db.cancel(project_id)
        def is_alive(self):
            return False
        def join(self, timeout=None):
            pass
    child = ExitedDuringCancel()
    monkeypatch.setattr("backend.app.worker.multiprocessing.get_context", lambda _: SimpleNamespace(Process=lambda *args, **kwargs: child))
    monkeypatch.setattr("backend.app.worker.terminate_job", lambda process: cleaned.append(process))
    assert worker.run_once()
    assert cleaned == [child]
    assert worker.db.get_project(project_id)["status"] == "canceled"

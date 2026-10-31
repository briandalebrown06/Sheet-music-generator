from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
import uuid

from .config import Settings
from .database import Database
from .engine import TranscriptionEngine, capabilities
from .pipeline import process_job
from .mixed_storage import cleanup_discarded_attempts
from .project_lifecycle import cleanup_deleted_projects
from .analysis_checkpoints import cleanup_analysis_checkpoints
from .uploads import cleanup_uploads

logger = logging.getLogger("score_worker")


def _signal_process_group(pid: int, signum: int) -> None:
    """Retain real permission failures while allowing macOS group teardown.

    On macOS an orphan's process group can briefly return EPERM after its last
    member exits, then ESRCH once group teardown finishes. A bounded retry must
    either deliver the signal or observe disappearance; persistent EPERM still
    propagates so a living, uncleaned process is never silently accepted.
    """
    deadline = time.monotonic() + 0.2
    while True:
        try:
            os.killpg(pid, signum)
            return
        except PermissionError:
            if sys.platform != "darwin" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def terminate_job(process) -> None:
    """Stop only this attempt and its decoder descendants, never another job."""
    try:
        if os.name == "posix":
            _signal_process_group(process.pid, signal.SIGTERM)
        elif process.is_alive():
            process.terminate()
    except ProcessLookupError:
        if process.is_alive():
            process.terminate()  # The child may not have called setsid yet.
    process.join(timeout=2)
    # Group members may outlive their leader. Clean them up even after an
    # unexpected analysis-process exit, not just when the leader is alive.
    if os.name == "posix":
        try:
            _signal_process_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
    elif process.is_alive():
        process.kill()
    process.join(timeout=2)


class Worker:
    def __init__(self, settings: Settings, engine: TranscriptionEngine | None = None, separator=None):
        self.settings, self.engine, self.separator = settings, engine, separator
        settings.prepare()
        self.db = Database(settings.database_path, settings.max_job_attempts, settings=settings)
        self.db.initialize()
        self.id = str(uuid.uuid4())
        self.stop = threading.Event()
        self.active_job: str | None = None

    def run_once(self) -> bool:
        cleanup_deleted_projects(self.settings, self.db)
        cleanup_discarded_attempts(self.settings, self.db)
        cleanup_analysis_checkpoints(self.settings, self.db)
        cleanup_uploads(self.settings, self.db)
        reported = capabilities(self.settings.ffmpeg_binary, self.settings.basic_pitch_runtime)
        self.db.heartbeat(self.id, capabilities=reported)
        job = self.db.claim_job(self.id, self.settings.lease_seconds)
        # claim_job may have expired and fenced a dead attempt during recovery.
        cleanup_discarded_attempts(self.settings, self.db)
        if job is None:
            return False
        self.active_job = job["id"]
        try:
            if self.engine is not None or self.separator is not None:
                process_job(self.settings, job, self.id, engine=self.engine, separator=self.separator, isolated=False)
                return True
            process = multiprocessing.get_context("spawn").Process(
                target=process_job, args=(self.settings, job, self.id), name=f"analysis-{job['id']}")
            process.start()
            timeout = self.settings.separation_timeout_seconds if job["kind"] == "separation" else self.settings.job_timeout_seconds
            started, last_pulse = time.monotonic(), 0.0
            try:
                while process.is_alive():
                    if not self.db.owns_job(job["id"], self.id):
                        terminate_job(process)
                        break
                    if self.stop.is_set():
                        terminate_job(process)
                        self.db.interrupted(job["id"], self.id, "The worker shut down during analysis. Recovery is queued within the attempt limit.")
                        break
                    if time.monotonic() - started >= timeout:
                        terminate_job(process)
                        setting = "SEPARATION_TIMEOUT_SECONDS" if job["kind"] == "separation" else "JOB_TIMEOUT_SECONDS"
                        self.db.finish(job, self.id, error=f"Analysis exceeded the configured job time limit. Try a shorter clip or adjust {setting}, then retry.")
                        break
                    if time.monotonic() - last_pulse >= min(2, self.settings.lease_seconds / 3):
                        self.db.heartbeat(self.id, job["id"], self.settings.lease_seconds, reported)
                        last_pulse = time.monotonic()
                    process.join(timeout=0.2)
            finally:
                # The leader can exit concurrently with cancellation or lease
                # loss; descendants still need cleanup in every exit path.
                terminate_job(process)
            if self.db.owns_job(job["id"], self.id):
                self.db.finish(job, self.id, error="The analysis process stopped before publishing a result. Inspect the worker log and retry.")
        except Exception:
            logger.exception("Worker supervisor failed for job %s", job["id"])
            self.db.interrupted(job["id"], self.id, "The worker could not run the analysis process. Check the worker log before retrying.", requeue=False)
        finally:
            self.active_job = None
            cleanup_deleted_projects(self.settings, self.db)
            cleanup_discarded_attempts(self.settings, self.db)
            cleanup_analysis_checkpoints(self.settings, self.db)
        return True

    def run(self, once: bool = False) -> None:
        logger.info("Local worker ready. Data directory: %s", self.settings.data_dir)
        while not self.stop.is_set():
            worked = self.run_once()
            if once:
                break
            if not worked:
                self.stop.wait(self.settings.worker_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the separate local audio analysis worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker(Settings.from_env())
    def shutdown(signum, frame):
        worker.stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.run(once=args.once)


if __name__ == "__main__":
    main()

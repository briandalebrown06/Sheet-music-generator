import json
import logging
import shutil
import subprocess
import sys
import time
import wave

from backend.config import Settings
from backend.db import Store

logger = logging.getLogger(__name__)


def process_job(store: Store, job: dict):
    directory = store.settings.data_dir / job["project_id"]
    normalized = directory / "normalized.wav"
    temporary = directory / "notes.tmp.json"
    error = None
    try:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is missing. Install it, restart the worker, and retry.")
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,mp3,flac,ogg,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2",
            "-i", str(directory / f"original{job['extension']}"),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "22050",
            "-t", str(store.settings.max_audio_seconds + 1), str(normalized),
        ]
        decoded = subprocess.run(command, capture_output=True, timeout=120, check=False)
        if decoded.returncode:
            raise RuntimeError("FFmpeg could not decode this audio. Try a valid supported audio file.")
        with wave.open(str(normalized)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration > store.settings.max_audio_seconds:
            raise RuntimeError(f"Audio exceeds the {store.settings.max_audio_seconds}-second limit.")
        if duration == 0:
            raise RuntimeError("No audio samples found.")
        inferred = subprocess.run(
            [sys.executable, "-m", "backend.transcribe", str(normalized),
             str(temporary), store.settings.engine],
            capture_output=True, timeout=600, check=False,
        )
        if inferred.returncode:
            # Preserve the actionable adapter message, not model progress output.
            message = inferred.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(message[-1][:500] if message else "Transcription engine failed.")
        notes = json.loads(temporary.read_text())
        if not isinstance(notes, list):
            raise RuntimeError("The transcription engine returned an invalid result.")
        temporary.replace(directory / "notes.json")
    except subprocess.TimeoutExpired:
        error = "Audio processing timed out. Try a shorter solo recording."
    except Exception as exc:
        logger.exception("Job %s failed", job["id"])
        error = str(exc)[:500]
    finally:
        normalized.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    with store.connect() as db:
        db.execute("""
            UPDATE jobs SET status=?, error=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id=?
        """, ("failed" if error else "completed", error, job["id"]))


def main():
    logging.basicConfig(level=logging.INFO)
    store = Store(Settings.from_env())
    logger.info("Worker ready. Waiting for local audio projects.")
    try:
        while True:
            job = store.claim()
            if job:
                process_job(store, job)
            else:
                time.sleep(2)
    except KeyboardInterrupt:
        logger.info("Worker stopped. Interrupted jobs become retryable after 20 minutes.")


if __name__ == "__main__":
    main()

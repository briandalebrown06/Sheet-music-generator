"""One real analysis attempt, isolated by the worker in a disposable process."""
from __future__ import annotations

import logging
import json
import os
import signal
import threading
import re
import sys
import wave
from pathlib import Path

from .audio import AnalysisError, prepare_analysis_audio
from .config import Settings
from .database import Database, QuotaError
from .engine import atomic_json
from .notation import build_exports
from .performance import write_performance_midi
from .mixed_storage import check_storage
from .project_lifecycle import artifact_lease

ARTIFACTS = {
    "raw-model-output": ("raw-model-output.npz", "application/octet-stream"),
    "raw-note-events": ("raw-note-events.json", "application/json"),
    "raw-performance": ("raw-performance.mid", "audio/midi"),
    "normalized-notes": ("notes.json", "application/json"),
    "manifest": ("manifest.json", "application/json"),
    "separation-manifest": ("separation-manifest.json", "application/json"),
}


class LostLease(Exception):
    pass


def measured_resources() -> dict:
    """Observed process high-water mark, not a machine-sizing guarantee."""
    if os.name != "posix":
        return {}
    import resource
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"analysis_process_peak_rss_bytes": int(usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024),
            "analysis_process_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
            "scope": "This analysis process through result construction; excludes API, worker supervisor, and decoder subprocesses."}


def validate_separation_result(result: dict, output: Path, input_audio: Path, prepared) -> dict:
    """Only publish real, complete, sample-aligned WAVs in this attempt."""
    import numpy as np
    import soundfile as sf
    with wave.open(str(input_audio), "rb") as source:
        frames, rate = source.getnframes(), source.getframerate()
    stems = result.get("stems", [])
    if not 1 <= len(stems) <= 8 or not isinstance(result.get("manifest"), dict):
        raise AnalysisError("The separation adapter returned an invalid result manifest.")
    categories = set()
    for stem in stems:
        category = stem.get("category", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", category) or category in categories:
            raise AnalysisError("The separation adapter returned invalid or duplicate stem categories.")
        categories.add(category)
        path = (output / stem.get("filename", "")).resolve()
        if not path.is_relative_to(output.resolve()) or path.suffix.lower() != ".wav" or not path.is_file():
            raise AnalysisError("The separation adapter did not produce the expected local WAV files.")
        with sf.SoundFile(path) as audio:
            if len(audio) != frames or audio.samplerate != rate or audio.channels != 2 or audio.format not in {"WAV", "WAVEX"}:
                raise AnalysisError("A separated stem lost sample alignment with the selected recording. No incomplete stems were published.")
            for block in audio.blocks(blocksize=rate, dtype="float32", always_2d=True):
                if not np.isfinite(block).all():
                    raise AnalysisError("The separator produced non-finite audio samples. No invalid stems were published.")
            stem.update(filename=str(path.relative_to(output.resolve())), duration_seconds=frames / rate,
                        sample_rate=rate, channels=2)
    result["manifest"].update(operation="source_separation", arrangement=False,
        original_recording_offset_seconds=prepared.clip_start, clip_duration_seconds=prepared.clip_duration,
        timing="All stems have the same PCM frame count and start at the selected clip's original offset.",
        categories=[stem["category"] for stem in stems])
    return result


def process_job(settings: Settings, job: dict, worker_id: str, engine=None, isolated: bool = True, separator=None) -> None:
    with artifact_lease(settings, job["project_id"]):
        db = Database(settings.database_path, settings.max_job_attempts, settings=settings)
        if not db.owns_job(job["id"], worker_id):
            return
        _process_job(settings, job, worker_id, engine, isolated, separator)


def _process_job(settings: Settings, job: dict, worker_id: str, engine=None, isolated: bool = True, separator=None) -> None:
    if isolated and os.name == "posix":
        # Decoder descendants share this group, so cancellation stops them too.
        os.setsid()
    logger = logging.getLogger("score_worker")
    db = Database(settings.database_path, settings.max_job_attempts, settings=settings)
    directory = settings.data_dir / "projects" / job["project_id"]
    output = directory / job["id"]
    output.mkdir(exist_ok=True, mode=0o700)
    duration = None
    score_bundle = None
    project = db.get_project(job["project_id"])
    if project is None:
        return
    mixed_family = project["input_mode"] == "mixed" or project["parent_project_id"] is not None
    guard_stop = threading.Event()

    def check_limits() -> None:
        try:
            db.check_storage_usage(project["owner_id"])
        except QuotaError as exc:
            raise AnalysisError(str(exc)) from exc
        if not mixed_family:
            return
        if os.name == "posix":
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_bytes = peak if sys.platform == "darwin" else peak * 1024
            if peak_bytes > settings.separation_memory_mb * 1024 * 1024:
                raise AnalysisError("Mixed-recording analysis exceeded SEPARATION_MEMORY_MB. Use a shorter segment/clip or raise the memory limit on a suitable computer.")
        check_storage(settings, db, project)

    if isolated and os.name == "posix":
        parent_pid = os.getppid()
        def guard() -> None:
            while not guard_stop.wait(0.5):
                if os.getppid() != parent_pid or not db.owns_job(job["id"], worker_id):
                    # Avoid an orphan continuing inference after its supervisor
                    # dies, the lease expires, or the user cancels the attempt.
                    os.killpg(os.getpid(), signal.SIGKILL)
                if mixed_family or settings.authentication_required:
                    try:
                        check_limits()
                    except AnalysisError as exc:
                        db.finish(job, worker_id, error=str(exc))
                        os.killpg(os.getpid(), signal.SIGKILL)
        threading.Thread(target=guard, name="analysis-lease-guard", daemon=True).start()

    def stage(label: str) -> None:
        if not db.stage(job["id"], worker_id, label):
            raise LostLease()
        check_limits()

    try:
        stage("Validating and decoding selected audio")
        mixed = project["input_mode"] == "mixed"
        prepared = prepare_analysis_audio(directory / "source", output / "normalized.wav", settings,
                                          project["clip_start_seconds"], project["clip_end_seconds"],
                                          channels=2 if mixed else 1, sample_rate=44100 if mixed else 22050)
        duration = prepared.original_duration
        if not db.record_decoded(job, worker_id, duration, prepared.clip_start, prepared.clip_end):
            return
        if mixed:
            from .separation import SeparationEngine, file_digests
            selected_separator = separator if separator is not None else SeparationEngine(settings, checkpoint_dir=directory / ".analysis-checkpoints")
            stage("Separating available model stems")
            result = selected_separator.separate(output / "normalized.wav", output, stage_callback=stage)
            result = validate_separation_result(result, output, output / "normalized.wav", prepared)
            result["manifest"]["original_source_sha256"] = file_digests(directory / "source")["sha256"]
            result["manifest"]["resource_limits"] = {
                "timeout_seconds": settings.separation_timeout_seconds,
                "monitored_peak_rss_mb": settings.separation_memory_mb,
                "family_storage_bytes": settings.max_separation_storage_bytes}
            stage("Validating aligned stems and saving results")
            result["manifest"]["measured_resources"] = measured_resources()
            atomic_json(output / "separation-manifest.json", result["manifest"])
            check_limits()
            db.finish(job, worker_id, duration=duration, artifacts=["separation-manifest"], separation_bundle=result)
            return
        from .transcription_profiles import get_transcription_engine
        selected = engine if engine is not None else get_transcription_engine(settings, project, checkpoint_dir=directory / ".analysis-checkpoints")
        if hasattr(selected, "analyze"):
            result = selected.analyze(output / "normalized.wav", output, job["id"],
                                      project["cleanup"], stage_callback=stage)
            notes = result["notes"]
            if project["parent_project_id"] and (output / "manifest.json").is_file():
                manifest = json.loads((output / "manifest.json").read_text())
                manifest["source_context"] = {"parent_project_id": project["parent_project_id"],
                    "stem_category": project["stem_category"], "transcription_profile": project["transcription_profile"],
                    "source_origin_seconds": project["source_origin_seconds"], "arrangement": False,
                    "separation": db.separation(project["parent_project_id"])["manifest"]}
                atomic_json(output / "manifest.json", manifest)
            stage("Writing performance MIDI")
            write_performance_midi(notes, output / "score.mid")
            exports = ["raw-midi", "midi"]
            artifacts = [key for key, (name, _) in ARTIFACTS.items() if (output / name).is_file()]
            stage("Constructing canonical musical notation")
            try:
                exports += build_exports(notes, output, project["title"],
                    tempo_hint=project["tempo_hint"], time_signature_hint=project["time_signature_hint"],
                    instrument_hint=project["instrument_hint"], include_midi=False)
                score_path, xml_path = output / "canonical-score.json", output / "score.musicxml"
                if score_path.is_file() and xml_path.is_file():
                    score_bundle = {"score": json.loads(score_path.read_text()), "musicxml": xml_path.read_text()}
                elif (output / "manifest.json").is_file():
                    manifest = json.loads((output / "manifest.json").read_text())
                    manifest["notation_unavailable_reason"] = "Notation could not be constructed for the complete transcription. All detected notes and full-performance MIDI are retained; no audio section was omitted. Adjust musical settings or use the preserved MIDI."
                    atomic_json(output / "manifest.json", manifest)
            except Exception as exc:
                logger.exception("Optional notation failed; complete performance outputs retained for job %s", job["id"])
                manifest_path = output / "manifest.json"
                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text())
                    manifest["notation_unavailable_reason"] = (str(exc) if isinstance(exc, ValueError) else
                        "The complete transcription was retained, but notation construction failed. Adjust musical settings or export raw MIDI; no audio section was omitted.")
                    atomic_json(manifest_path, manifest)
        else:
            # Explicit injected test adapters only; the production path always
            # selects a real transcription adapter and never substitutes missing inference.
            if engine is None:
                raise AnalysisError("The configured engine does not implement durable analysis.")
            stage("Running explicitly injected test engine")
            notes = selected.transcribe(output / "normalized.wav")
            exports, artifacts = [], []
        stage("Saving transcription results")
        manifest_path = output / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text())
            manifest["measured_resources"] = measured_resources()
            atomic_json(manifest_path, manifest)
        db.finish(job, worker_id, notes=notes, duration=duration, exports=exports, artifacts=artifacts,
                  score_bundle=score_bundle)
    except LostLease:
        return
    except AnalysisError as exc:
        logger.exception("Analysis failed for job %s", job["id"])
        db.finish(job, worker_id, duration=duration, error=str(exc))
    except Exception:
        logger.exception("Unexpected failure for job %s", job["id"])
        db.finish(job, worker_id, duration=duration,
                  error="Analysis failed unexpectedly. Check the worker log and installed runtime, then retry within the attempt limit.")
    finally:
        guard_stop.set()

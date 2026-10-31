from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import math
import tempfile
import sys
import uuid
import wave
from pathlib import Path
from typing import Callable, Protocol

from .audio import AnalysisError
from .analysis_checkpoints import CheckpointStore, audio_sha256
from .notes import NoteEvent, cleanup_notes, cleanup_settings, raw_note_id
from .performance import write_midi_object


class TranscriptionEngine(Protocol):
    name: str

    def transcribe(self, audio: Path) -> list[NoteEvent]: ...


def package_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _plain(value):
    """Losslessly convert NumPy scalar/list containers to JSON-compatible values."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    return value


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w") as stream:
            json.dump(_plain(value), stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_npz(path: Path, output: dict) -> None:
    import numpy as np
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            np.savez_compressed(stream, **output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class BasicPitchEngine:
    """Real model inference with replaceable adapter and preserved raw artifacts.

    Stream upstream's overlapping model windows into disk-backed activation
    arrays. Every retained frame uses its actual input-window sample position;
    nominal FPS is not used to truncate the end of long recordings. Extract
    notes once globally, preserving sustained notes and repeated attacks across
    model/checkpoint boundaries. Failed attempts can reuse verified windows.
    """

    name = "basic-pitch"

    def __init__(self, runtime: str = "onnx", checkpoint_dir: Path | None = None) -> None:
        self.runtime = runtime
        self.checkpoint_dir = checkpoint_dir
        self._windowing = {}
        self._model = None
        self._model_path: Path | None = None

    def _load_model(self):
        if sys.version_info[:2] not in {(3, 10), (3, 11)}:
            raise AnalysisError("Basic Pitch requires the supported analysis environment: Python 3.11. Run backend/install_analysis.py inside a Python 3.11 virtual environment, restart the worker, then retry.")
        try:
            if self.runtime == "onnx":
                import onnxruntime
                onnxruntime.disable_telemetry_events()
            from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
            from basic_pitch.inference import Model
        except (ImportError, OSError) as exc:
            raise AnalysisError("Basic Pitch or its inference runtime is missing. Install backend/requirements-analysis.txt in a supported Python environment, restart the worker, then retry.") from exc
        try:
            if self._model is None:
                self._model_path = Path(build_icassp_2022_model_path(FilenameSuffix[self.runtime]))
                self._model = Model(self._model_path)
        except Exception as exc:
            raise AnalysisError("Basic Pitch model loading failed. Check the worker log and supported runtime versions, then retry.") from exc
        return self._model

    def _predict(self, audio: Path, output_dir: Path, stage: Callable[[str], None]):
        stage("Loading transcription model")
        model = self._load_model()
        try:
            import numpy as np
            import soundfile as sf
            from basic_pitch.constants import AUDIO_N_SAMPLES, AUDIO_SAMPLE_RATE, FFT_HOP, ANNOT_N_FRAMES
            from basic_pitch.note_creation import output_to_notes_polyphonic, get_pitch_bends, note_events_to_midi

            info = sf.info(str(audio))
            if info.samplerate != AUDIO_SAMPLE_RATE or info.channels != 1 or info.frames <= 0:
                raise AnalysisError("Basic Pitch needs normalized mono audio at 22,050 Hz.")
            overlap_frames = 30
            trim = overlap_frames // 2
            overlap_samples = overlap_frames * FFT_HOP
            hop = AUDIO_N_SAMPLES - overlap_samples
            core_frames = ANNOT_N_FRAMES - overlap_frames
            # Retained frames after overlap trimming cover [window*hop + j*FFT_HOP].
            # The last model window may start past EOF due to left context; it
            # has no retained input frames and must not invent trailing notes.
            windows = math.ceil(info.frames / hop)
            final_count = min(core_frames, math.ceil((info.frames - (windows - 1) * hop) / FFT_HOP))
            count = (windows - 1) * core_frames + final_count
            work = output_dir / ".activation-work"
            work.mkdir(exist_ok=True)
            projected_bytes = count * (88 + 88 + 264) * 4
            if shutil.disk_usage(work).free < projected_bytes + 64 * 1024 * 1024:
                raise AnalysisError("There is not enough private disk space for the complete activation timeline. Free storage and retry; no partial transcription was published.")
            output = {name: np.lib.format.open_memmap(work / f"{name}.npy", mode="w+", dtype="float32", shape=(count, bins))
                      for name, bins in (("note", 88), ("onset", 88), ("contour", 264))}
            model_hash = audio_sha256(self._model_path) if self._model_path and self._model_path.is_file() else None
            runtime_package = {"onnx": "onnxruntime", "coreml": "coremltools", "tf": "tensorflow", "tflite": "tflite-runtime"}[self.runtime]
            store = CheckpointStore(self.checkpoint_dir or output_dir / ".analysis-checkpoints", audio,
                       {"engine": self.name, "engine_version": importlib.metadata.version("basic-pitch"),
                        "runtime": self.runtime, "model_sha256": model_hash,
                        "runtime_version": importlib.metadata.version(runtime_package),
                        "window_samples": AUDIO_N_SAMPLES, "hop_samples": hop, "trim_frames": trim,
                        "mapping": "absolute-window-samples-v1"})
            shapes = {name: (core_frames, value.shape[1]) for name, value in output.items()}
            with sf.SoundFile(str(audio)) as source:
                for index in range(windows):
                    stage(f"Running Basic Pitch inference: window {index + 1} of {windows} · {index * hop / AUDIO_SAMPLE_RATE:.1f} seconds")
                    values = store.load(index, shapes)
                    if values is not None and not all(np.isfinite(value).all() for value in values.values()):
                        values = None
                    if values is None:
                        start = index * hop - overlap_samples // 2
                        left_padding = max(0, -start)
                        source.seek(max(0, start))
                        samples = source.read(max(0, min(AUDIO_N_SAMPLES - left_padding, info.frames - max(0, start))), dtype="float32")
                        window = np.zeros(AUDIO_N_SAMPLES, dtype="float32")
                        window[left_padding:left_padding + len(samples)] = samples
                        predicted = model.predict(window[None, :, None])
                        values = {name: np.asarray(predicted[name][0, trim:-trim], dtype="float32") for name in output}
                        if any(value.shape != shapes[name] or not np.isfinite(value).all() for name, value in values.items()):
                            raise AnalysisError("Basic Pitch returned incomplete or invalid model frames. No partial transcription was published.")
                        store.save(index, values)
                    length = min(core_frames, count - index * core_frames)
                    for name, value in output.items():
                        value[index * core_frames:index * core_frames + length] = values[name][:length]
            for value in output.values():
                value.flush()
            frame_index = np.arange(count)
            times = ((frame_index // core_frames) * hop + (frame_index % core_frames) * FFT_HOP) / AUDIO_SAMPLE_RATE
            self._windowing = {"implementation": "streamed upstream model.predict windows; disk-backed global activations",
                "sample_rate": AUDIO_SAMPLE_RATE, "model_window_samples": AUDIO_N_SAMPLES,
                "overlap_frames": overlap_frames, "fft_hop_samples": FFT_HOP,
                "window_hop_samples": hop, "retained_frames_per_window": core_frames,
                "windows": windows, "checkpoint_windows_reused": store.hits,
                "activation_frames": count, "covered_input_frames": info.frames,
                "coverage_intervals_seconds": [[index * hop / AUDIO_SAMPLE_RATE,
                    min(info.frames, (index + 1) * hop) / AUDIO_SAMPLE_RATE] for index in range(windows)],
                "last_activation_seconds": float(times[-1]),
                "frame_time_mapping": "(window_index * window_hop_samples + within_window_frame * fft_hop_samples) / sample_rate",
                "reconciliation": "trim model overlap; assemble complete sample-aligned global activation timeline; extract notes once globally, retaining repeated onsets and sustained notes",
                "normalized_audio_sha256": store.audio_sha256}
            stage("Preserving raw model output")
            _atomic_npz(output_dir / "raw-model-output.npz", output)
            stage("Extracting raw note events from complete activation timeline")
            frames = output_to_notes_polyphonic(output["note"], output["onset"], onset_thresh=.5,
                frame_thresh=.3, min_note_len=2, infer_onsets=True, min_freq=None,
                max_freq=None, melodia_trick=True)
            bent = get_pitch_bends(output["contour"], frames)
            events = [(float(times[n[0]]), float(times[n[1]]), n[2], n[3], n[4]) for n in bent]
            midi = note_events_to_midi(events, multiple_pitch_bends=False, midi_tempo=120)
            return output, midi, events, times
        except AnalysisError:
            shutil.rmtree(output_dir / ".activation-work", ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(output_dir / ".activation-work", ignore_errors=True)
            raise AnalysisError("Basic Pitch inference or note extraction failed. Completed model-window checkpoints are retained for retry; no partial transcription was published.") from exc

    def transcribe(self, audio: Path) -> list[NoteEvent]:
        """Compatibility method; durable worker processing should use analyze."""
        with tempfile.TemporaryDirectory(prefix="basic-pitch-") as temporary:
            return self.analyze(audio, Path(temporary), str(audio))["notes"]

    def analyze(self, audio: Path, output_dir: Path, namespace: str,
                cleanup: dict | None = None,
                stage_callback: Callable[[str], None] | None = None) -> dict:
        options = cleanup_settings(cleanup)
        stage = stage_callback or (lambda _: None)
        output_dir.mkdir(parents=True, exist_ok=True)
        output, raw_midi, events, frame_times = self._predict(audio, output_dir, stage)
        stage("Preserving raw note events and performance MIDI")
        raw_events = _plain(events)
        atomic_json(output_dir / "raw-note-events.json", {
            "schema_version": 1, "engine": self.name,
            "fields": ["start_seconds", "end_seconds", "midi_pitch", "model_amplitude", "pitch_bends"],
            "time_basis": "seconds relative to selected clip; unquantized",
            "score_semantics": "model amplitude activation; not a calibrated probability or accuracy percentage",
            "note_events": raw_events,
            "note_ids": [raw_note_id(namespace, index) for index in range(len(raw_events))],
        })
        write_midi_object(raw_midi, output_dir / "raw-performance.mid")
        stage("Cleaning detected notes")
        with wave.open(str(audio), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
        notes, audit = cleanup_notes(raw_events, namespace, options, output, frame_times, duration)
        stage("Saving editable performance notes")
        atomic_json(output_dir / "notes.json", {"schema_version": 1,
            "time_basis": "seconds relative to selected clip; unquantized", "notes": notes})
        model_hash = None
        if self._model_path and self._model_path.is_file():
            model_hash = hashlib.sha256(self._model_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": 1, "engine": self.name, "namespace": namespace,
            "clip_duration_seconds": duration,
            "normalized_audio_sha256": audio_sha256(audio),
            "engine_version": importlib.metadata.version("basic-pitch"),
            "runtime": self.runtime, "model_sha256": model_hash,
            "runtime_version": importlib.metadata.version("onnxruntime") if self.runtime == "onnx" else None,
            "time_basis": "seconds relative to selected clip; unquantized",
            "windowing": self._windowing,
            "extraction": {"onset_threshold": 0.5, "frame_threshold": 0.3,
                "minimum_note_frames": 2, "melodia_trick": True, "infer_onsets": True},
            "cleanup": audit, "raw_note_count": len(raw_events), "normalized_note_count": len(notes),
            "model_score_semantics": "mean model note activation for a raw event; merged notes use the maximum source activation; not calibrated accuracy",
            "pitch_bend_semantics": "original pitch bends are retained in raw-note-events.json and upstream raw-performance.mid where MIDI permits; editable normalized notes currently contain discrete pitches only",
            "velocity_semantics": "MIDI velocity derived by rounding model amplitude * 127; editable and not measured playing force",
            "original_timing_semantics": "earliest source event snapshot; merged source events remain in raw-note-events.json via source_note_ids",
        }
        atomic_json(output_dir / "manifest.json", manifest)
        shutil.rmtree(output_dir / ".activation-work", ignore_errors=True)
        return {"notes": notes, "manifest": manifest, "artifacts": {
            "raw_model_output": "raw-model-output.npz", "raw_note_events": "raw-note-events.json",
            "raw_midi": "raw-performance.mid", "notes": "notes.json", "manifest": "manifest.json"}}


def capabilities(ffmpeg_binary: str, runtime: str = "onnx") -> dict[str, bool]:
    return {"ffmpeg": bool(shutil.which(ffmpeg_binary)),
            "transcription": package_available("basic_pitch") and package_available({"onnx": "onnxruntime", "coreml": "coremltools", "tf": "tensorflow", "tflite": "tflite_runtime"}[runtime]) and sys.version_info[:2] in {(3, 10), (3, 11)},
            "notation": package_available("music21")}

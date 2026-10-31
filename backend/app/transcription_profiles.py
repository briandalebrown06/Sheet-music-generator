"""Replaceable, measured part transcription; never arrangement generation.

Solo and experimental polyphonic stems retain Basic Pitch. Vocal melody and
bass use pYIN's monophonic F0 estimator with inspectable raw frames and a
separate, deterministic vibrato/fragmentation cleanup pass.
"""
from __future__ import annotations

import importlib.metadata
import math
from pathlib import Path
from typing import Callable

from .audio import AnalysisError
from .analysis_checkpoints import CheckpointStore, audio_sha256
from .engine import BasicPitchEngine, _atomic_npz, atomic_json
from .notes import cleanup_settings, detected_notes
from .performance import write_performance_midi

PROFILES = {
    "vocals": {"fmin_midi": 36, "fmax_midi": 96, "frame_length": 2048,
               "hysteresis_semitones": .75, "transition_seconds": .05, "median_seconds": .07},
    "bass": {"fmin_midi": 21, "fmax_midi": 60, "frame_length": 4096,
             "hysteresis_semitones": .65, "transition_seconds": .035, "median_seconds": .045},
}


def frame_events(pitches, voiced, probabilities, rms, frame_seconds: float, duration: float,
                 *, hysteresis: float = .5, transition_seconds: float = 0,
                 median_seconds: float = 0) -> list[list]:
    """Segment only observed voiced frames; retain silence and rearticulations.

    A pitch change must exceed the semitone hysteresis for a sustained interval.
    Small within-note vibrato remains one discrete note. No interpolated F0 or
    missing accompaniment is invented. RMS dips split repeated same-pitch notes.
    """
    import numpy as np
    from scipy.ndimage import median_filter

    pitches = np.asarray(pitches, dtype=float).copy()
    voiced = np.asarray(voiced, dtype=bool) & np.isfinite(pitches)
    if median_seconds:
        size = max(1, round(median_seconds / frame_seconds)) | 1
        # Smooth within each voiced island; never smear pitch across a silence.
        edges = np.flatnonzero(np.diff(np.r_[False, voiced, False]))
        for left, right in zip(edges[::2], edges[1::2]):
            pitches[left:right] = median_filter(pitches[left:right], size=size, mode="nearest")
    required = max(1, math.ceil(transition_seconds / frame_seconds))
    events = []
    start = None
    pitch = None
    pending_start = None
    pending_pitch = None
    peak = 0.0
    minimum_since_peak = 0.0

    def finish(end_index):
        if start is None or end_index <= start:
            return
        left = max(0, start * frame_seconds - frame_seconds / 2)
        right = min(duration, end_index * frame_seconds - frame_seconds / 2)
        if right > left:
            score = float(np.mean(probabilities[start:end_index]))
            events.append([left, right, int(pitch), score, []])

    for i in range(len(pitches) + 1):
        valid = i < len(pitches) and voiced[i]
        if not valid:
            finish(i)
            start = pitch = pending_start = pending_pitch = None
            peak = minimum_since_peak = 0.0
            continue
        candidate = int(np.clip(np.rint(pitches[i]), 0, 127))
        if start is None:
            start, pitch = i, candidate
            peak = minimum_since_peak = float(rms[i])
            continue
        # An audible reattack after a deep energy dip is separate from vibrato.
        level = float(rms[i])
        reattack = (i - start) * frame_seconds >= .1 and minimum_since_peak < peak * .35 and level > max(peak * .65, minimum_since_peak * 2.5)
        if reattack:
            finish(i)
            start, pitch = i, candidate
            peak = minimum_since_peak = level
            pending_start = pending_pitch = None
            continue
        minimum_since_peak = min(minimum_since_peak, level)
        if level > peak:
            peak, minimum_since_peak = level, level
        if candidate != pitch and abs(pitches[i] - pitch) > hysteresis:
            if candidate != pending_pitch:
                pending_pitch, pending_start = candidate, i
            if i - pending_start + 1 >= required:
                finish(pending_start)
                start, pitch = pending_start, pending_pitch
                peak = minimum_since_peak = level
                pending_start = pending_pitch = None
        else:
            pending_start = pending_pitch = None
    return events


class PyinPartEngine:
    """One predominant pitched line, not all voices in a separated vocal stem."""
    def __init__(self, profile: str, checkpoint_dir: Path | None = None):
        if profile not in PROFILES:
            raise ValueError("pYIN part profile must be vocals or bass.")
        self.profile = profile
        self.checkpoint_dir = checkpoint_dir
        self.name = f"pyin-{profile}"

    def analyze(self, audio: Path, output_dir: Path, namespace: str, cleanup: dict | None = None,
                stage_callback: Callable[[str], None] | None = None) -> dict:
        import numpy as np
        try:
            import librosa
        except ImportError as exc:
            raise AnalysisError("Vocal/bass transcription requires librosa in the analysis environment.") from exc
        options = cleanup_settings(cleanup)
        settings = PROFILES[self.profile]
        stage = stage_callback or (lambda _: None)
        output_dir.mkdir(parents=True, exist_ok=True)
        stage(f"Loading selected {self.profile} stem for monophonic pitch tracking")
        import soundfile as sf
        info = sf.info(str(audio))
        if info.samplerate != 22050 or info.channels != 1:
            raise AnalysisError("Pitch tracking needs normalized mono audio at 22,050 Hz.")
        sr, total_samples = info.samplerate, info.frames
        duration = total_samples / sr
        hop = 256
        frame_seconds = hop / sr
        count = max(1, math.ceil(total_samples / hop))
        f0 = np.full(count, np.nan)
        flags = np.zeros(count, dtype=bool)
        probability = np.zeros(count)
        rms = np.zeros(count)
        core = math.ceil(30 * sr / hop)
        overlap = math.ceil(.5 * sr / hop)
        store = CheckpointStore(self.checkpoint_dir or output_dir / ".analysis-checkpoints", audio,
            {"engine": self.name, "version": importlib.metadata.version("librosa"), "settings": settings,
             "numpy_version": importlib.metadata.version("numpy"), "scipy_version": importlib.metadata.version("scipy"),
             "sample_rate": sr, "hop": hop, "core": core, "overlap": overlap})
        # Read only each bounded context, never the full hour into RAM. The
        # shared hop lattice and one global segmentation preserve boundaries.
        with sf.SoundFile(str(audio)) as source:
            for index, left in enumerate(range(0, count, core)):
                right = min(count, left + core)
                context_left, context_right = max(0, left - overlap), min(count, right + overlap)
                stage(f"Tracking {self.profile} pitch: audio {left * frame_seconds:.1f}–{min(duration, right * frame_seconds):.1f} seconds")
                length = right - left
                values = store.load(index, {key: (length,) for key in ("f0", "voiced", "probability", "rms")})
                if values is None:
                    source.seek(context_left * hop)
                    part = source.read(min(total_samples, context_right * hop) - context_left * hop, dtype="float32")
                    pitches, voiced, prob = librosa.pyin(part, sr=sr,
                        fmin=librosa.midi_to_hz(settings["fmin_midi"]),
                        fmax=librosa.midi_to_hz(settings["fmax_midi"]),
                        frame_length=settings["frame_length"], hop_length=hop,
                        resolution=.1, n_thresholds=100, center=True, fill_na=np.nan)
                    levels = librosa.feature.rms(y=part, frame_length=settings["frame_length"], hop_length=hop, center=True)[0]
                    offset = left - context_left
                    values = {key: value[offset:offset + length] for key, value in
                              (("f0", pitches), ("voiced", voiced), ("probability", prob), ("rms", levels))}
                    if any(len(value) != length for value in values.values()):
                        raise AnalysisError("Pitch tracking did not cover the complete audio window. No partial result was published.")
                    store.save(index, values)
                f0[left:right], flags[left:right] = values["f0"], values["voiced"]
                probability[left:right], rms[left:right] = values["probability"], values["rms"]
        floor = max(10 ** (-70 / 20), float(np.max(rms, initial=0)) * .04)
        audible = flags & (probability >= .15) & (rms >= floor)
        midi = np.full(count, np.nan)
        finite = np.isfinite(f0) & (f0 > 0)
        midi[finite] = librosa.hz_to_midi(f0[finite])
        stage("Preserving raw pitch, voicing and amplitude frames")
        _atomic_npz(output_dir / "raw-model-output.npz", {"f0_hz": f0, "voiced_flag": flags,
                    "voiced_probability": probability, "rms": rms, "frame_times": np.arange(count) * frame_seconds})
        raw_events = frame_events(midi, audible, probability, rms, frame_seconds, duration)
        raw_notes = detected_notes(raw_events, namespace, duration)
        atomic_json(output_dir / "raw-note-events.json", {"schema_version": 1, "engine": self.name,
            "fields": ["start_seconds", "end_seconds", "midi_pitch", "mean_voicing_score", "pitch_bends"],
            "time_basis": "seconds relative to selected stem; unquantized",
            "score_semantics": "mean pYIN frame voicing probability; not calibrated note accuracy",
            "note_events": raw_events, "note_ids": [n["id"] for n in raw_notes]})
        write_performance_midi(raw_notes, output_dir / "raw-performance.mid")
        stage("Stabilizing vibrato and removing short pitch fragments")
        stable = frame_events(midi, audible, probability, rms, frame_seconds, duration,
                    hysteresis=settings["hysteresis_semitones"], transition_seconds=settings["transition_seconds"],
                    median_seconds=settings["median_seconds"])
        clean = []
        removed = 0
        for event in stable:
            if event[1] - event[0] < options["minimum_note_seconds"]:
                removed += 1
                continue
            # Bind the stabilized event to overlapping, actually observed raw
            # frames/events. One raw identity may contribute to multiple changes;
            # keep each accepted ID unique and retain all source lineage.
            sources = [n for n in raw_notes if n["start"] < event[1] and n["end"] > event[0]]
            if not sources:
                continue
            pitch_sources = [n for n in sources if n["pitch"] == event[2]]
            anchor = (pitch_sources or sources)[0]
            used = {n["id"] for n in clean}
            identifier = next((n["id"] for n in pitch_sources + sources if n["id"] not in used), None)
            if identifier is None:
                from uuid import NAMESPACE_URL, uuid5
                identifier = str(uuid5(NAMESPACE_URL, f"{namespace}:stable:{event[0]}:{event[2]}"))
            clean.append({**anchor, "id": identifier, "pitch": event[2], "start": event[0], "end": event[1],
                          "model_score": event[3], "velocity": max(1, min(127, round(event[3] * 127))),
                          "part_id": self.profile, "source_note_ids": [n["id"] for n in sources]})
        atomic_json(output_dir / "notes.json", {"schema_version": 1, "time_basis": "stem-local performance seconds; unquantized", "notes": clean})
        manifest = {"schema_version": 1, "engine": self.name, "engine_version": importlib.metadata.version("librosa"),
            "namespace": namespace, "profile": self.profile, "clip_duration_seconds": duration,
            "normalized_audio_sha256": audio_sha256(audio),
            "model_weights": None, "algorithm": "librosa.pyin", "code_license": "ISC (librosa); no learned checkpoint",
            "sample_rate": sr, "hop_samples": hop, "settings": settings,
            "chunking": {"core_seconds": core * frame_seconds, "context_seconds": overlap * frame_seconds,
                         "covered_input_frames": total_samples, "checkpoint_windows_reused": store.hits,
                         "audio_loading": "bounded soundfile windows; full-recording frame segmentation",
                         "reconciliation": "shared hop lattice; overlap context discarded; full global frame timeline segmented once"},
            "voicing": {"minimum_probability": .15, "rms_absolute_floor_dbfs": -70, "rms_relative_floor": .04},
            "cleanup": {"settings": options, "removed_short_stabilized_events": removed,
                        "rule": "voiced-island median, persistent semitone hysteresis and deep RMS reattack detection; no silence bridging"},
            "raw_note_count": len(raw_notes), "normalized_note_count": len(clean),
            "model_score_semantics": "pYIN voicing score, not calibrated note accuracy; MIDI velocity is a voicing-derived audition proxy",
            "limitations": "One predominant pitch per frame. Vocal harmonies, breath, vibrato wider than the configured hysteresis, portamento and separation leakage can cause errors. No accompaniment is generated.",
            "result_kind": "transcription"}
        atomic_json(output_dir / "manifest.json", manifest)
        return {"notes": clean, "manifest": manifest}


def get_transcription_engine(settings, project: dict, checkpoint_dir: Path | None = None):
    profile = project.get("transcription_profile") or "solo"
    if profile in PROFILES:
        return PyinPartEngine(profile, checkpoint_dir=checkpoint_dir)
    if profile in {"solo", "other", "basic-pitch"}:
        return BasicPitchEngine(settings.basic_pitch_runtime, checkpoint_dir=checkpoint_dir)
    raise AnalysisError(f"No pitched transcription adapter supports {profile!r}. Drum notation is unsupported.")

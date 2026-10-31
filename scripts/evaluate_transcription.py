#!/usr/bin/env python3
"""Reproducible real-model evaluation on generated, explicitly labeled audio.

From repository root:
    .venv/bin/python scripts/evaluate_transcription.py

Fixtures/expected notes are authored in evaluation/fixtures.json. Generated WAVs
and full model artifacts stay under ignored evaluation/generated/. Summary JSON
and Markdown record actual results; no model output is replaced or fabricated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from scipy.optimize import linear_sum_assignment

from backend.app.engine import BasicPitchEngine


EVALUATION_NAMESPACE = uuid.UUID("2459c119-3fa2-4cb4-a4d1-3cddc404090a")


def synthesize(fixture: dict, spec: dict, path: Path) -> None:
    rate = spec["sample_rate"]
    audio = np.zeros(round(fixture["duration_seconds"] * rate), dtype=np.float64)
    settings = spec["synthesis"]
    for note in fixture["notes"]:
        first = round(note["start"] * rate)
        last = round(note["end"] * rate)
        times = np.arange(last - first, dtype=np.float64) / rate
        duration = note["end"] - note["start"]
        envelope = np.minimum(1, times / settings["attack_seconds"])
        envelope *= np.minimum(1, np.maximum(0, duration - times) / settings["release_seconds"])
        frequency = 440 * 2 ** ((note["pitch"] - 69) / 12)
        signal = sum(weight * np.sin(2 * np.pi * frequency * harmonic * times)
                     for harmonic, weight in enumerate(settings["harmonics"], start=1))
        audio[first:last] += settings["amplitude_per_note"] * envelope * signal
    if np.max(np.abs(audio), initial=0) >= 1:
        raise ValueError(f"Fixture {fixture['id']} clips; adjust the authored manifest, not inference outputs")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.rint(audio * 32767).astype("<i2").tobytes())


def match_notes(expected: list[dict], predicted: list[dict], tolerance: float, exact_pitch: bool) -> list[tuple[int, int]]:
    """One-to-one minimum-cost assignment; no ground truth is used by inference.

    Cardinality dominates the cost. Valid-pair costs combine onset error with
    small pitch-distance and offset penalties; chord correspondence is ambiguous. Unmatched notes
    remain false positives/negatives rather than being forced into a match.
    """
    if not expected or not predicted:
        return []
    costs = np.full((len(expected), len(predicted)), 1_000_000.0)
    for i, reference in enumerate(expected):
        for j, estimate in enumerate(predicted):
            onset_error = abs(estimate["start"] - reference["start"])
            pitch_error = abs(estimate["pitch"] - reference["pitch"])
            if onset_error <= tolerance and (not exact_pitch or pitch_error == 0):
                costs[i, j] = onset_error + 0.0001 * pitch_error + 0.000001 * abs(estimate["end"] - reference["end"])
    rows, columns = linear_sum_assignment(costs)
    return [(int(i), int(j)) for i, j in zip(rows, columns) if costs[i, j] < 1_000_000]


def metrics(expected: list[dict], predicted: list[dict], tolerance: float) -> dict:
    matches = match_notes(expected, predicted, tolerance, exact_pitch=True)
    tp = len(matches)
    precision = tp / len(predicted) if predicted else (0.0 if expected else None)
    recall = tp / len(expected) if expected else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else
          (0.0 if expected else None))
    onset = [abs(predicted[j]["start"] - expected[i]["start"]) for i, j in matches]
    offset = [abs(predicted[j]["end"] - expected[i]["end"]) for i, j in matches]
    # Wider onset-gated pitch diagnostic explicitly includes wrong-pitch matches.
    pitch_pairs = match_notes(expected, predicted, 0.15, exact_pitch=False)
    pitch_errors = [abs(predicted[j]["pitch"] - expected[i]["pitch"]) for i, j in pitch_pairs]
    return {
        "expected_notes": len(expected), "predicted_notes": len(predicted),
        "matched_exact_pitch_and_onset": tp, "false_positive_notes": len(predicted) - tp,
        "missed_notes": len(expected) - tp, "precision": precision, "recall": recall, "f1": f1,
        "onset_mae_seconds": float(np.mean(onset)) if onset else None,
        "offset_mae_seconds": float(np.mean(offset)) if offset else None,
        "onset_max_error_seconds": max(onset) if onset else None,
        "offset_max_error_seconds": max(offset) if offset else None,
        "pitch_diagnostic_pairs": len(pitch_pairs),
        "pitch_mae_semitones_within_150ms": float(np.mean(pitch_errors)) if pitch_errors else None,
        "octave_errors_within_150ms": sum(error > 0 and error % 12 == 0 for error in pitch_errors),
    }


def aggregate(rows: list[dict], key: str) -> dict:
    results = [row[key] for row in rows]
    total_expected = sum(row["expected_notes"] for row in results)
    total_predicted = sum(row["predicted_notes"] for row in results)
    tp = sum(row["matched_exact_pitch_and_onset"] for row in results)
    precision = tp / total_predicted if total_predicted else 0
    recall = tp / total_expected if total_expected else 0
    weighted = lambda field: (
        sum(row[field] * row["matched_exact_pitch_and_onset"] for row in results if row[field] is not None) / tp if tp else None
    )
    return {"expected_notes": total_expected, "predicted_notes": total_predicted,
            "matched_exact_pitch_and_onset": tp, "false_positive_notes": total_predicted - tp,
            "missed_notes": total_expected - tp, "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
            "onset_mae_seconds": weighted("onset_mae_seconds"),
            "offset_mae_seconds": weighted("offset_mae_seconds")}


def number(value, scale=1):
    return "—" if value is None else f"{value * scale:.3f}"


def write_report(report: dict, path: Path) -> None:
    lines = [
        "# Synthetic transcription evaluation", "",
        f"Run: {report['run_at_utc']}. Engine: Spotify Basic Pitch {report['engine_version']}, explicit ONNX runtime.", "",
        "This is a small deterministic test of the real model on generated additive-synthesis audio. It is **not** an accuracy claim for voices, physical instruments, recordings, or commercial songs.", "",
        "## Reproduce", "", "```sh", ".venv/bin/python scripts/evaluate_transcription.py", "```", "",
        "The fixed reference notes and synthesis settings are in `evaluation/fixtures.json`. The script generates PCM WAV inputs, runs the installed production adapter, and preserves actual raw outputs plus cleaned notes in ignored `evaluation/generated/`. The summary is `evaluation/results.json`. No output is injected into the model or substituted for its result.", "",
        "## Matching and timing definitions", "",
        f"A true positive requires exact MIDI pitch and an onset within **{report['onset_tolerance_seconds'] * 1000:.0f} ms**, with one-to-one minimum-cost matching. Offset is not a matching gate: offset errors remain visible. Precision/recall/F1 use note counts, not frame classification. Timing MAE is computed only on those true-positive pairs; missed notes do not disappear from recall. Zero-reference silence has no meaningful recall or F1 and reports false-positive notes instead.", "",
        "A supplementary pitch diagnostic pairs notes within 150 ms regardless of pitch and reports mean absolute semitone error. Assignment minimizes onset error with small pitch-distance and offset penalties; chord correspondence can be ambiguous. This diagnostic complements, rather than replaces, exact-pitch precision and recall.", "",
        "Boundaries are authored starts/ends of attack/release envelopes. Attack/release durations are 20/40 ms, so the measured model boundaries need not equal perceptual onset/offset. MIDI timing remains in seconds; no score quantization is applied for evaluation.", "",
        "## Cleaned-note results", "",
        "| Fixture | Reference / predicted / matched | Precision | Recall | F1 | Onset MAE (ms) | Offset MAE (ms) | Pitch MAE (semitones, 150 ms diagnostic) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["fixtures"]:
        m = row["cleaned"]
        label = row["id"] + (" (supplementary)" if row["supplementary"] else "")
        lines.append(f"| {label} | {m['expected_notes']} / {m['predicted_notes']} / {m['matched_exact_pitch_and_onset']} | {number(m['precision'])} | {number(m['recall'])} | {number(m['f1'])} | {number(m['onset_mae_seconds'], 1000)} | {number(m['offset_mae_seconds'], 1000)} | {number(m['pitch_mae_semitones_within_150ms'])} |")
    total = report["primary_aggregate_cleaned"]
    lines += ["", f"Primary five-fixture aggregate: {total['matched_exact_pitch_and_onset']} exact-pitch/onset matches from {total['expected_notes']} expected and {total['predicted_notes']} predicted notes; precision {number(total['precision'])}, recall {number(total['recall'])}, F1 {number(total['f1'])}. Matched-note onset MAE {number(total['onset_mae_seconds'], 1000)} ms, offset MAE {number(total['offset_mae_seconds'], 1000)} ms.", "", "## Raw versus cleanup", "", "| Fixture | Raw notes | Cleaned notes | Raw F1 | Cleaned F1 |", "| --- | --- | --- | --- | --- |"]
    for row in report["fixtures"]:
        lines.append(f"| {row['id']} | {row['raw']['predicted_notes']} | {row['cleaned']['predicted_notes']} | {number(row['raw']['f1'])} | {number(row['cleaned']['f1'])} |")
    lines += ["", f"Cleanup settings were fixed before the run: `{json.dumps(report['cleanup'], sort_keys=True)}`. Model thresholds and provenance are recorded in each model manifest. No fixture-specific threshold tuning was used.", "", "## Interpretation and limitations", "",
        "- The 20-second sustained note crosses the model's internal overlapping windows. It tests retained global timing and fragmentation; the application uses upstream window assembly and global note extraction rather than independent chunk stitching.",
        "- Silence is run directly through the real model to count false positives. The production worker rejects a silent selected clip before inference; this direct engine evaluation does not bypass that product input check. Upstream note extraction emitted a divide warning on all-zero activations, then completed with zero events.",
        "- A correct synthetic pitch does not establish correct instrumentation, rhythm, key, meter, articulation, or engraving. No acoustic recordings, background noise, rubato, drums, voice formants, or source separation are represented.",
        "- Model score/activation values are not calibrated confidence or accuracy percentages. Reported precision/recall come from these known synthetic references only.",
        "- The generated audio is original procedural test material described entirely by the checked-in manifest and generator. No licensed commercial recording was used. Outputs and input hashes are recorded for reproducibility.",
        "", "## Tested environment", "", f"- Python: `{report['python_version']}`", f"- Platform: `{report['platform']}`", f"- ONNX Runtime: `{report['onnxruntime_version']}`", f"- NumPy: `{report['numpy_version']}`", f"- Fixture manifest SHA-256: `{report['fixture_manifest_sha256']}`", f"- Generator SHA-256: `{report['generator_sha256']}`", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation/generated")
    parser.add_argument("--summary", type=Path, default=ROOT / "evaluation/results.json")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/TRANSCRIPTION_EVALUATION.md")
    args = parser.parse_args()
    manifest_path = ROOT / "evaluation/fixtures.json"
    spec = json.loads(manifest_path.read_text())
    cleanup = {"minimum_note_seconds": 0.08, "merge_gap_seconds": 0.03}
    tolerance = 0.05
    engine = BasicPitchEngine("onnx")
    rows = []
    for fixture in spec["fixtures"]:
        directory = args.output / fixture["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "DEVELOPMENT-FIXTURE.wav"
        synthesize(fixture, spec, path)
        began = time.perf_counter()
        result = engine.analyze(path, directory, str(uuid.uuid5(EVALUATION_NAMESPACE, fixture["id"])), cleanup=cleanup)
        elapsed = time.perf_counter() - began
        raw_document = json.loads((directory / "raw-note-events.json").read_text())
        raw_events = raw_document if isinstance(raw_document, list) else raw_document["note_events"]
        # Preserve verbatim engine event fields; adapt only for metric field names.
        if raw_events and isinstance(raw_events[0], (list, tuple)):
            raw_notes = [{"start": float(event[0]), "end": float(event[1]), "pitch": int(event[2])} for event in raw_events]
        else:
            raw_notes = raw_events
        row = {"id": fixture["id"], "supplementary": fixture.get("supplementary", False),
               "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "duration_seconds": fixture["duration_seconds"], "elapsed_seconds": round(elapsed, 4),
               "raw": metrics(fixture["notes"], raw_notes, tolerance),
               "cleaned": metrics(fixture["notes"], result["notes"], tolerance),
               "predicted_notes": result["notes"], "manifest": result["manifest"]}
        rows.append(row)
        print(f"{fixture['id']}: expected={len(fixture['notes'])}, raw={len(raw_notes)}, cleaned={len(result['notes'])}, F1={number(row['cleaned']['f1'])}", flush=True)
    import importlib.metadata
    primary = [row for row in rows if not row["supplementary"]]
    report = {"run_at_utc": datetime.now(timezone.utc).isoformat(), "onset_tolerance_seconds": tolerance,
              "cleanup": cleanup, "python_version": platform.python_version(), "platform": platform.platform(),
              "engine_version": importlib.metadata.version("basic-pitch"),
              "onnxruntime_version": importlib.metadata.version("onnxruntime"), "numpy_version": np.__version__,
              "fixture_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "primary_aggregate_raw": aggregate(primary, "raw"), "primary_aggregate_cleaned": aggregate(primary, "cleaned"),
              "fixtures": rows}
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    write_report(report, args.report)
    print(f"Saved {args.summary} and {args.report}", flush=True)


if __name__ == "__main__":
    main()

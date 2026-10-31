# Synthetic transcription evaluation

Run: 2026-10-27T19:03:37.617498+00:00. Engine: Spotify Basic Pitch 0.4.0, explicit ONNX runtime.

This is a small deterministic test of the real model on generated additive-synthesis audio. It is **not** an accuracy claim for voices, physical instruments, recordings, or commercial songs.

## Reproduce

```sh
.venv/bin/python scripts/evaluate_transcription.py
```

The fixed reference notes and synthesis settings are in `evaluation/fixtures.json`. The script generates PCM WAV inputs, runs the installed production adapter, and preserves actual raw outputs plus cleaned notes in ignored `evaluation/generated/`. The summary is `evaluation/results.json`. No output is injected into the model or substituted for its result.

## Matching and timing definitions

A true positive requires exact MIDI pitch and an onset within **50 ms**, with one-to-one minimum-cost matching. Offset is not a matching gate: offset errors remain visible. Precision/recall/F1 use note counts, not frame classification. Timing MAE is computed only on those true-positive pairs; missed notes do not disappear from recall. Zero-reference silence has no meaningful recall or F1 and reports false-positive notes instead.

A supplementary pitch diagnostic pairs notes within 150 ms regardless of pitch and reports mean absolute semitone error. Assignment minimizes onset error with small pitch-distance and offset penalties; chord correspondence can be ambiguous. This diagnostic complements, rather than replaces, exact-pitch precision and recall.

Boundaries are authored starts/ends of attack/release envelopes. Attack/release durations are 20/40 ms, so the measured model boundaries need not equal perceptual onset/offset. MIDI timing remains in seconds; no score quantization is applied for evaluation.

## Cleaned-note results

| Fixture | Reference / predicted / matched | Precision | Recall | F1 | Onset MAE (ms) | Offset MAE (ms) | Pitch MAE (semitones, 150 ms diagnostic) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| single_note_melody | 4 / 5 / 4 | 0.800 | 1.000 | 0.889 | 3.288 | 44.447 | 0.000 |
| sustained_notes | 2 / 3 / 2 | 0.667 | 1.000 | 0.800 | 3.620 | 44.640 | 0.000 |
| repeated_notes | 6 / 8 / 6 | 0.750 | 1.000 | 0.857 | 8.519 | 42.890 | 0.000 |
| simple_chords | 6 / 10 / 6 | 0.600 | 1.000 | 0.750 | 5.910 | 28.989 | 0.000 |
| silence | 0 / 0 / 0 | — | — | — | — | — | — |
| window_boundary_sustain (supplementary) | 1 / 2 / 1 | 0.500 | 1.000 | 0.667 | 0.771 | 51.801 | 0.000 |

Primary five-fixture aggregate: 18 exact-pitch/onset matches from 18 expected and 26 predicted notes; precision 0.692, recall 1.000, F1 0.818. Matched-note onset MAE 5.942 ms, offset MAE 38.797 ms.

## Raw versus cleanup

| Fixture | Raw notes | Cleaned notes | Raw F1 | Cleaned F1 |
| --- | --- | --- | --- | --- |
| single_note_melody | 5 | 5 | 0.889 | 0.889 |
| sustained_notes | 3 | 3 | 0.800 | 0.800 |
| repeated_notes | 11 | 8 | 0.706 | 0.857 |
| simple_chords | 11 | 10 | 0.706 | 0.750 |
| silence | 0 | 0 | — | — |
| window_boundary_sustain | 2 | 2 | 0.667 | 0.667 |

Cleanup settings were fixed before the run: `{"merge_gap_seconds": 0.03, "minimum_note_seconds": 0.08}`. Model thresholds and provenance are recorded in each model manifest. No fixture-specific threshold tuning was used.

## Interpretation and limitations

- The 20-second sustained note crosses the model's internal overlapping windows. It tests retained global timing and fragmentation; the application uses upstream window assembly and global note extraction rather than independent chunk stitching.
- Silence is run directly through the real model to count false positives. The production worker rejects a silent selected clip before inference; this direct engine evaluation does not bypass that product input check. Upstream note extraction emitted a divide warning on all-zero activations, then completed with zero events.
- A correct synthetic pitch does not establish correct instrumentation, rhythm, key, meter, articulation, or engraving. No acoustic recordings, background noise, rubato, drums, voice formants, or source separation are represented.
- Model score/activation values are not calibrated confidence or accuracy percentages. Reported precision/recall come from these known synthetic references only.
- The generated audio is original procedural test material described entirely by the checked-in manifest and generator. No licensed commercial recording was used. Outputs and input hashes are recorded for reproducibility.

## Tested environment

- Python: `3.11.18`
- Platform: `macOS-27.0.1-arm64-arm-64bit`
- ONNX Runtime: `1.31.1`
- NumPy: `1.26.4`
- Fixture manifest SHA-256: `b7371754be16b2f254e664dad471e2142ac0ffde5e616cd22abdc9c2166fc78b`
- Generator SHA-256: `005a1a58c358db5cc23ba5c8cb497756fa8c960c52f058ec6df2542e5f9b4cba`

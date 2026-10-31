# Mixed-recording evaluation

Actual run: 2026-10-27T21:16:15.126655+00:00. Original procedural CC0 audio; no commercial recordings or injected detections.

## Method

The 12-second stereo fixture combines separately known synthetic vowel-like vibrato, bass, struck chords and seeded-noise percussion. Production Open-Unmix UMX-HQ separates the mixture without seeing the references. Its 10-second windows with 25% overlap are assembled on original sample positions. All four outputs have exactly 529,200 stereo frames at 44,100 Hz; no alignment search or delay correction is used in these measurements.

SI-SDR compares each estimate with its known target up to one scalar gain; higher is better. Improvement is relative to the unseparated mixture. A separate least-squares projection onto all four known sources estimates a leakage proxy: energy attributable to non-target source projections divided by summed projection energy. Sources are not orthogonal and nonlinear separation artifacts remain in the residual, so this is not a calibrated isolation or accuracy percentage. The unexplained residual fraction reports estimate energy not captured by that linear projection. Reference waveform shapes, not only note pitches, enter these metrics.

| Category | SI-SDR dB | Improvement dB | Projection target/interference dB | Leakage proxy (fraction) | Unexplained residual (fraction) |
| --- | ---: | ---: | ---: | ---: | ---: |
| vocals | 29.149 | 27.108 | 48.157 | 0.000015 | 0.001200 |
| bass | 16.904 | 20.823 | 25.199 | 0.003012 | 0.017036 |
| other | 13.904 | 25.509 | 31.120 | 0.000772 | 0.038368 |
| drums | 7.842 | 21.883 | 22.105 | 0.006121 | 0.135887 |

## Actual detected notes

Exact MIDI pitch and onset within 100 ms define a one-to-one match. Precision, recall and F1 count notes; missed and extra notes remain errors. Onset/offset MAE uses only matched notes. Offset is not a matching criterion. The comparison with transcription of each clean reference helps separate pitch-estimator limitations from separation damage; references never affect production inference. Timing remains unquantized performance seconds.

| Part / input | Expected / predicted / matched | Precision | Recall | F1 | Onset MAE ms | Offset MAE ms | Pitch MAE semitones (150 ms pairs) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| vocals / reference | 8 / 8 / 8 | 1.000 | 1.000 | 1.000 | 30.737 | 33.685 | 0.000 |
| vocals / separated | 8 / 8 / 8 | 1.000 | 1.000 | 1.000 | 30.737 | 33.685 | 0.000 |
| bass / reference | 6 / 6 / 6 | 1.000 | 1.000 | 1.000 | 15.253 | 41.602 | 0.000 |
| bass / separated | 6 / 6 / 6 | 1.000 | 1.000 | 1.000 | 21.678 | 35.797 | 0.000 |
| other / reference | 18 / 18 / 18 | 1.000 | 1.000 | 1.000 | 7.549 | 13.130 | 0.000 |
| other / separated | 18 / 33 / 18 | 0.545 | 1.000 | 0.706 | 7.549 | 76.830 | 1.333 |

## Limits and reproducibility

Measured separation wall time including weight verification/loading: 1.293 seconds on macOS-27.0.1-arm64-arm-64bit; this is a measurement, not a user-facing completion estimate.

Vocal and bass parts use one-pitch-at-a-time pYIN, with preserved raw F0/voicing frames and separate vibrato/fragment cleanup. Other uses Basic Pitch and is experimental. There is no percussion transcription and no generated accompaniment. A vocal stem can contain several singers; one estimated line does not recover every harmony. Synthetic timbres and a single short mixture do not establish quality on real vocals, physical instruments, mastered songs or long recordings. Leakage, harmonic errors, missing notes and timing shifts need review.

Every engine version, checksum, inference setting, original note and measured result is retained in `evaluation/mixed-results.json` and the ignored generated artifact folder. All sources have the same sample timeline. Each adapter runs in a fresh subprocess, as production jobs do; the evaluator requires a successful process exit before accepting its output. This avoids sharing native separation and transcription runtime lifetimes, and propagates crashes or timeouts instead of publishing a successful report. Rerun:

```sh
.venv/bin/python backend/install_separation.py
.venv/bin/python scripts/evaluate_mixed.py
```

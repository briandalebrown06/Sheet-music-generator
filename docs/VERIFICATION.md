# Verification · 2026-10-27

## Sheet music discovery update · 2026-10-27

The completed-project screen now leads with inline/full-screen sheet music and direct saved-score downloads. **144 focused browser assertions**, **71 review regression assertions**, **34 hosted HTTPS assertions**, and **44 focused backend tests** passed. These are this update's checks, separate from the historical deployment audit below. See [SHEET_MUSIC_DOWNLOADS.md](SHEET_MUSIC_DOWNLOADS.md) for exact coverage, reproduction, limitations, and update commands.


## Deployment audit

The latest audit added authentication, owner checks, quotas, upload idempotency and durable deletion. **218 Python tests and 303 browser assertions passed**, alongside actual maximum-duration processing, worker SIGKILL/restart recovery, cancellation and proxy-log privacy checks. Full details, reproducible commands and unverified deployment gates are in [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md). Earlier-stage records below are historical, not a claim that hosted deployment has been qualified.

## Mixed recordings and selected-part scores

Added explicit solo/mixed input modes, real Open-Unmix UMX-HQ separation, aligned four-stem audition and persistent child-part projects in SQLite schema 6. Vocals and bass use the installed librosa pYIN profiles; experimental other uses the existing real Basic Pitch ONNX adapter. Drums are audition-only. No accompaniment is generated. See [SEPARATION_ENGINE.md](SEPARATION_ENGINE.md) for the separate code/weight-license and maintenance review.

| Final mixed-stage check | Result |
| --- | --- |
| Complete Python suite, real models and evaluation metrics | **191 passed**, five upstream deprecation warnings |
| Mixed-song browser workflow | **48 assertions passed** |
| Existing solo review browser regression | **71 assertions passed** |
| Playback timing and scheduler tests | **8 passed** |
| Two complete isolated multitrack evaluations | Successful exits; identical quality metrics |
| Frontend build/type checking and full formatting check | Passed; existing OSMD large-chunk advisory |
| Dependency consistency and Git whitespace | Passed |
| Mixed-part PDF and desktop/mobile screenshots | Independently visually inspected |

The mixed Chrome integration passed **48 assertions** using actual installed models and disposable data: generated multitrack upload, all four WAV players, selected bass transcription, note editing, saved score revision, MusicXML/MIDI/PDF parsing, reopen, duplicate part selection, rejected drum transcription, original offset mapping and preservation of source/stem/raw files. The selected source clip spans 0.1–11.9 seconds and all stems/child notes retain that original offset. Desktop and 390px mobile screenshots were independently inspected. The edited bass part produced a one-page A4 vector PDF with visible, unclipped staves and margins.

The existing solo review browser regression passed **71 assertions** again, and all **8 playback timing/scheduler tests** passed. Frontend compilation/formatting and dependency consistency were checked. Chrome uses emulated viewports; this does not establish other-browser/device or audible quality coverage.

The reproducible 12-second multitrack fixture has separately known procedural sources. Actual separation preserved exactly 529,200 stereo frames per stem. Separated vocal/bass transcription matched all 14 expected notes under the exact-pitch/100ms-onset criterion, with onset MAE of 30.737/21.678 ms and offset MAE of 33.685/35.797 ms. Experimental other matched all 18 expected notes but produced 15 additional detections (precision 0.545, recall 1.000). Leakage and nonlinear artifacts remain measurable: SI-SDR is 29.149 dB vocals, 16.904 bass, 13.904 other and 7.842 drums; projection leakage and residual fractions are reported separately in [MIXED_EVALUATION.md](MIXED_EVALUATION.md). These are measurements on one synthetic mixture, not evidence of commercial-song accuracy.

Resource tests cover bounded timeout/RSS/storage, partial-attempt cleanup, restart recovery, cancellation, retry budgets, aligned-result rejection, concurrent child deduplication and disk-copy failure rollback. Injected adapters in lifecycle tests are explicitly labeled test fixtures; the separate model tests and browser workflow run real inference.

Repeated evaluator execution exposed an ONNX Runtime native telemetry-thread shutdown crash after inference. The evaluator now runs each adapter in a fresh subprocess, matching production isolation, and accepts results only after successful exit. Two complete reruns exited normally with identical separation and note metrics; nonzero exit and timeout propagation are tested. No failure is suppressed or converted into a successful evaluation. A separate macOS process-group teardown race briefly returned EPERM after the last descendant had stopped, then ESRCH about 23 ms later. Worker cleanup now retries that Darwin-specific transition for at most 0.2 seconds; persistent denial still raises. Real descendant tests include a SIGTERM-resistant child that requires SIGKILL, alongside deterministic permission-failure tests.

The local API/worker were restarted and health reports separation, transcription, notation and PDF available. The existing user project remains in solo mode with 1,037 notes at revision 1 and an unchanged note-data hash. No user recording was used as a write-test fixture.

Maximum-length mastered songs, real multi-singer harmony recovery, GPU inference, other operating systems and physical listening are unverified. The chosen separator is an older reference implementation; its current installed dependencies and actual CPU inference were verified, with a replaceable adapter retained.

The sections below preserve the earlier-stage verification record.

## Transcription review workspace

The review screen now combines actual decoded waveform peaks, four labeled playback modes, a note-event editor, canonical notation, musical settings and saved-revision exports. SQLite schema version 5 retains stable note identities across deletion/restoration and atomically commits performance notes with the corresponding score. Raw audio, model events and raw MIDI remain immutable.

| Earlier review-stage check | Result |
| --- | --- |
| Complete Python suite, evaluation metrics and real model tests | **155 passed**, five upstream deprecation warnings |
| Review persistence/identity/assignment tests | **8 passed** within the full suite |
| Actual PDF engraving and revision tests | **11 passed** within the full suite |
| Playback timing and scheduler tests | **8 passed** |
| Expanded review browser integration | **71 assertions passed** in installed Chrome |
| Canonical notation browser regression against the review UI | **54 assertions passed** |
| Transcription browser regression against the review UI | **61 assertions passed** |
| Frontend TypeScript, production build and formatting | Passed; OSMD retains the documented large-chunk advisory |
| Dependency consistency | `pip check`: no broken requirements |

Review checks cover atomic preview/save/reopen, split lineage, manual-note provenance, undo restoration of deleted identities, explicit voices and multiple parts, raw-artifact preservation, actual waveform amplitudes, conflicting saves, and stale export rejection. Save responses retain the exact project snapshot captured within the commit transaction even if another tab saves immediately afterward. The browser suite uploads generated audio and runs real Basic Pitch before editing those actual detected events. It checks notehead selection including piano staves and manual parts/voices, pitch/start/duration/part/voice changes, add/delete/split/merge, undo/redo, four playback modes, oscillator frequencies, loops and cursors, the original recording's 0.1-second clip offset, saved revisions, MusicXML reparsing, edited MIDI pitches/times, PDF parsing and responsive layout.

The eight-note generated review input produced 11 detected notes after clipping. The exported edited note's MIDI timing matched the canonical 0.25–0.75-second interval, separately from its edited performance timing of 0.3–0.7 seconds. Desktop and 390px mobile screenshots were inspected. The browser reported no application exceptions or console errors; two confirmed optional `/favicon.ico` 404s were recorded separately. The earlier notation suite was migrated to the review interface and passed again, including all musical controls, triplets, pickup, staff corrections and exact measure sums.

PDF export uses the installed Verovio 6.3.0 headless engraver and vector PDF conversion. A 48-bar development fixture containing piano staves, triplets, rests, multiple voices and barline ties produced three A4 pages. All three pages were independently rendered and visually inspected: legible staves, intact systems, clear margins and page/revision footers. The browser fixture's actual uploaded-audio transcription was also edited, saved, exported and visually inspected as a one-page PDF with two assigned parts. The API rejects vectors that exceed printable margins and rechecks the saved revision after engraving. MuseScore's installed CLI was also evaluated but crashed on shutdown; the application uses the working Verovio path. See [PDF_EXPORT.md](PDF_EXPORT.md) for reproduction and licenses.

Only installed Chrome on macOS and emulated desktop/mobile viewports were exercised. No physical listening assessment, audio-device latency measurement, paper printing, Safari/Firefox, physical mobile, Windows or Linux testing is claimed. Synthesized playback uses simple tones and does not reproduce instrument timbre or pitch bends. The synthetic audio verifies the editing/export workflow, not transcription quality on commercial recordings. Constant-tempo alignment and one quantization grid remain limitations.

The local API and separate worker were restarted with this implementation. Health reports decoding, transcription, notation and PDF available. The existing uploaded project still has 1,037 notes at performance revision 1; its note-data hash was identical before and after the update. Its 2,000-bin waveform and a transient score preview were inspected without saving over the project.

See [REVIEW_WORKSPACE.md](REVIEW_WORKSPACE.md) for semantics and commands. The sections below record checks performed at earlier implementation stages.

## Canonical notation update

The current notation implementation has a separate canonical score in rational quarter-note beats, preserves source performance seconds, and renders music21-generated MusicXML with OpenSheetMusicDisplay 2.2.1. Score settings and revisions persist in SQLite schema version 4 independently from performance-note revisions.

| Earlier notation check | Result |
| --- | --- |
| Full Python suite, evaluation metrics, and real model tests | **136 passed**, five upstream deprecation warnings |
| New canonical score unit/integration tests | **32 passed** within the full suite |
| Isolated notation browser suite | **53 assertions passed** in installed Google Chrome |
| Frontend TypeScript, production build, formatting | Passed |
| Dependency consistency and Git whitespace | Passed |
| Desktop and 390px mobile rendering | Visually inspected; no page overflow |

Rhythm tests exercise barline ties, pickup measures (including an empty piano staff), rests, chords, triplets, independent overlapping voices, exact measure duration, deterministic randomized invariants across all five grids, and MusicXML round trips. Additional checks cover tempo suggestions/fallbacks, key spelling and transposition, compound-meter beaming, corrected piano assignments, canonical MIDI tempo/timing, API revisions, preview immutability, and stale-export rejection.

The browser test uploads generated audio and runs real Basic Pitch before rendering notation. Its eight played fixture notes produced ten detected performance events; notation used those actual detections without substituting reference notes. It checks all musical controls, preview/save/reload, MusicXML and MIDI downloads, XML re-parsing, exact voice durations, piano overrides, raw preservation, conflicts, and stale scores. This verifies the workflow, not acoustic transcription accuracy. Only installed Chrome and emulated desktop/mobile viewport sizes were tested, not other browsers or physical mobile devices.

A separate non-destructive preview of the existing uploaded project was generated and visibly inspected in the in-app browser: 1,037 performance events, 76 measures, and four explicit voices. Tempo evidence was insufficient, so the UI labels 120 BPM as assumed, alongside meter/key/alignment assumptions. It also reports 181 notes extended to the selected grid and 37 coincident same-pitch detections represented once while preserving source IDs. This preview was not saved over user settings, and performance notes were unchanged.

OSMD is loaded in a separate renderer chunk (~363 KB gzip). The build's large-chunk advisory remains; rendering a dense score can be expensive. Current rhythm compilation uses one straight or triplet grid per score and constant tempo. Direct graphical note dragging, mixed grids, rubato maps, and professional engraving remain future work. See [NOTATION.md](NOTATION.md) for model semantics and reproduction commands.

## Real transcription update

Installed and ran Spotify Basic Pitch 0.4.0 with its bundled ONNX weights in the repository's durable Python 3.11 `.venv`. Its interpreter base lives in ignored `.runtime/python`; the local FFmpeg executable is in `.runtime/bin/ffmpeg`. The API and separate worker now run from this environment on loopback. Live health reports the worker online and decoding, transcription, and notation capabilities available. The existing completed project remains present after migration to schema version 3.

| Earlier transcription check | Result |
| --- | --- |
| Full Python and evaluation-metric suite with `RUN_MODEL_TESTS=1` | **89 passed**, five upstream deprecation warnings |
| Isolated transcription browser suite | **58 assertions passed** in installed Google Chrome |
| TypeScript and production build | Passed |
| Frontend formatting and Git whitespace checks | Passed |
| Installed dependency consistency | `pip check`: no broken requirements |
| Desktop and 390px mobile screenshots | Visually inspected; no page overflow |
| Deterministic model evaluation | Reproduced across two runs; actual metrics below |

The browser suite uploads generated audio, observes real worker stages, downloads and validates raw MIDI/model/event artifacts, edits/adds/removes notes, reloads saved edits, and verifies raw artifacts are unchanged. It also checks queued cancellation, attempt limits, revision-conflict draft retention, stable IDs, original timing, provenance, and responsive layout. It runs in disposable directories on ports 8001/5174 using the actual model; it does not modify user projects.

Python regression checks cover running-job cancellation, decoder descendant cleanup after a process leader exits, cancellation/exit races, expired lease recovery and retry budgets, stale-worker fencing, duplicate-job prevention, atomic edit revisions, legacy migrations, raw artifact preservation, and real upload-to-MIDI processing. One sandboxed run raised `PermissionError` while signaling a reparented test subprocess; rerunning the complete suite with normal host process permissions passed all 89 checks. Cleanup behavior was not weakened to bypass that restriction.

The five required generated fixtures contain **18 expected notes**. Cleanup produced **26 notes**, with **18 exact-pitch/onset matches and eight extra detections**: precision **0.692**, recall **1.000**, F1 **0.818**. Matched-note onset/offset mean absolute errors were **5.942 / 38.797 ms**. Silence produced **zero notes**. A supplementary 20-second sustained note crossed upstream inference windows, retained global timing, and produced an extra tail fragment. Full definitions, per-fixture results, model hashes, and limitations are in [the evaluation report](TRANSCRIPTION_EVALUATION.md).

The test material is generated additive synthesis, not physical instruments or commercial recordings. Model activations are not calibrated accuracy percentages. Hardware microphones, other browsers, Windows, Linux, and maximum-duration clips were not newly validated by this transcription suite. The earlier audio-input checks below describe their separate coverage.

## Audio-input workflow update

Implemented clip selection and preview, optional musical hints, recording lifecycle handling, strict full-source decoding, selected-clip silence checks, normalized playback, and schema migration.

| Earlier audio-input check | Result |
| --- | --- |
| Complete Python suite with `RUN_MODEL_TESTS=1` | **51 passed**, five upstream deprecation warnings |
| Dedicated browser audio-input suite | **90 assertions passed across 9 categories** |
| Original browser smoke suite | Passed against the updated app; missing-dependency retry branch skipped because analysis was ready |
| Frontend TypeScript + production build | Passed |
| Frontend formatting and Git whitespace checks | Passed |
| Desktop and 390px mobile screenshots | Visually inspected; no horizontal overflow |

Real FFmpeg-generated **WAV, MP3, M4A, and FLAC** fixtures were uploaded in installed Google Chrome, played, cropped, processed by the actual worker/model, and retrieved after refresh. Checks verified that original downloads were byte-identical, normalized copies had the chosen duration, and offsets/hints persisted. Browser playback advanced, paused, and respected selection boundaries. A forced native-preview failure was followed by real local FFmpeg preview decoding without any project or transcription job being created.

The actual MediaRecorder API used **Chrome's test microphone** for capture, stop/finalization, playback, discard, submit, and track-cleanup checks. Permission denial, missing microphone, unsupported recorder, insecure-context errors, and delayed permission responses used explicit test doubles. No physical microphone, system permission panel, Safari, Firefox, mobile device, or cross-browser compatibility testing was performed.

Backend checks cover corrupted/truncated and empty audio, silent selections, source-duration limits even with short clips, finite/range validation, adjustable silence floor, wall-clock timeout cleanup, preview cleanup on client disconnect, original preservation, schema upgrades, and hint-aware MusicXML/MIDI timing. Decoder resource enforcement has been exercised on macOS; the Linux address-space and Windows fallback behavior remain untested here.

That suite runs in disposable data directories and ports 8001/5174 via `pnpm -C scripts test:audio`; test-only engine spies in unit tests are explicitly labeled, while browser/model integration uses real inference. Existing user data was not used as a test fixture. At that stage, the existing completed project remained present under schema version 2; the transcription update above subsequently migrated it to version 3.

## Earlier foundation checks

| Check | Result |
| --- | --- |
| Frontend TypeScript and Vite production build | Passed on Node 24.19.0, React 19.3.0, Vite 8.3.5 |
| Frontend formatting | Prettier check passed |
| API/worker suite without analysis packages | 16 passed, optional analysis/notation modules skipped |
| Full Python suite with real analysis enabled | 20 passed on Python 3.11.18, macOS arm64; five upstream deprecation warnings |
| Dependency consistency | `pip check` passed in API and analysis environments |
| Browser integration | 27 assertions across 13 categories passed in installed Google Chrome via Playwright, desktop and 390px mobile viewport |
| MusicXML/MIDI round trips | Passed for sequential and overlapping test note events and actual model output |

## What the Python checks establish

- Uploads and processing jobs persist across independent API instances.
- Audio bytes are retrievable, microphone source metadata is saved, and unsafe filenames are sanitized.
- Unsupported containers/extensions, empty/corrupt files, excessive bytes, and decoded over-duration audio are rejected.
- Client/Host/Origin restrictions work, including malformed Host rejection.
- Job claiming is transactional; duplicate active attempts are prevented, stale attempts are fenced, and retries retain history.
- Missing dependencies yield actionable failures without fabricated notes or exports.
- FFmpeg receives bounded duration/time settings and restricted input protocols/demuxers.
- A failure to produce MusicXML does not hide an independently successful MIDI file.

## Real model test

The opt-in integration test synthesizes a **clearly labeled development fixture**: three seconds containing harmonically rich C4 and E4 tones. It uploads those audio bytes through FastAPI, runs the real worker and FFmpeg decoder, invokes Spotify's bundled Basic Pitch ONNX weights, then downloads and reparses both actual export files. Both expected MIDI pitches (60 and 64) were present in model output. No note events were substituted for the model result.

Tested combination: Python 3.11.18, Basic Pitch 0.4.0, ONNX Runtime 1.31.1, music21 9.9.1, NumPy 1.26.4, librosa 0.11.0, numba 0.68.1. The FFmpeg binary used for testing was provided by `imageio-ffmpeg` in an isolated temporary environment; that helper is not an application dependency or vendored binary. ONNX telemetry is disabled before model loading.

Bundled ONNX model SHA-256:

```text
2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec
```

Re-run with analysis dependencies, pytest/httpx, and a local FFmpeg:

```sh
RUN_MODEL_TESTS=1 python -m pytest backend/tests -q
```

This demonstrates the local inference path and export compatibility. It is **not an accuracy benchmark** on voices, instruments, or full songs. The Apple Silicon test does not expand upstream's documented support matrix; Windows and Linux were not tested here. Real microphone hardware quality was not evaluated.

## Browser checks

The browser smoke test uses a disposable data directory and visibly named fixture projects. It covers API health, foreign-Origin and invalid-upload rejection, keyboard source-tab navigation, WAV upload, project/job persistence after reload, original-audio retrieval, failure and retry history, desktop/mobile overflow, and no uncaught browser errors. It exercises start/stop/preview/upload through the actual MediaRecorder API using Chrome's test microphone and separately tests denied permission. Application assets and audio requests stay local; external fonts were removed.

## Remaining work

- Direct graphical note/rhythm dragging, mixed quantization grids and expressive tempo maps. Event editing, synchronized synthesized playback and canonical notation are implemented.
- Broader MusicXML interoperability, physical printing, international title glyphs and complex-score engraving evaluation. Real paginated PDF export is implemented and tested on the fixtures described above.
- A representative physical-recording quality evaluation; the synthetic fixtures above do not establish real-world transcription accuracy. Maximum-length clips and arbitrary mixed songs remain outside the tested scope.
- Broader source-separation and selected-part evaluation on physical/mastered recordings; drum notation and multi-singer harmony recovery remain unsupported.
- Target-host Linux/container qualification, real-domain TLS, dependency advisory remediation/review, operations and load testing before public launch. Authentication and project authorization are now implemented.

No user recordings or generated model-output binaries are included in source control. Test generators, the explicit evaluation manifest, and measured JSON summary are included for reproducibility.

## Live preview for this session

The preview runs at `http://127.0.0.1:5173`, with API on port 8000 and a separate worker. It uses the durable repository `.venv` and `.runtime/bin/ffmpeg`. Health was verified with FFmpeg, transcription, notation, PDF, and worker availability all true. Follow the README for fresh-clone setup. Test fixtures remain separate from the default project data.

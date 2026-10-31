# Sheet-music-generator · Scorecraft

An audio-to-sheet-music workspace with a local mode and an authenticated single-host deployment candidate. Upload a solo performance, record through your microphone, or separate a mixed recording into stems. Select a pitched part, review its detected notes, and export an editable score.

This repository started with only a README. The foundation adds a React + TypeScript frontend, FastAPI backend, SQLite storage, and a separate Python analysis worker. Solo and experimental pitched-stem transcription use Spotify Basic Pitch; vocal melody and bass use pYIN. Mixed recordings use a separately licensed Open-Unmix UMX-HQ separator. All engines sit behind replaceable adapters. There is no simulated transcription or seeded song data.

## What is implemented

- Responsive upload/record, project library, and project detail screens.
- Independent defaults of **500,000,000 bytes per upload** and **60 minutes per decoded recording**, with resumable 8 MiB transfers, actual upload progress, private disk spooling, and checkpointed inference.
- Runtime microphone MIME negotiation, start/stop/elapsed/discard controls, playback, HTTPS/permission/device guidance, and track cleanup.
- Clip start/end selection and playback before submission, with optional instrument, tempo and meter hints.
- Local FFmpeg preview fallback for browser-incompatible audio, without starting analysis.
- Persistent projects, original recordings, note events, job history, and failed-job retries.
- Health/capability reporting and worker heartbeats; atomic job claims and interrupted-job recovery.
- Bounded FFmpeg audio decoding to mono 22,050 Hz PCM, followed by Basic Pitch inference.
- Editable performance-note table with stable IDs, provenance, original timing, manual additions/removal, and revision-conflict protection.
- Immutable raw model arrays/events, raw-performance MIDI, separate cleaned/edited MIDI, and a per-attempt provenance manifest.
- Real stages, cancellation, bounded retries, process isolation, and automatic recovery after worker restart.
- A separate canonical score with rational beats, measures, rests, chords, explicit voices, and ties across barlines.
- Non-destructive notation preview, saved score revisions, musical-setting controls, and OpenSheetMusicDisplay rendering.
- Straight/triplet quantization, manual tempo/meter/key/downbeat/pickup/clef/transposition, and optional correctable piano staff assignment.
- MusicXML and notation MIDI generated from the canonical score; raw and edited performance MIDI remain separate.
- Review workspace with a real waveform, synchronized note selection, split/merge, part/voice assignments, and undo/redo.
- Original, raw-performance, edited-performance, and quantized-score audition with region loops and visible cursors.
- Atomic review saves and readable, paginated vector PDF engraving from saved canonical notation.
- Explicit, reversible piano accompaniment with separate hand/voice/staff assignments, configurable reach, a three-held-key texture target, movement checks, and a separate notation-readability pass.
- Locked edits, manual fingerings, explicit pedal/key-release annotations, optional vocal cue staff, and reassessment after exact transposition.
- Prominent **Your sheet music** results with inline/full-screen viewing and one-click PDF, MIDI, and editable MusicXML downloads; saved-revision filenames, generation status, and retry links.
- Explicit solo/mixed input modes, local source separation, four-stem audition and persistent selected-part projects.
- Vocal/bass pitch tracking with raw F0 preservation, vibrato cleanup and exact original-song offsets.
- Bounded separation jobs, checksum-verified weights, reproducibility manifests and discarded-attempt cleanup.
- Provisioned accounts, expiring server-side sessions, CSRF protection and owner checks on every recording/stem/score/export route.
- Durable request/processing quotas, upload retry deduplication, and lease-protected project/child deletion.
- Digest-pinned deployment images, Linux dependency locks, private storage and a separate offline worker.

**Stage boundaries:** direct graphical note dragging, expressive tempo maps, arbitrary mixed straight/triplet grids, and drum notation are not implemented yet. Mixed separation returns vocals, bass, drums and other; other is a residual category and pitched transcription is experimental. Transcription does not silently generate accompaniment. An explicit piano action creates a separate editorial accompaniment with its own saved revisions, while retaining the source. Its movement and fingering checks are heuristics, not pianist validation. The edition chooser proposes tempo and key from bounded note analysis and labels them as estimates. Original scores retain the explicit 120 BPM fallback until changed; meter and bar alignment require musical review. A sixteenth-note melody grid can coexist with an eighth-note accompaniment grid. Performance MIDI keeps detected/edited seconds, while notation MIDI follows the saved score's rational beats. Synthesized audition offers locally hosted sampled piano and simple tones; the recorded piano timbre does not add expressive timing or fabricated dynamics. Pitch, timing, voice allocation and engraving need review and are not a promise of professional scores.

Start with the [deployment runbook and demonstration checklist](docs/DEPLOYMENT.md), [audit evidence and launch gates](docs/DEPLOYMENT_AUDIT.md), [authentication](docs/AUTH_SECURITY.md) and [retention/deletion policy](docs/RETENTION.md). Public deployment has not been performed; target-host validation and dependency-advisory review are still required.

See [sampled piano playback and sample licensing](docs/PIANO_PLAYBACK.md), [three piano editions and musical-setting evidence](docs/PIANO_EDITIONS.md), the [published-song notation study](docs/musical-reference-study.md), [piano accompaniment and measured playability checks](docs/PIANO_ACCOMPANIMENT.md), [sheet music and download verification](docs/SHEET_MUSIC_DOWNLOADS.md), the [mixed-recording workflow](docs/MIXED_RECORDINGS.md), [separation engine/license evaluation](docs/SEPARATION_ENGINE.md), [measured multitrack results](docs/MIXED_EVALUATION.md), [review workspace](docs/REVIEW_WORKSPACE.md), [PDF engraving](docs/PDF_EXPORT.md), [canonical notation model and controls](docs/NOTATION.md), [transcription architecture and API](docs/TRANSCRIPTION.md), [actual synthetic evaluation](docs/TRANSCRIPTION_EVALUATION.md), [audio-input workflow and API contract](docs/AUDIO_INPUT.md), [implementation plan](docs/PLAN.md) and [dependency/code/model license review](docs/DEPENDENCIES.md).

## Prerequisites

- Node.js **24 LTS** (or a maintained Node >=22.12) and pnpm **11.25.0**.
- Python **3.11** for analysis. The API can run independently on Python 3.12, but Basic Pitch must use its compatible environment. Do not use this computer's system Python 3.15 for analysis.
- FFmpeg available on `PATH`, or set `FFMPEG_BINARY` to its absolute path.
- A current Chrome, Firefox, Safari, or Edge browser. Microphone access needs localhost or a secure context and browser permission.

Basic Pitch's upstream documentation claims Apple M1 support only on Python 3.10. That version is now EOL; this project targets Python 3.11 with explicit ONNX inference. See the dependency review and the verification report for the distinction between upstream support and locally tested compatibility.

## Local setup

This checkout currently has a working `.venv`, repository-local Python under `.runtime/python`, and `.runtime/bin/ffmpeg`. Its ignored `.env` points to that decoder. These were installed and tested locally; they are not committed runtime binaries. Activate `.venv` and use the three start commands below. For a fresh clone, follow the installation steps here.


Run from the repository root:

```sh
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
```

This installs the API and test tools without loading a model. To enable real transcription and draft exports in that environment:

```sh
python backend/install_analysis.py
python -m pip install -r backend/requirements-engraving.txt
python -m pip check
ffmpeg -version
```

The checked installer requires Python 3.11. On macOS arm64 14+, it installs the fully resolved `backend/requirements-analysis-macos-arm64-py311.lock.txt`; other platforms use `backend/requirements-analysis.txt` and still need their own inference validation. It uses the model bundled by Spotify; it does not fetch arbitrary third-party weights. Dependency downloads require internet; application processing is local. Installation may take several minutes.

To enable mixed recordings in the same environment:

```sh
python -m pip install -r backend/requirements-separation.txt
python backend/install_separation.py
python -m pip check
```

The separator uses local CPU inference and four verified UMX-HQ weight files (about 143 MB). Jobs do not download models. See the mixed workflow for RAM/disk limits and maintenance caveats.

Install the frontend:

```sh
cd frontend
pnpm install --frozen-lockfile
cd ..
```

Start the three processes in separate terminals from the repository root:

```sh
# Terminal 1: API
source .venv/bin/activate
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

```sh
# Terminal 2: analysis worker
source .venv/bin/activate
python -m backend.app.worker
```

```sh
# Terminal 3: frontend
cd frontend
pnpm dev
```

Open [the local studio](http://127.0.0.1:5173). API health is at [127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health); interactive API docs are at [127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

Uploading works without inference installed: the recording and job are persisted. A running worker records an actionable error when a prerequisite is missing; no notes are substituted. Once dependencies are ready, restart the worker and select **Retry transcription** on the project. Without a worker, jobs remain queued.

### Separate analysis environment

The API and worker may use separate virtual environments, provided they run on the same computer with the **same `DATA_DIR`**. The API needs music21 and pretty_midi for notation preview/exports; install the analysis requirements in both environments for the simplest supported setup. Health reports the active worker's package capabilities rather than assuming the API interpreter can perform inference. A capability means the package is present, not that every model call will succeed. The mixed-recording path is verified with one shared environment and model directory; install the separation dependencies there for both API and worker capability checks.

## Configuration and storage

Copy `.env.example` to `.env`; both processes load it from the repository root. Existing process environment variables take precedence. Restart the API and worker after changing settings.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `.data` | SQLite database and recordings; relative to repository root |
| `MAX_UPLOAD_BYTES` | `500000000` | Exact per-file byte ceiling; legacy `MAX_UPLOAD_MB` (MiB) is read only when this variable is absent |
| `MAX_DURATION_SECONDS` | `3600` | Maximum decoded duration of the whole original; longer audio is rejected, not silently trimmed |
| `UPLOAD_CHUNK_BYTES` | `8388608` | Maximum resumable request chunk; partial transfers resume from the last verified chunk |
| `UPLOAD_TTL_SECONDS` | `86400` | Idle upload expiry; worker reclaims abandoned bytes |
| `FFMPEG_BINARY` | `ffmpeg` | Command or absolute path to FFmpeg |
| `BASIC_PITCH_RUNTIME` | `onnx` | Explicit bundled-model runtime; see analysis dependencies |
| `DECODE_TIMEOUT_SECONDS` | `600` | Decoder wall-clock timeout |
| `DECODE_CPU_SECONDS` | `300` | Decoder CPU-time limit |
| `DECODE_MEMORY_MB` | `512` | Linux decoder address-space limit; macOS uses allocation/thread/output bounds |
| `SILENCE_THRESHOLD_DBFS` | `-70` | Reject selections with both peak and RMS at/below this level |
| `MAX_JOB_ATTEMPTS` | `3` | Total attempts per new project, including retries and recovery |
| `JOB_TIMEOUT_SECONDS` | `7200` | Maximum wall time for one isolated solo analysis attempt |
| `MINIMUM_NOTE_SECONDS` | `0.08` | Default short-note cleanup cutoff, seconds |
| `MERGE_GAP_SECONDS` | `0.03` | Default conservative fragment-merge gap, seconds |

The duration limit applies to the entire original, including when selecting a short clip. Upload admission checks size, container, and metadata; full decoding, actual duration, and selected-clip silence checks run in the worker before inference. Invalid audio remains a failed project with a useful error. Clip offsets and musical hints persist across reloads. Note event times are relative to the selected clip; add its saved start offset to locate them in the original.

`.data/projects.sqlite3` holds projects, jobs and worker heartbeats. `.data/projects/<project-id>/source` holds the original audio; each job has its own output directory. The database uses WAL and job leases. A restarted worker marks expired attempts interrupted and automatically queues recovery within the saved attempt budget. Manual retry also creates a new attempt and retains history. Cancellation is durable and stops the isolated analysis process. A lost worker cannot overwrite the results of a later attempt. Keep one worker for the intended single-user setup.

Mixed projects and selected parts have a default 16,384 MiB family storage budget, a 14,400-second separation timeout and a 4,096 MiB monitored process-memory threshold. The default per-account storage quota is 32,768 MiB. Configure these and model context/thread settings in `.env.example`; see [mixed limits](docs/MIXED_RECORDINGS.md). Failed/canceled mixed-attempt generated files are cleaned after termination. Recordings are not automatically deleted. Project deletion immediately denies access and the worker reclaims its artifacts after file/process leases release. Back up the complete data directory with the API and worker stopped; see the retention policy for backup and crash-orphan handling. No data is committed to Git; test fixtures use temporary directories.

See [large-audio implementation, measured results, and exact reproduction commands](docs/LARGE_AUDIO.md). A real generated 12-minute, 127,008,044-byte performance was uploaded through HTTP, resumed after transfer/API interruption, processed through actual Basic Pitch after a worker restart, and checked through its final MIDI note. Exact 500 MB and 60-minute limits were tested independently. An additional real one-hour Basic Pitch/raw-MIDI run retained the ending at 3,599.83 seconds and used 2.46 GiB peak process RSS on this Mac. The existing 10,000-note editor limit remains: dense inputs can finish transcription with full raw notes/MIDI while notation reports its limit; no prefix is passed off as the full recording.

## Local access boundary

Both servers bind to **127.0.0.1**. The API rejects remote clients, unexpected Host headers, and foreign browser origins. Uploaded containers, bytes, decoded duration, decode time and permitted FFmpeg protocols/formats are bounded. Files are stored under generated IDs, not user paths.

Local anonymous mode remains loopback-only and uses the internal owner `local`. Accounts and per-project authorization are implemented for `AUTH_ENABLED=true` or mandatory `APP_MODE=hosted`. Hosted mode requires an exact HTTPS `PUBLIC_ORIGIN`, secure session cookies, CSRF tokens and the private reverse-proxy topology in [DEPLOYMENT.md](docs/DEPLOYMENT.md). Local projects are never automatically exposed to hosted accounts; explicit offline ownership migration is required. Do not publish the anonymous local mode through a tunnel. This is a deployment candidate, not a claim of production readiness.

## Checks

```sh
source .venv/bin/activate
python -m pytest backend/tests -q
python -m pip check
pnpm -C frontend typecheck
pnpm -C frontend build
pnpm -C frontend format:check
# Optional: exercise the real installed model and FFmpeg, rather than only core checks.
RUN_MODEL_TESTS=1 RUN_SEPARATION_TESTS=1 python -m pytest backend/tests scripts/test_evaluation.py scripts/test_mixed_evaluation.py -q
```

The original foundation smoke test requires Google Chrome plus Playwright and running API, worker and frontend processes. Use a disposable data directory (for example `DATA_DIR=/tmp/scorecraft-smoke` for **both** API and worker). It creates explicitly labeled fixture recordings; it never injects fabricated analysis results.

```sh
pnpm -C scripts install --frozen-lockfile
pnpm -C scripts test
```

The smoke runner checks uploads, persistence, original audio, retries when prerequisites are absent, desktop/mobile layout, denied microphone access, and a real MediaRecorder lifecycle using Chromium's test microphone. It does not access your microphone. `PLAYWRIGHT_MODULE`, `APP_URL`, `API_URL`, `CHROME_CHANNEL`, and `SMOKE_ARTIFACT_DIR` can override the defaults.

The dedicated audio-input integration test launches isolated API/worker/frontend processes on ports 8001/5174. It requires the real analysis dependencies, an installed FFmpeg, and Google Chrome. It generates WAV/MP3/M4A/FLAC fixtures and tests real decoding, playback, crop persistence, and browser test-microphone capture. Explicit browser-error mocks are reported separately. It does not access hardware microphones or default project data.

```sh
AUDIO_TEST_PYTHON="$PWD/.venv/bin/python" FFMPEG_BINARY=ffmpeg pnpm -C scripts test:audio
```

The transcription and notation integration tests launch isolated services and check real raw MIDI, preserved artifacts, note editing, cancellation, rendered scores, saved settings, and canonical exports:

```sh
pnpm -C scripts test:transcription
pnpm -C scripts test:notation
pnpm -C scripts test:playback
pnpm -C scripts test:review
pnpm -C scripts test:piano
pnpm -C scripts test:sheet-music
pnpm -C scripts test:mixed
pnpm -C scripts test:deployment
# Optional ingress audit with a verified Caddy binary:
pnpm -C scripts test:ingress
.venv/bin/python scripts/evaluate_transcription.py
.venv/bin/python scripts/evaluate_mixed.py
```

The five required synthetic fixtures yielded 18 expected-note matches and 8 extra cleaned detections: precision 0.692, recall 1.000, F1 0.818, with matched onset/offset mean absolute errors of 5.942/38.797 ms. Silence produced 0 notes. See [the evaluation report](docs/TRANSCRIPTION_EVALUATION.md) for definitions and limitations; these results do not establish commercial-song accuracy.

See [the verification report](docs/VERIFICATION.md) for checks actually run and remaining limitations.

## Repository layout

```text
frontend/                 React UI, Vite dev proxy, TypeScript, locked dependencies
backend/app/main.py       HTTP API and persistent upload flow
backend/app/database.py   SQLite projects, durable jobs, leases and history
backend/app/worker.py     Separate process for analysis and exports
backend/app/audio.py      Controlled FFmpeg decoding
backend/app/engine.py     Transcription protocol and Basic Pitch adapter
backend/app/separation.py Verified UMX-HQ separation adapter
backend/app/transcription_profiles.py Vocal/bass pYIN profiles
backend/app/mixed_api.py  Stem audition and persistent part selection
backend/app/notation.py   music21 draft exports from actual note events
backend/app/score.py      Canonical rational-beat score and music21 serialization
backend/app/score_api.py  Non-destructive preview, saved revisions and notation exports
backend/tests/            API, job, decoder and export tests
scripts/                  Browser integration smoke test
```

The next stage can add direct graphical note/rhythm dragging and expressive tempo maps. Review editing, synthesized playback, canonical notation settings, OSMD rendering, and vector PDF export are implemented. Mixed recordings now support local separation and selected-part scores. Drum notation, richer vocal-harmony handling and broader physical-recording evaluation remain future work.

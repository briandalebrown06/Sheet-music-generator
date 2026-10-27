# Score Studio

A **local, single-user** audio-to-sheet-music workspace. Start with a clear solo
voice/instrument recording. Transcription is an estimate, not a promise of perfect
notes or professional orchestral scores from arbitrary songs.

## Implementation stages

1. **Foundation (implemented):** responsive React/TypeScript workspace, upload and
   microphone capture, original-audio playback, project detail/history, persistent
   SQLite projects/jobs, Python API and separate worker. The optional Basic Pitch
   adapter produces real raw note events, displayed as a review table.
2. **Next:** rhythmic quantization, editable notation and synthesized playback.
   Evaluate music21 for score construction and OpenSheetMusicDisplay for rendering.
3. **Then:** MusicXML and MIDI export via music21; PDF through an explicitly
   installed notation engraver (for example MuseScore), not a screenshot of raw notes.
4. **Later:** source separation, part selection, long-song chunking/merging and
   selected-part transcription, after separation model/code licenses are evaluated.

Notation editing, score playback, exports, and full-song separation **are not
implemented**. No random notes, hardcoded songs, or demo analysis are used.

## Local development

Use Node **22.12+** (tested on 24) and Python **3.11 or 3.12** for the core API.
Install a maintained **FFmpeg** OS package and verify `ffmpeg -version`.
For example, on Ubuntu: `sudo apt install ffmpeg`; on macOS: `brew install ffmpeg`.
The optional model runtime below initially targets Linux x86-64/Python 3.11.

From the repository root (POSIX shell):

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
cp .env.example .env
# The app reads process environment. Load the same trusted configuration in each terminal.
set -a; . ./.env; set +a
python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
```

In a second terminal, from the repository root:

```sh
. .venv/bin/activate
set -a; . ./.env; set +a
python -m backend.worker
```

In a third terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open **http://localhost:5173** (127.0.0.1 also works). Vite proxies `/api` to
127.0.0.1:8000, so no permissive CORS configuration is needed.
Health: `curl http://127.0.0.1:8000/api/health`.
Keep the API and worker running from the repository root with the **same DATA_DIR**.
The API starts without a model installed; jobs then fail with an actionable
message and can be retried after installing prerequisites.

`npm run build` checks types and builds the frontend; `npm run preview` is a static
preview only and does **not** proxy the API. Use the dev server for the working local
application. Production serving/deployment is intentionally out of scope.

### Optional real transcription

Basic Pitch 0.4.0's Linux Python 3.11 dependency requires TensorFlow `<2.15.1`.
Use an isolated environment rather than forcing that stack into Python 3.12:

```sh
python3.11 -m venv .venv-analysis
.venv-analysis/bin/python -m pip install -r backend/requirements-analysis.txt
.venv-analysis/bin/python -m pip check
# Stop the other worker first. Load the same .env as the API.
set -a; . ./.env; set +a
.venv-analysis/bin/python -m backend.worker
```

This runtime was installed and smoke-tested on **Linux x86-64/Python 3.11**,
not validated across platforms. A labeled synthetic 440 Hz tone passed through
real Basic Pitch inference and produced an A4 event. A browser-generated synthetic
stream also passed recording → upload → FFmpeg → worker inference → note display.
These checks do **not** establish accuracy on musical performances.
The core API and missing-model behavior were tested on Python 3.12; inference was
tested only in the isolated 3.11 environment. Check model-asset licensing below before redistribution.
TensorFlow is large; install it only when needed. macOS/Apple Silicon and Windows
require separate runtime validation. Review transitive advisories before deploying
or redistributing this legacy inference stack.

The worker converts a supported local audio container to mono, 22,050 Hz PCM WAV
with FFmpeg. It limits decoding to the configured duration plus one second, rejects
overlong/empty/undecodable audio, then runs the engine in a separate process with a
10-minute timeout. Notes contain start/end seconds, MIDI pitch and model amplitude
(**not a calibrated confidence probability**). Empty detections stay empty.
Temporary PCM is removed; original audio and real note results remain on disk.
The engine boundary is `backend/engines.py`; no model is loaded in the API.

### Configuration and storage

See `.env.example`. `DATA_DIR` defaults to `./data` and stores `projects.sqlite3`,
original uploads and note results. Paths are generated IDs, never client filenames.
Default limits are 50 MB and 300 seconds (duration configurable from 1–600 seconds).
Browser recording is capped at 60 seconds and requires microphone permission on
localhost/a secure context. Supported containers: WAV, MP3, FLAC, OGG, WebM, M4A, MP4;
actual decoding is checked by the worker, not trusted from a filename or MIME type.

Jobs are claimed transactionally. Stop workers before deleting data. A killed
worker's job becomes failed/retryable after 20 minutes when a worker next polls.
Originals are retained until you stop the app and remove the relevant data; there
is no automatic retention policy or delete UI yet. Back up the data directory only
while API/workers are stopped. Keep data and `.env` out of git.

**Do not expose this service publicly.** There is no authentication or project-level
authorization yet. Both servers bind loopback by default; the API additionally rejects
non-loopback clients, untrusted hosts and browser origins. Do not defeat these checks
with a reverse proxy, public tunnel or port forwarding. Authentication and per-project
authorization are required before any multi-user/public deployment. Audio/model
processing is local; dependencies must be downloaded during setup.

## API

- `GET /api/health`: service mode, engine name and FFmpeg availability (not model readiness).
- `GET /api/projects`: persisted project summaries with latest job status.
- `POST /api/projects`: multipart field `audio`; persists original and queues a job.
- `GET /api/projects/{id}`: project, job history and completed raw note events.
- `GET /api/projects/{id}/audio`: original audio playback.
- `POST /api/projects/{id}/retry`: queue a new job for a failed project only.

## Dependency evaluation and licenses

Verified official registry metadata and release-tagged source on **2026-10-27**.
Versions below are evaluations, not all installed dependencies:

| Component | Decision / compatibility | License |
| --- | --- | --- |
| [Basic Pitch 0.4.0](https://github.com/spotify/basic-pitch/tree/v0.4.0) **code** | Initial optional engine; published support through Python 3.11. [Metadata](https://pypi.org/pypi/basic-pitch/0.4.0/json) requires TensorFlow `<2.15.1` on Linux Python 3.11. | [Apache-2.0](https://github.com/spotify/basic-pitch/blob/v0.4.0/LICENSE) |
| Basic Pitch **model weights** (`icassp_2022`) | Supplied by the optional package, not vendored here. Inspected separately: [model tree](https://github.com/spotify/basic-pitch/tree/v0.4.0/basic_pitch/saved_models/icassp_2022), [manifest](https://github.com/spotify/basic-pitch/blob/v0.4.0/MANIFEST.in), [NOTICE](https://github.com/spotify/basic-pitch/blob/v0.4.0/NOTICE). | Apparently repository-level Apache-2.0 coverage, but **no distinct weight-specific license grant found**. Obtain upstream confirmation for redistribution/commercial asset clearance. Test audio notices do not establish a model license. |
| [music21 10.5.1](https://github.com/cuthbertLab/music21/tree/v10.5.1) | Suitable for symbolic score construction and MusicXML/MIDI export in stage 2/3; not installed yet. Release [metadata](https://pypi.org/pypi/music21/10.5.1/json): Python ≥3.11, NumPy ≥1.26.4. | [BSD-3-Clause](https://github.com/cuthbertLab/music21/blob/v10.5.1/LICENSE); corpus/third-party scores may differ. |
| [OpenSheetMusicDisplay 2.2.1](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay/tree/2.2.1) | Suitable MusicXML browser renderer, not a note editor or transcription engine. Bundle locally when scores exist; not installed yet. [Official quick start](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay/wiki/Getting-Started). | [BSD-3-Clause](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay/blob/2.2.1/LICENSE) |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg/blob/n9.0.2/LICENSE.md) | External controlled decoder; 9.0.2 current upstream at evaluation, actual smoke test used OS FFmpeg 6.1.1. | Usually LGPL-2.1-or-later; build flags/components may switch to GPL/v3 or nonredistributable `nonfree`. Inspect `ffmpeg -buildconf` and `ffmpeg -L`; no binary bundled here. |

TensorFlow 2.15.0 [metadata](https://pypi.org/pypi/tensorflow/2.15.0/json)
requires NumPy `>=1.23.5,<2`; NumPy 1.26.4 intersects with music21 10.5.1's
requirement. This verifies declared constraints; the Basic Pitch environment was
also installed and smoke-tested, but music21 has not been runtime-tested here.
No music21/OSMD dependency is added until notation is implemented.
React/Vite/TypeScript preserve their package license notices through npm; no
external fonts, analytics or music fixtures are loaded by the UI.

## Checks

```sh
.venv/bin/python -m unittest discover -s backend/tests -v
.venv/bin/python -m pip check
cd frontend
npm run lint
npm run build
npm audit
```

Tests use temporary databases and explicitly labeled synthetic silence/adapter
fixtures. They cover uploads, limits, persistent state, local-only access, queue
claims, retries and failure handling, not model accuracy. Manual verification
includes browser upload/project navigation, responsive layout, original-audio playback,
real FFmpeg decoding and real Basic Pitch inference on labeled synthetic audio.
MediaRecorder capture was tested with an explicitly synthetic browser stream;
physical microphone capture and accuracy on real musical performances still require
validation on a supported local machine. The runner has no physical microphone.

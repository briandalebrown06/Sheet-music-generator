# Real transcription and durable jobs

Spotify Basic Pitch 0.4.0 runs in the local background worker using its bundled ONNX model. The API does not substitute notes when inference is unavailable. Python 3.11 and the pinned dependencies are installed in this checkout's `.venv`; its interpreter base and FFmpeg executable are in ignored `.runtime/`. They do not depend on the earlier `/tmp` test environment. Keep these runtime directories local and out of Git.

## Processing and preserved data

1. Validate the complete original recording, decode the selected range, and reject silent/invalid inputs.
2. Load the actual model and run upstream overlapping-window inference.
3. Save raw frame activations (`note`, `onset`, `contour`) as compressed NPZ and extract raw note events on the assembled clip timeline.
4. Save exact extracted events, associated stable raw IDs, and the upstream raw-performance MIDI including its pitch bends.
5. Apply the project's validated cleanup settings, save editable performance notes, and generate a separate performance MIDI.
6. Optionally construct a canonical rational-beat score and its MusicXML, then atomically publish the attempt's database result. See [the notation model](NOTATION.md) for settings, preview, editing, and notation exports.

The UI reports the current real stage. It has no invented percentage or completion-time estimate. Raw artifacts are published for completed attempts; partial files in failed/canceled attempt directories are retained but not advertised as successful results. Each attempt owns a UUID directory. Writes are atomic where artifacts are produced, and completion requires the current worker's unexpired lease and processing state.

A completed attempt contains:

| File | Meaning |
| --- | --- |
| `normalized.wav` | Validated selected clip, mono 22,050 Hz PCM |
| `raw-model-output.npz` | Unmodified output activation arrays |
| `raw-note-events.json` | Verbatim extracted event values and stable raw IDs |
| `raw-performance.mid` | Upstream performance MIDI, unquantized |
| `notes.json` | Initial normalized editable note representation |
| `score.mid` | Cleaned performance MIDI, unquantized |
| `manifest.json` | Model/audio hashes, versions, extraction thresholds, cleanup audit and score semantics |
| `score.musicxml` | Optional provisional notation; not the source of timing truth |
| `canonical-score.json` | Initial rational-beat score used to construct that notation; later saved revisions live in SQLite |

Raw and cleaned MIDI are separate downloads. Raw MIDI preserves upstream pitch bends; the editable representation currently edits pitches/timing/velocity/parts, and its MIDI export does not carry the original bend curves. If velocity is unavailable for a manually added note, the MIDI writer uses 64; the JSON field remains null.

## Note schema and editing

Every new normalized note has `id`, MIDI `pitch`, `start`/`end` in seconds, nullable `velocity`, `part_id`, optional `voice_id`, nullable `model_score`, `provenance`, `source_note_ids`, `original_timing`, and `score_timing`.

- IDs are deterministic within a job's raw events and remain unchanged by edits. A new analysis attempt has its own identity namespace.
- `provenance` is `detected`, `edited`, or `manually_added`. Manually created notes have no invented model score, raw source IDs, or original timing.
- `model_score` is a model activation, **not** calibrated confidence or an accuracy percentage. Merged notes use the maximum source activation. Detected velocity is derived from that activation, not a measured physical playing force.
- `original_timing` preserves the original source event snapshot; merged source events remain independently inspectable through `source_note_ids` in raw JSON.
- Current `start` and `end` remain performance seconds relative to the selected clip. Add the project's clip offset to locate them in the original recording. The legacy `score_timing` field stays null: rational score positions now live in the separate canonical model and refer back to these source IDs.

`PUT /api/projects/{id}/notes` accepts `{revision, notes}`. Each submitted note contains `id` (null for a server-assigned new ID), `pitch`, `start`, `end`, `velocity`, `part_id`, and optional `voice_id`. A new client-generated UUID requires `new_note: true`; split/merge lineage may reference known `derived_from_ids`. The server validates identity, assigns provenance and preserves original metadata. It rejects unknown/duplicate IDs, invalid pitches, non-finite or out-of-range times, and stale revisions. A maximum of 10,000 notes may be edited per request. A stale edit returns HTTP 409, and the browser keeps the draft for review.

Each accepted edit writes a unique revision directory before an atomic database pointer swap. Raw model files and raw MIDI are never rewritten. Edited performance MIDI is regenerated, while stale draft MusicXML is removed from available exports. Older projects receive stable legacy IDs without inventing missing raw activations; raw artifacts are available only for analyses that actually preserved them.

The review workspace uses `PUT /api/projects/{id}/review` to save notes and canonical notation together, with both revision checks and an exact transaction snapshot in the response. See [REVIEW_WORKSPACE.md](REVIEW_WORKSPACE.md) for undo/redo, selection, playback and saved-revision exports.

## Configurable cleanup

`MINIMUM_NOTE_SECONDS` defaults to 0.08; `MERGE_GAP_SECONDS` defaults to 0.03. Upload controls can override them per project (ranges 0–1 s and 0–0.2 s). Settings are persisted and reused on retries. Raw extraction keeps events as short as two model frames (~23 ms), independently of the application's configurable cleanup cutoff.

Cleanup removes short events and merges only same-pitch/part fragments within the allowed gap or overlap when frame activations support continuity and there is no strong local onset or sudden activation rise. A small gap alone is insufficient: this protects repeated notes. Setting merge gap to 0 disables merging. The manifest records removals and merged source IDs. These rules are conservative heuristics, not a calibrated accuracy system.

## Long clips

The Basic Pitch adapter streams the upstream model's overlapping 43,844-sample windows at 22,050 Hz (7,680-sample overlap) into disk-backed global activation arrays. It extracts notes once across the assembled timeline, rather than concatenating separately transcribed chunks. Frame times use the exact window/sample positions; this avoids upstream nominal-frame-rate trimming dropping the tail of long recordings. The manifest records windows, covered source samples, activation intervals and the last activation time. Model output for each completed window is checkpointed atomically under the project's private directory, keyed by audio, model, runtime and settings, and reused after interrupted attempts. pYIN uses bounded contexts; separation retains overlapping windows and exact global sample positions.

The default full-source limit is 60 minutes. Decoding, native browser playback, waveform generation and model-window input avoid whole-waveform application buffers. Final note extraction is global and still needs memory proportional to duration. The 12-minute HTTP/restart audit verifies all 720 seconds and the ending MIDI event; see [LARGE_AUDIO.md](LARGE_AUDIO.md). Global assembly preserves timing coverage, not perfect note detection. The older 20-second sustained fixture produced an extra tail fragment, and the long fixture also has extra detections. Constant tempo/meter settings remain consistent over the whole score; expressive tempo maps are a later stage.

Scores remain bounded at 10,000 notes, 10,000 measures, and the export limits documented for engraving. A notation failure retains the full raw/cleaned notes and raw-performance MIDI and records `notation_unavailable_reason`; it does not silently shorten the transcription.

Official implementation: [Basic Pitch inference](https://github.com/spotify/basic-pitch/blob/v0.4.0/basic_pitch/inference.py), [note extraction](https://github.com/spotify/basic-pitch/blob/v0.4.0/basic_pitch/note_creation.py).

## Durable job states

`queued → processing → completed / failed / canceled`

- `POST /api/projects/{id}/cancel` cancels a queued/running job durably. It cannot publish afterward. The supervisor stops the isolated analysis process and its decoder process group; a child watchdog also checks the lease and parent lifetime on POSIX.
- `POST /api/projects/{id}/retry` creates a new attempt only for failed/canceled projects within their saved budget. The default is three total attempts, including the first, user retries, and restart recovery.
- An expired lease marks the old attempt interrupted and automatically queues another attempt if budget remains. Otherwise the project stays failed. History is retained.
- Graceful worker shutdown also interrupts and queues recovery within budget. Unexpected child exit or `JOB_TIMEOUT_SECONDS` (default 7,200 seconds; separation 14,400 seconds) produces an actionable failed attempt rather than an endless processing state.
- SQL transactions, a unique active-job index, per-attempt output directories, lease checks, and editor revision checks prevent duplicate workers or stale edits from overwriting accepted results.

CPU/process-group cleanup was tested on macOS. Windows does not provide the same POSIX process-group/parent watchdog behavior and was not validated in this run. This remains a local, single-user service; public deployment still requires authentication and project authorization.

## Reproduce validation

```sh
source .venv/bin/activate
export FFMPEG_BINARY="$PWD/.runtime/bin/ffmpeg"  # or your own system FFmpeg
PATH="$PWD/.runtime/bin:$PATH" RUN_MODEL_TESTS=1 python -m pytest backend/tests scripts/test_evaluation.py -q
python scripts/evaluate_transcription.py
pnpm -C scripts test:transcription
```

See [actual evaluation results](TRANSCRIPTION_EVALUATION.md) and [verification history](VERIFICATION.md). Generated audio is an evaluation fixture, never production transcription output. Synthetic results do not establish accuracy on voices, acoustic instruments, or commercial recordings.

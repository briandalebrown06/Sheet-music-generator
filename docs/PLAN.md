# Implementation plan

Repository inspection (2026-10-27): only the initial README; no app, dependency files, or repository instructions. Preserve the repository and introduce a React + TypeScript client with a Python API and independent worker.

1. **Foundation / initial single-part transcription:** responsive local studio, upload and browser microphone recording, project detail and original-audio playback; FastAPI health/capabilities, SQLite projects and durable processing jobs, bounded FFmpeg decoding, real Basic Pitch inference through a replaceable adapter. A missing engine must produce an actionable failure, never substitute notes. Save inferred note events for later review.
2. **Review and correction:** render MusicXML through OpenSheetMusicDisplay; implement persisted note editing, explicit tempo/meter/quantization choices, undo, and synchronized synthesized playback. Inference produces a draft requiring musical review.
3. **Export:** construct notation with music21, validate MusicXML and MIDI round trips, and add a real PDF engraving path. Initial MusicXML/MIDI integration may be available with optional analysis dependencies; no placeholder downloads.
4. **Full songs:** evaluate a separately licensed source separation adapter, selected-part workflow, stem previews, cancellation and resource budgeting. Do not promise automatic professional ensemble scores.

## Foundation acceptance

- Local file and microphone inputs create persisted projects and jobs through the same API.
- Refresh preserves projects; status, failures, retries and worker availability are visible.
- Separate worker atomically claims jobs and records real results or actionable errors.
- Upload and decoded-duration limits, local-only origins/hosts, and loopback defaults.
- `.env.example`, pinned dependencies, documented setup and stage boundaries.
- API/worker checks, TypeScript/build checks and browser checks, with unverified inference clearly disclosed.

Authentication and project-level authorization are required before any public deployment. This foundation is intentionally a single-user local service.

## Canonical notation implementation

1. Compile performance notes into a separate rational-beat score with explicit voices, measures, rests, chords, ties and pickup duration; retain original seconds and source IDs.
2. Construct MusicXML and notation MIDI from that canonical score. Add explicit musical settings, limited onset-based tempo suggestions, and correctable piano staff heuristics.
3. Persist score revisions independently from performance revisions; preview without mutation, reject stale saves and exports, and render MusicXML with locally served OpenSheetMusicDisplay.
4. Verify rhythm/measure invariants and MusicXML round trips, then exercise a generated audio upload through real Basic Pitch and visible browser notation.

## Review workspace implementation

1. Keep performance seconds and canonical score beats separate; synchronize selection through stable note IDs. Add note operations, explicit part/voice assignment and undo/redo.
2. Combine waveform, original/raw/edited/quantized playback, loop/cursor mapping and notation. Commit notes and score atomically with revision checks and persistent identity lineage.
3. Generate all notation exports from the saved canonical score. Verify a real headless PDF engraver, paginate vector pages and reject stale export revisions.
4. Exercise generated audio through real inference, browser editing, playback, save/reopen and exported XML/MIDI/PDF; inspect multi-page engraving and report platform limits.

## Mixed-recording implementation

1. Evaluate maintained separators and independently verify code/weight licenses; install and run the selected local engine behind an adapter.
2. Add explicit solo/mixed modes, aligned stem audition and persistent selected-part jobs while preserving the solo path.
3. Use part-specific pitch adapters, retain raw analysis/provenance, and connect each child to the existing review and saved-revision exports.
4. Bound separation time/context/memory/storage, clean abandoned attempts, and verify cancellation/retries/deduplication.
5. Evaluate a known-source generated mixture for leakage and pitch/timing errors; exercise the complete real mixed-song browser workflow.

## Deployment audit implementation

1. Audit and exercise the full real audio-to-export flow plus malformed input, limits, stale revisions and process failures in disposable data.
2. Add provisioned accounts, private owner-scoped routes, CSRF/rate/quota controls, persistent upload keys and durable leased deletion.
3. Verify browser authentication, mobile/keyboard behavior and protected media/exports, retaining the existing solo and mixed flows.
4. Prepare pinned frontend/API/worker images, a private local-volume database/queue/artifact topology, model setup, health probes and a staging launch checklist.
5. Record actual model/long-input/restart measurements and dependency/security findings; explicitly distinguish local verification from unperformed container/public deployment.

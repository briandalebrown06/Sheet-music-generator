# Transcription review workspace

The project detail screen combines decoded waveform peaks and audio transport, a note-event editor, rendered notation, musical settings, and saved-revision exports. The renderer displays canonical MusicXML; application state owns the edits.

## Editing and identity

Select a note in the event table, waveform note lane, or a bound score notehead. Stable source IDs connect the views. The score renderer associates parsed staff/measure/voice/beat/pitch with canonical events and their source IDs; ambiguous glyphs are left unbound instead of selecting the wrong event.

- Edit MIDI pitch, start seconds, duration, velocity, part, and voice. Moving a start preserves duration. Original timing remains available for comparison.
- Add or delete notes. New notes get client-created UUIDs validated by the server and remain identifiable after saving.
- Split inside a selected note. The first fragment keeps its ID; the new fragment gets its own ID and preserves known source lineage.
- Merge selected notes only when pitch, part, and voice agree and their intervals overlap or have gaps no larger than 0.05 seconds. The earliest selected note keeps its ID.
- Undo/redo maintains up to 100 editing states, including across saves within the open workspace. Reloading opens the persisted result and starts a fresh undo history.
- Parts become distinct canonical parts. Voices can remain automatic or be assigned explicitly from 1–32. Simultaneous notes with the same boundaries can form a chord in a voice; conflicting overlaps in one explicit voice require a different voice or corrected timing.

The backend keeps a note-identity registry for deleted notes so undo can restore an ID without inventing new detected provenance. Clients cannot supply arbitrary original timing or model scores. Derived edit lineage refers only to known saved identities.

## Preview and save

Performance edits affect the unquantized synthesizer immediately. **Preview score** rebuilds notation from the current draft without persisting it. **Save review** validates the notes, compiles the canonical score, writes edited performance artifacts, and commits the notes and score together in one SQLite transaction. Failed notation validation does not partially save the notes.

Both the source-note revision and score revision are checked. A competing save returns a conflict and retains the local draft. Original audio, raw events, and raw-performance MIDI are never rewritten by review edits. Saved projects reopen with their accepted note identities, assignments, score settings, and notation.

The legacy performance-only save endpoint remains available and marks older notation stale. Stale notation exports return HTTP 409 until a new canonical score is saved. Downloaded notation is always identified as a **saved revision**; an unsaved preview is not silently substituted into exports.

## Playback and alignment

| Mode | Source and timing |
| --- | --- |
| Original recording | Actual recording; the transport adds the selected clip's offset when seeking the original media. |
| Edited performance · unquantized | Current valid draft pitches and seconds, with simple synthesized tones. |
| Raw performance · unquantized | Preserved model event pitches and seconds, before edits and cleanup. Unavailable for older projects that did not preserve raw events. |
| Quantized score | Canonical beats, tempo, ties, and transposition mapped to clip seconds using `origin_seconds + beats × 60 / BPM`. |

The waveform is computed from actual decoded mono PCM; no decorative or generated waveform stands in for audio. The original is preserved. A separate cache holds the envelope. For legacy projects without a normalized clip, bounded FFmpeg decoding supplies the waveform.

Loop controls use clip-local start/end seconds and require at least 0.05 seconds. The waveform cursor and active note identities follow the transport. The synthesizer schedules a bounded look-ahead window on the Web Audio clock, reconnecting tied score fragments into sustained notes. It stops previous oscillators when edits, settings, or playback mode change. User actions start or resume the audio context; unmounting closes it.

Synthesized tones are an audition aid, not a reproduction of the recorded instrument. Raw audition uses discrete pitches and does not reproduce original pitch-bend curves. Raw MIDI retains the upstream bends where the model produced them. Audio-device latency, browser scheduling, and expressive tempo can affect perceived alignment. Quantized playback is disabled when its displayed score is stale relative to the draft.

## Exports

Completed scores now open with **Your sheet music** before playback and editing tools. View the inline/full-screen score or download PDF, MIDI, and MusicXML directly. Pending generation, failures, and retry/download links stay beside the relevant button. Unsaved changes show **Save changes** before allowing downloads; filenames contain the title, written key-signature pair, and saved revision. See [the focused verification report](SHEET_MUSIC_DOWNLOADS.md).

MusicXML, notation MIDI, and vector PDF use the saved canonical score. Raw-performance MIDI is separately labeled and uses immutable detected events. Edited performance MIDI remains available with unquantized edited seconds.

PDF is engraved by a real headless notation engine into paginated vector pages. See [PDF_EXPORT.md](PDF_EXPORT.md) for the tested engine, dependency installation, limits, and the installed MuseScore CLI issue. Export responses carry `X-Score-Revision` and `X-Notes-Revision`; longer MIDI/PDF generation rechecks revisions before returning. Download links pass the displayed saved revision as `?revision=N`, which rejects a newer competing save instead of silently downloading a different revision. Stale generation is rejected rather than attributed to a newer score.

## API additions

- `POST /api/projects/{id}/review/preview`: transient draft score from `{notes_revision, score_revision, notes, settings}`.
- `PUT /api/projects/{id}/review`: atomic performance-and-score save using the same body.
- `GET /api/projects/{id}/waveform`: real peak envelope and selected-clip duration.
- `GET /api/projects/{id}/playback/raw`: preserved raw events for discrete-pitch audition, or an explicit unavailable response.
- `GET /api/projects/{id}/score/exports/pdf`: current saved-revision vector PDF.

## Reproduce checks

```sh
source .venv/bin/activate
python -m pip install -r backend/requirements-engraving.txt
PATH="$PWD/.runtime/bin:$PATH" RUN_MODEL_TESTS=1 python -m pytest backend/tests scripts/test_evaluation.py -q
pnpm -C scripts test:playback
pnpm -C scripts test:review
```

Browser checks use disposable project data, explicitly generated audio, and real Basic Pitch. They do not edit the user's uploaded project. See [VERIFICATION.md](VERIFICATION.md) for checks actually completed and platform limits.

## Piano accompaniment

Use **Create playable accompaniment** below the primary sheet-music actions to derive a separately saved project. The source remains available through its link. Configure left/right reach independently, review measure/hand/note-specific issues, and use **Revoice for comfort** for another reversible version. Exact transposition only changes the written interval and reruns checks. Accompaniment-mode primary playback and score exports use the derived saved arrangement; raw-performance MIDI stays separately labeled.

The note inspector supports physical hand, musical role, edit locking, optional manual finger number and explicit pedal/key-release annotations. Edits participate in undo/redo and saved-revision checks. Read [PIANO_ACCOMPANIMENT.md](PIANO_ACCOMPANIMENT.md) for assumptions, limits, tests and before/after examples.

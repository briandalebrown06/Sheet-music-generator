# Canonical musical notation

Notation is a separate, versioned interpretation of the detected performance. Raw model events, editable performance-note seconds, and raw-performance MIDI are preserved. A musical score is generated from the current performance notes plus explicit musical settings; it is not a renamed timing-only MIDI export.

## Model and timing

The canonical score uses quarter-note beats with rational numerator/denominator values for positions and durations. For example, an eighth-note triplet is `1/3` of a quarter-note beat. A 6/8 bar is three quarter-note beats; the time signature remains 6/8 for engraving and grouping.

The model contains musical parts, staves, measures, explicit voices, and note/chord/rest events. Transcription starts with one part; review assignments can create up to 16 parts with aligned measures. Notes refer back to their performance-note IDs and original seconds. Rendering and notation MusicXML/MIDI/PDF exports are generated from this same model. Performance and raw MIDI remain separate exports with their original timing semantics.

Quantization controls only the notation interpretation. Simultaneous notes with compatible quantized durations form chords; independent overlaps are allocated explicit voices. Notes crossing barlines are split with ties. Rests fill the remaining duration in each voice, and complete bars have the exact duration required by the time signature. A declared pickup is the intentional short first bar. The selected grid bounds rhythmic granularity, avoiding arbitrary sub-grid rests from small timing errors. Detected notes shorter than one grid unit are extended for notation with an explicit warning; their original duration is preserved. The first implementation limits a score to 10,000 performance notes, 1,000 full measures, and 32 voices per staff.

## Controls

| Control | Meaning |
| --- | --- |
| Tempo | Quarter notes per minute. Automatic estimation is a suggestion from detected onset spacing, with an assumed fallback when the evidence is insufficient. Manual tempo overrides it. Half/double-tempo ambiguity and expressive timing still require review. |
| Time signature | Manual bar length and grouping. An unconfirmed default is labeled assumed. |
| Key signature | Manual number of flats or sharps, from seven flats through seven sharps. It does not establish the recording's key or mode automatically. |
| First downbeat | Seconds from the selected audio clip to the start of the first **full** bar. Add the saved clip offset to locate it in the original recording. |
| Pickup length | Quarter-note beats preceding that full bar, entered as a rational value such as `1`, `1/2`, or `1/3`. Use `0` for no pickup. |
| Quantization | Quarter, eighth, sixteenth, eighth-triplet, or sixteenth-triplet grid. Straight and triplet grids are explicit choices. |
| Clef | Automatic pitch-range heuristic or manual treble, bass, alto, or tenor. |
| Transposition | Semitone shift applied to the notation interpretation; it does not alter detected/performance pitches. The written key signature remains the selected value and must be changed separately when wanted. |
| Score zoom | Browser display scale only. It does not change the canonical score. |

At 120 BPM, a one-beat pickup and a first full downbeat at 0.5 seconds place the start of the pickup at zero. Setting a downbeat/pickup combination that would leave detected notes outside the representable score requires correction; source notes are not silently erased.

Defaults and inferred choices are displayed as assumptions. Editing a setting confirms that field manually; automatic tempo and clef remain heuristic choices. The application does not claim to infer meter, key, performer intent, or professional engraving from arbitrary audio.

## Optional piano staves

In ordinary transcription mode, the piano option places unassigned notes above or below a configurable MIDI-pitch boundary on treble and bass staves. This is a pitch-based engraving heuristic for each assigned part, not identification of the performer's actual hands. A per-note staff override lets the user correct crossings and inappropriate assignments. The original source-note identity stays the same when the displayed staff changes.

The explicit [piano accompaniment pass](PIANO_ACCOMPANIMENT.md) instead assigns physical hands using held keys, keyboard shape and phrase movement. Hand, staff and voice are independent fields. Accompaniment readability uses those hands as a starting point, retains manual staff overrides and prints hand indications for cross-staff assignments. Original timing and detected events remain available; the derived project is labeled editorial accompaniment. Manual key releases distinguish physical holding from explicit pedal resonance, and optional manual fingerings are exported without generating guessed finger numbers.

## Preview, save, and concurrency

**Preview score** generates transient canonical notation and MusicXML without replacing saved notation or changing performance notes. The review workspace's **Save review** publishes edited notes and a new notation revision together. The notation-only API still supports separate score saves. Reloading the browser retrieves the saved revision. See [REVIEW_WORKSPACE.md](REVIEW_WORKSPACE.md) for note editing, synchronized playback and atomic saves.

Both operations identify the source performance-note revision. Saving also checks the existing notation revision, so stale requests cannot silently overwrite concurrent changes. A performance edit makes older saved notation stale; regenerate and save before downloading current notation exports. Raw transcription remains available for comparison and reprocessing.

The score API follows the configured local or authenticated hosted mode, with per-project authorization:

- `GET /api/projects/{id}/score`: saved model, revision, staleness, and default settings.
- `POST /api/projects/{id}/score/preview`: transient model and MusicXML from `{notes_revision, settings}`.
- `PUT /api/projects/{id}/score`: save from `{notes_revision, revision, settings}`.
- `GET /api/projects/{id}/score/musicxml`: current saved score for rendering.
- `GET /api/projects/{id}/score/exports/{musicxml|midi|json|pdf}?revision=N`: notation exports from the saved canonical model; an optional requested revision must match.

## Libraries and rendering

The installed music21 9.9.1 constructs and serializes the notation. Its documented duration model supports exact `Fraction` values and tuplets; explicit `Measure` and `Voice` streams preserve bar and overlap structure. OpenSheetMusicDisplay 2.2.1 renders that MusicXML as SVG in the browser. OSMD is a renderer; the application owns musical settings, canonical state, and revision handling. It is loaded on demand and served locally with the frontend.

Primary documentation: [music21 rational durations and tuplets](https://music21.org/music21docs/usersGuide/usersGuide_19_duration2.html), [music21 streams, measures, and voices](https://music21.org/music21docs/moduleReference/moduleStreamBase.html), [OSMD usage](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay/wiki/Getting-started), [OSMD 2.2.1 release](https://opensheetmusicdisplay.org/blog/opensheetmusicdisplay-2-2-1-released-66-bug-fixes-smarter-dynamics-cleaner-slurs/). Code-license review is in [DEPENDENCIES.md](DEPENDENCIES.md).

## Reproduce verification

```sh
source .venv/bin/activate
python -m pytest backend/tests/test_score.py -q
pnpm -C frontend build
pnpm -C scripts test:notation
```

The browser test launches disposable API/worker/frontend instances on ports 8001/5174, uploads an explicitly labeled generated melody, and runs real Basic Pitch before score generation. Its input is synthetic audio, not injected note events or a hardcoded score. It checks SVG rendering, settings, preview/save/reload, raw preservation, exports, staff corrections, conflicts, and staleness. It saves desktop/mobile screenshots and a report in a temporary directory. On 2026-10-27, all **54 browser assertions passed** after adapting the test to the review workspace in installed Google Chrome. Export round trips confirmed the selected 3/4 meter, one-sharp key signature, bass clef, +2-semitone transposition, triplets, pickup measure, and exact rational duration sums for every voice. Desktop and 390px mobile screenshots were visually inspected. The implementation was exercised with actual model output from an eight-note generated melody; the model produced ten notes, so the displayed score still requires transcription review. Installed Google Chrome, Playwright, FFmpeg, and the real analysis environment are required.

The review workspace adds synchronized playback and [paginated PDF engraving](PDF_EXPORT.md). Variable tempo, mixed straight/triplet rhythms within one grid, advanced engraving edits and identification of the original performer's actual hands are not implemented. Mixed recordings support source separation, and derived accompaniment supports explicitly heuristic hand assignment. Readability and transcription errors still need musical review.

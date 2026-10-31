# Piano accompaniment: playability and readability

This document retains the initial regression measurements below. The later phrase-refinement engine (`scorecraft-piano-phrase-v2`) adds bounded movement repair without changing assessment thresholds. The subsequent [three-edition workflow](PIANO_EDITIONS.md) adds musical-setting evidence, separate vocal melody/cue treatment and independently saved outputs; its current private examples are separate from this earlier report.

The app now has an explicit **Comfortable accompaniment** action for a completed pitched-part transcription. It creates a separate, editable piano arrangement from saved detected/corrected notes. It does not claim to recover the original piano performance, compose missing accompaniment, or identify an isolated piano in an `other` stem.

The original transcription remains available through **Open source version**. Each changed revoice creates another saved project with its own note/score revisions and exports. An identical repeated derivation request reopens the existing result rather than making a duplicate. In accompaniment mode, the prominent **Your sheet music** preview and PDF, MIDI and MusicXML buttons use the saved accompaniment. Raw-performance MIDI stays separately labeled and retains the original detections.

## What caused the defect

The previous score compiler had a pitch-threshold staff split, but no physical-hand model. Different detected release times could create several notation voices. A mixed `other` part could therefore place multiple instruments' notes onto one staff, with large held-note spans, dense rests/ties, and crowded engraving. Moving a note to the bass staff did not establish that a left hand could reach it.

The earlier local project used in the initial review was available and was inspected as an existing generated score. Its saved source did not contain actual hand assignments, so the baseline hand audit is explicitly **provisional**: it uses the old pitch split and also reports missing hand assignments. Its baseline error total must not be interpreted as that many independently verified human-performance failures. No external annotated pianist score or pianist test recording was supplied; none is claimed to have been examined.

Octave transcription errors, separation leakage, instrumental identity, pedal and important musical roles are not reliably inferred by this pass. Same-key duplicate attacks are audited; redundant doublings may be removed in the derived arrangement. Register-based bass/harmony priorities are labeled editorial assumptions. Mark characteristic riffs, countermelodies, bass lines and locked edits before deriving when their importance cannot be inferred from the events.

## Two independent passes

1. **Physical playability** (`backend/app/piano.py`) searches competing hand assignments and voicings across successive attacks. It retains held-key obligations and previous hand positions in a bounded phrase beam. Candidate repairs redistribute notes, octave-revoice inner material, remove redundant doublings or secondary textures, and explicitly shorten secondary holds. An automatically inferred bass sustain may be articulated at the next bass attack while retaining its bass pitch and onset. Explicitly marked bass/riff/countermelody roles and locked notes are protected. No automatic arpeggio or invented pedal is used to hide an impossible chord.
2. **Notation readability** (`backend/app/readability.py` and `score.py`) aligns only small unlocked timing discrepancies, bounded by 40 ms and 20% of the selected grid, and reconciles near-identical chord releases. It rebuilds automatic voices by register and overlap while preserving locked assignments. The canonical rational-beat model then constructs full measures, pickups, rests, ties and tuplets. Intentional offbeats, repeated attacks and independent long holds are retained. The resulting notes are assessed again, so the saved report describes the final arrangement rather than an intermediate candidate.

Hand, staff and voice are separate concepts:

- `hand` identifies the left or right physical hand responsible for a note.
- Staff assignment determines where it is printed. The default accompaniment starts with hand-based treble/bass placement. Extreme outliers use the clearer existing staff with explicit R.H./L.H. labels, without changing physical hand, pitch or timing. Manual staff overrides take priority.
- A voice represents an independent musical line and its rhythmic continuity. It is not an extra hand.

Non-cue source parts are combined into one pianist's grand staff for accompaniment. An explicitly identified vocal cue remains a separate optional staff, never an automatic right-hand doubling. A source containing only a vocal melody is rejected as an accompaniment source; select a transcribed instrumental part instead.

## Comfortable accompaniment settings and checks

| Setting/check | Behavior |
| --- | --- |
| Left and right reach | Independent defaults of **9 semitones**, adjustable from 5 to 12. This is a conservative product choice, not a comfort guarantee. |
| Finger load | At most **five independently pressed keys per hand** by default; the API can select a smaller maximum. |
| Keyboard range | Written notes are checked against the standard 88-key MIDI range, 21–108. |
| Held keys | Every attack is checked together with notes that the same hand must still physically hold. |
| Keyboard shape | An internal ordered finger-capacity heuristic considers uneven intervals and approximate black/white-key geometry. Count and span alone do not establish compliance. |
| Movement | Feasible hand-position intervals, register displacement, preceding release times, attack spacing and intended score tempo are considered. Nearby notes that can use different fingers are not automatically treated as full hand jumps. |
| Phrase continuity | Recent crossed hand positions and frequent changes of hand within a marked line are flagged. A large distance between the two hands by itself is not an error. |
| Manual fingering | Explicit finger numbers are preserved. Assigning one finger to different simultaneously pressed/held keys in the same hand is an error. No guessed finger numbers are generated. |

The report identifies the measure, hand, affected stable note IDs and problem. Hard errors cover the configured reach/key/shape/range rules and explicit finger conflicts. Movement, crossing and line-handoff warnings remain heuristic review items. `hard_constraints_passed` can therefore be true while `passed_automated_constraints` is false because warnings remain. Neither field means a pianist has approved the music; the report records `pianist_review: not_performed`.

The search is bounded and conservative. It does not exhaust all human fingerings or guarantee an optimal reduction. Changing the profile can produce another useful candidate, but a smaller reach setting does not authorize changes to locked notes. Impossible locked passages remain intact and are reported for manual correction.

## Pedal, locks and exact transposition

A note's `end` is its sounding end. An earlier `key_release_seconds` is treated as physical release only when `pedal_sustained` is explicitly set. Otherwise the audit conservatively treats the sound as physically held and warns about an unconfirmed release. Pedal cannot make an impossible simultaneous attack fit one hand. Pedal is not detected from the recording, and it is not silently added during repair.

The note editor exposes hand, lock, musical role, manual fingering and physical-release metadata. Edited/manually added source notes with no explicit lock value are protected by default; algorithmic arrangement edits carry `locked: false` so subsequent revoicing remains possible. Lock any corrections that must survive another repair. Changes and omissions retain source IDs and original timing, with a separate arrangement/readability audit. A locked cue is retained even when the default optional-cue setting would otherwise omit it.

Exact transposition remains the score's explicit semitone operation. It changes written pitches together and reruns range, reach, movement and notation checks. It does **not** silently change octaves, omit notes or alter intervals to make the result easier. **Revoice for comfort** is a separate, reversible action that creates another saved arrangement. Its octave and texture changes are listed as editorial changes. Save the current notes/score before deriving; stale revision checks prevent exporting or deriving from an inconsistent snapshot.

## Readable notation and exports

Accompaniment notation uses stable register-ordered voices, explicit stem directions, beat-aware beaming/rest segmentation, key-aware accidental spelling, and fixed clefs unless the user chooses otherwise. Automatic voices print a single set of rests only for genuine staff-wide silence; hidden rhythmic space still fills each voice exactly. Explicit manual voice rests remain visible. Cross-staff hand instructions remain explicit. Manual finger numbers and explicit pedal directions are written to MusicXML; no fingering solver is advertised.

The Verovio PDF path uses readable staff dimensions, staff/system spacing, smart system breaks and additional A4 pages. It does not shrink an arbitrarily dense score to fit a fixed page count or export an application screenshot. MusicXML, current score MIDI, PDF and quantized-score playback consume the same saved canonical score. Original and edited-performance playback remain separately labeled, since their timing can differ from score quantization.

Overlapping same-pitch voices use independent MIDI channels so a note-off cannot prematurely end another voice. This applies to current score MIDI and edited-performance MIDI; immutable raw model MIDI is preserved. Exports that would require more than 15 non-percussion channels return a clear error instead of truncating notes; MusicXML remains available.

The notation audit reports remaining dense voices and ledger-line concerns. These counts are review cues, not a collision detector. Successful XML parsing or MIDI playback does not establish that the notation is visually readable or physically playable. Rendered PDF inspection and pianist review are separate checks.

## Measured before/after examples

The score-only evaluation on 2026-10-27 used one explicitly generated regression fixture and an available existing project snapshot. It never substituted fixture events for model inference. The evaluator asserted that the original notes were unchanged and independently compared exported MusicXML pitches, onsets and durations against MIDI after following voice/staff positions and ties.

| Result | Generated regression | Existing generated project |
| --- | --- | --- |
| Source notes → arrangement notes | 144 → **120** | 1,474 → **1,326** |
| Final hard constraint errors | **0** | **0** |
| Final playability warnings | **0** | **24**: nine movement warnings and fifteen line-handoff warnings |
| Final maximum hand span | Left 4 / right 9 semitones | Left 9 / right 9 semitones |
| Final maximum pressed keys | Left 2 / right 3 | Left 4 / right 5 |
| Maximum notation voices per staff | 2 → **1** | 5 → **3** |
| PDF pages | 1 → **1** | 4 → **10**, with a two-staff piano part and increased spacing |
| XML round trip and MIDI agreement | Passed before and after | Passed before and after |
| Pianist validation | **Not performed** | **Not performed** |

In the generated example, visible rests decreased from 120 to 88. The existing example expands from one crowded staff to two staves while visible rests decrease from 661 to 223; 802 redundant automatic rest fragments remain hidden in the duration-complete model. Twenty-four extreme-register notes use the clearer staff with explicit hand indications. Independent held lines and their ties are retained where needed.

The existing example still needs review at these passages:

| Concern | Measures / hand |
| --- | --- |
| Rapid movement | Left: 3, 11, 29, 31, 43, 50, 52, 69. Right: 66. |
| Frequent line handoffs | Both hands: 18, 19, 23, 38, 41, 43, 65, 66, 67. Some measures contain more than one warning. |
| Ledger lines | Bass measure 67 retains four estimated ledger lines; the opposite staff would be worse. Review its register/clef manually. |
| Three independent voices | 43 staff-measure warnings remain, including treble 20, 22, 25 and bass 25–27; the complete report lists every affected measure and source note. |
| Mixed hands on one staff | Ten chord warnings identify explicit R.H./L.H. pitch labels that need attention. Staff location never overrides hand assignment. |

The zero-hard-error result does not erase those warnings or establish recognizability, comfort, expressive quality, or suitability for every pianist. This is a meaningful automatic repair with explicit remaining review work, not a claim that the reported product defect has been human-validated as resolved.

The before/after PDF, MusicXML, MIDI, canonical JSON and complete reports are written under the ignored local `output/pdf/piano-review/` directory. `development-*` outputs contain generated events; `existing-project-*` outputs and the optional snapshot can contain private musical content and must not be committed or published automatically. This document intentionally contains no private title, project ID or note IDs.

## Reproduce and inspect

Use the installed Python analysis/engraving environment and the existing frontend setup:

```sh
# Generated score-only before/after artifacts; no live project is changed.
.venv/bin/python scripts/evaluate_piano.py

# Optional private snapshot regression; the snapshot directory must also contain
# the previously exported existing-project-before.musicxml.
.venv/bin/python scripts/evaluate_piano.py \
  --project-snapshot /path/to/private/existing-project-source.private.json

# Core, notation and persistence/authorization regressions.
DATA_DIR=/tmp/scorecraft-piano-test-default-app .venv/bin/python -m pytest \
  backend/tests/test_piano.py backend/tests/test_readability.py \
  backend/tests/test_piano_api.py -q

# Isolated Chrome end-to-end accompaniment/review/export workflow.
pnpm -C scripts test:piano
```

The tests cover oversized chords, conflicting held keys, fast phrase jumps, explicit pedal release versus held sound, dense multi-part input, shape constraints beyond count/span, manual finger conflicts, locked edits, repeated fast triplet attacks, exact transposition and separate revoicing. Readability/API checks cover pickups, ties, tuplets, valid measure durations, voice/rest handling, revision conflicts, persistence and canonical export consistency. Final local verification on 2026-10-27: **297 backend/evaluation tests passed**, including actual installed model and separation tests; **62 Chrome piano-workflow assertions passed**, including desktop/mobile checks; the frontend typecheck/build passed. Two upstream Python deprecation warnings and the existing OSMD bundle-size warning remain. The generated and existing examples' browser playback plans also matched all MIDI pitches, onsets and durations (120 and 1,326 events respectively).

All ten final existing-project PDF pages and both one-page generated comparisons were rendered and visually inspected. The original four-page PDF was retained as evidence of the earlier dense engraving. Staff sizes, page margins and systems were intact; the identified rest stacks and extreme final ledger column were resolved. This is visual software verification, not performance by a pianist. The live API saved a separate arrangement, reopened it, returned all three revision-1 exports, and verified that the source notes and canonical score remained unchanged.

For a practical review, open the completed source, save corrections, set the two reach limits, and create the accompaniment. Listen to original and quantized-score playback, inspect the listed measures, lock retained edits, and save. Open/download the prominent PDF, MIDI and editable MusicXML. Transpose, review the refreshed constraints, and use the separate revoice action only if desired. Render every exported PDF page and inspect staff size, margins, systems and symbol collisions; then ask a pianist to play representative passages at the intended tempo. No physical pianist validation, expressive tempo-map inference, pedal detection or broader commercial-song accuracy evaluation was performed by this implementation.

# Three explicitly different piano editions

The score chooser on a completed instrumental project creates separate persistent projects. It never overwrites the source notes or claims that an editorial arrangement was detected in the recording.

- **Faithful instrumental transcription:** retains every supplied instrumental event's pitch, start, end, velocity and ID, including possible errors. Hand placement is metadata. Dense input can remain physically unplayable.
- **Singer's piano accompaniment:** reduces the actual instrumental events with the existing two-hand pass. An aligned vocal transcription may appear on a separate cue staff; that staff is excluded from piano MIDI/playback.
- **Solo piano with melody:** protects the separately transcribed vocal line while reducing competing instrumental texture. An explicit solo melody register control offers the original octave or an octave above/below; the chooser proposes an octave up for a low detected vocal register and leaves higher or locked vocal sources at their original register. This is an editorial piano placement, not a claimed vocal pitch correction. Nonzero shifts reject locked/manual vocal corrections. Secondary register changes, removals and hand changes are audited. It does not invent harmony. Source separation leakage and mistaken vocal notes can remain.

Only vocal and instrumental children of the same mixed project, with matching clip origins/durations and an accessible current vocal revision, may be combined. Solo mode requires a completed vocal transcription. New edition IDs, source references, raw timings, revision fences and a copied vocal-note snapshot preserve lineage. Duplicate requests reuse the existing edition. Subsequent edits and all exports use the saved canonical score. Deleting either instrumental or vocal source cascades to dependent editions and their revoices; private artifacts are removed by the existing worker cleanup. A migration backfills older vocal dependencies and completes already-requested deletions.

## Musical interpretation before engraving

`GET /api/projects/{id}/musical-analysis` runs bounded, standard-library analysis of saved notes; no audio model or long-running decoder is invoked in an HTTP request. It returns tempo aliases, provisional grid phase, key-template candidates, uncalibrated evidence metrics, warnings and proposed settings. The chooser requires explicitly selecting these estimates; they apply only to new editions. Existing corrections remain untouched.

The tempo search samples at most 768 distinct attack clusters and evaluates short windows before refining over the complete span. An integer tempo is proposed only within 0.05 BPM and when rounding adds at most 25 ms drift over the observed span. Exact fitted values remain in the evidence. An eighth-note lattice cannot identify meter, bar downbeat, swing or expressive tempo changes. Pitch-class correlation cannot establish a unique major/minor tonic, modulation, chord roots, or harmonic function.

`estimated_fields` keeps selected estimates distinct from manually confirmed settings through save/reopen. A numeric estimate is not relabelled manual. Changing a setting directly clears its estimated provenance. Both major and relative-minor interpretations remain visible in the key-signature control.

A separately identified melody/cue can use a sixteenth-note grid while the instrumental texture uses the selected straight grid. This prevents many short vocal attacks from becoming unintended simultaneous chords. The canonical compiler and physical assessment use the same per-note grid. Mixed straight/triplet notation still requires explicit editing; it is not silently approximated.

The conservative melodic duration pass operates only on explicitly tagged melody/cue events, with bounded corrections, preserved original timing and per-field logs. Instrumental rests, protected edits, repeated attacks and distinct physical releases remain intact. The API rejects duration normalization if it increases physical hard conflicts. No dynamics, slurs, fingerings, lyrics, form or phrase breaks are invented. Vocal cues choose treble or bass from their actual register, with a manual override; this never transposes sounding pitches. Cross-staff hand directions are consolidated at each onset, with explicit hand returns and a printed scope legend, instead of repeated overlapping labels.

## Lighter texture and phrase continuity

`PianoSettings.target_keys_per_hand` is a separate editorial density target, default **3**, configurable from 1–5. The existing `max_keys_per_hand` remains the physical count limit (default 5); passing it never establishes comfort. The target counts distinct keys already held as well as newly attacked keys. Reach, uneven keyboard geometry, physical releases, previous/following attacks and hand continuity remain independently assessed with unchanged thresholds.

The **Simplify and smooth movement** action derives a new saved project from the current arrangement, preserving its corrections, score settings and original source lineage. Optional unlocked inner voices may be removed or explicitly articulated. The pass retains every canonical attack and protects melody, bass lines, marked riffs, countermelodies, manual fingerings and locked edits. Automatically inferred bass notes may be octave-revoiced in an explicitly recorded phrase repair; their IDs, attacks and pitch classes remain traceable. No pedal is inferred to conceal an otherwise impossible held-key demand. Every accepted change is checked against the complete score; bounded budgets and protected conflicts are reported rather than hidden.

Repeated source figures are kept recognizable through consistent local voicing decisions. Random pitch variation, timing jitter and invented accompaniment are not used to make output seem less repetitive. A faithful transcription can legitimately contain repetition or unusual AI-generated writing. Simplifying that writing is an explicit arrangement choice, not evidence that the original notes were misdetected. AI-generated and human-performed recordings use the same transcription and validation path; neither source type makes detector output automatically accurate.

The playability panel reports the actual maximum held keys in each hand, positions exceeding the selected target and protected exceptions. Automatic notation lanes are compacted after an entire overlap episode ends, preserving continuous held lines and barline ties. Explicit manual voice assignments are unchanged. This avoids carrying unnecessary lanes through later passages; it does not merge independent musical lines or alter playback.

## API

- `GET /api/projects/{id}/piano-editions`: reopen saved derived editions.
- `POST /api/projects/{id}/piano-editions`: `notes_revision`, `score_revision`, `edition_mode` (`faithful`, `accompaniment`, `solo_piano`), `settings`, `piano_settings`, optional `melody_project_id`/`melody_notes_revision`, and solo-only `melody_octave_shift` (`-12`, `0`, or `12`; API default `0`).
- Existing `/accompaniment`, preview, review, exact-transposition and export routes remain available.

Changing the transposition moves pitches exactly. The written key-signature control remains explicit; choose its destination spelling too. Revoicing is a separate, reversible operation and reruns hand checks. A filename includes title, written key signature and saved score revision.

## Verification and limitations

Tests include event preservation, protected lead melody, register reduction, bounded secondary conflict repair, independent rhythmic grids, canonical bar duration and MIDI/XML agreement, idempotent creation, reopen/revision behavior, source authorization, stale-source rejection and private deletion cascades. The reference study documents actual inspected published-score pages and their separate composition, edition and recording rights.

Neither constraint compliance nor visual comparison proves that a score sounds natural or is comfortable for a particular pianist. Listening and pianist review have not been substituted by synthetic checks. A permission-cleared, exact-edition human-performance corpus and musician feedback are still needed for broad perceptual claims. The app offers [locally hosted sampled piano](PIANO_PLAYBACK.md) and simple tones. Recorded timbre improves the audition, but does not reproduce a pianist's interpretation or validate the transcription.

Run the focused checks with the installed environment:

```sh
DATA_DIR=/tmp/scorecraft-edition-check .venv/bin/python -m pytest \
  backend/tests/test_piano.py backend/tests/test_piano_editions.py \
  backend/tests/test_edition_readability.py backend/tests/test_musical_analysis.py \
  backend/tests/test_piano_api.py backend/tests/test_piano_editions_api.py -q
pnpm -C frontend build
pnpm -C scripts test:playback
```

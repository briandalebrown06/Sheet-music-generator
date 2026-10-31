# Published-song notation reference study

Reviewed 27 October 2026. This is a visual notation study, not an accuracy benchmark or a listening review. Three published song examples were inspected as rendered PDF pages. Two publishers provide matching synthesized audio; a human recording was located for the third, but exact edition alignment was not established. No claim is made that a human recording was heard, aligned, or transcribed during this study.

Reference downloads and page images are confined to the ignored `output/private/notation-references/` directory. No reference audio, score scan, modern arrangement, or extracted musical passage is distributed as an application fixture. The regression fixtures are newly generated note events.

## Sources and observed notation

### Stephen Foster: Beautiful Dreamer

- Historical edition: Wm. A. Pond, 1864, voice and piano, confirmed by the [Johns Hopkins Levy collection](https://levysheetmusic.mse.jhu.edu/collection/067/012).
- Inspected the [five-page historical scan](https://www.sheetmusicsinger.com/wp-content/uploads/2022/12/Beautiful-Dreamer.pdf), PDF pages 2 and 3, printed music pages 2 and 3. E-flat major, 9/8: a piano introduction precedes a distinct vocal staff. Vocal dotted notes and ties coexist with grouped accompaniment eighths. The bass uses sustained values and deliberate rests. Page 3 changes accompaniment texture, then finishes with piano alone while the voice rests. The key signature avoids individually marking every diatonic flat.
- The [source page](https://www.sheetmusicsinger.com/beautiful-dreamer/) pairs the scan with James Pitt-Payne accompaniment MIDI and a Fred Feild performance. The MIDI filename references Al Jolson; an exact match to this historical edition was not established. The [Hampsong Foundation reference](https://www.songofamerica.net/song/beautiful-dreamer) identifies a 2001 Hampson/Rieger voice-piano recording, also not independently edition-aligned.
- Historical composition/edition: public domain; a separately catalogued [1865 Pond scan has an explicit public-domain mark](https://commons.wikimedia.org/wiki/File:Beautiful_Dreamer_Stephen_Foster_(Pond_1865).pdf). That mark does not license the modern performances or MIDI, for which reuse permission was not found.

### Franz Schubert: Ständchen, D.957 no.4

- [Publisher's score and matching audio](https://www.mfiles.co.uk/scores/franz-schubert-standchen-serenade.htm), [PDF](https://www.mfiles.co.uk/scores/franz-schubert-standchen-serenade.pdf), [MP3](https://www.mfiles.co.uk/mp3-downloads/franz-schubert-standchen-serenade.mp3).
- Inspected all four PDF pages. D minor, 3/4: the opening has separate voice and piano staves, offbeat staccato chord patterns, and half-note basses followed by quarter rests. Vocal triplets and dotted rhythms coexist. Later pages contain sustained syllables, answering piano figures, endings, and a postlude. These show why “make everything legato” or “use one rhythmic value everywhere” would damage the music. Repeats, dynamics, staccato, grace notes, and fermatas are authored instructions, not evidence the transcription engine can infer them.
- The publisher explicitly uses clarinet to illustrate the voice in its synthesized audio. This is a score/audio pairing, not a human vocal recording. Local FFmpeg metadata inspection reported MP3, stereo, 44.1 kHz, approximately 3:08; no perceptual or note-alignment test was performed.
- Schubert's composition is historical public-domain material. This modern layout, arrangement, and synthesized performance remain subject to the publisher's terms below. They are research references only.

### Auld Lang Syne

- [Publisher's keyboard edition and matching synthesized audio](https://www.mfiles.co.uk/scores/auld-lang-syne.htm), [PDF](https://www.mfiles.co.uk/scores/auld-lang-syne.pdf), [MP3](https://www.mfiles.co.uk/mp3-downloads/auld-lang-syne.mp3).
- Inspected the single PDF page. F major, 4/4: a quarter-note pickup, recurring dotted-quarter/eighth melody, compact right-hand harmonization, and longer left-hand support make the melodic line identifiable. The ending balances the pickup; repeats structure the form. Chord symbols and guitar grids are authored harmonic guidance. Some chord-symbol font glyphs rendered as boxes locally; the note engraving was visible. That rendering issue is not an example to reproduce.
- The publisher also supplies separate melody-instrument parts and a piano-accompaniment edition, demonstrating that a keyboard solo and an accompaniment are distinct editorial outputs. The accompaniment PDF was not visually inspected, so no claim is made about whether it doubles the melody.
- This is a traditional composition, but the inspected modern arrangement and its synthesized recording are copyrighted under the publisher's terms. No human performance was evaluated.

### Modern reference rights

[Music Files Ltd's terms](https://www.mfiles.co.uk/copyright.htm) distinguish historical source music from its copyrighted arrangements, sheet-music layout, MIDI, and MP3 performances. The terms allow personal use, require permission for redistribution, and describe limited noncommercial uses. These files must not be bundled, adapted into an app example, or treated as openly licensed evaluation recordings without separate permission. A public-domain composition does not automatically clear an edition or recording.

## Design conclusions and their limits

These are implementation recommendations drawn from the visual comparisons, not claims about composer intent in an arbitrary upload:

1. **Choose the edition before changing its contents.** Faithful transcription preserves detected events. Singer accompaniment preserves instrumental material and optionally shows a separately identified vocal cue. Solo piano explicitly arranges an actual selected melody with accompaniment. Neither arrangement should be labelled an unmodified detection result.
2. **Keep melody, accompaniment, physical hand, and notation staff separate.** A recurring accompaniment figure may be the identity of the song. Do not replace it with generic block chords merely to lower note density. A high instrumental note is not proof of a vocal melody.
3. **Set the musical coordinate system first.** Tempo, meter, key, pickup and first downbeat materially affect notation. Proposed estimates require visible provenance and correction. A flat-heavy passage under an assumed C signature needs a key review before cosmetic accidental cleanup. An estimated key does not resolve every enharmonic spelling.
4. **Normalize melodic releases conservatively.** In an explicitly identified melody, a very short unmarked gap can be an editorial duration correction. Leave meaningful rests, repeated attacks, overlaps, breath/phrase boundaries, locks, pedal distinctions and accompaniment articulations intact. Do not turn the Schubert-style offbeat accompaniment into sustained chords.
5. **Group according to meter.** Compound-meter eighths should expose dotted-quarter beats; straight and triplet figures can occur in different voices. Rational durations, barline ties, explicit voices and valid rests remain the score compiler's responsibility. A single global grid cannot model all expressive or mixed rhythmic material.
6. **Do not infer expression from a style template.** The examples' lyrics, slurs, repeats, dynamics, staccato, fermatas and postludes belong to their authored editions. Do not insert those features into an uploaded song and present them as detected. Current work does not infer phrase-level layout breaks, lyrics, form, or expressive markings.
7. **Validate exports and listen with a musician.** Canonical XML/MIDI equality establishes event consistency, not recognizability, natural phrasing, or pianist approval. Published-sheet visual comparisons and synthetic test passages are insufficient to establish accuracy on finished songs. A permission-cleared, exact-edition human-performance corpus and musician review remain necessary for that claim.

## Implemented conservative duration pass

`backend/app/edition_readability.py` provides `prepare_edition_notes(notes, score_settings, mode=...)`. It is enabled only on a derived accompaniment or solo-piano edition; faithful mode returns unchanged note data. It never invents a role and only considers existing `melody` or `vocal_cue` lines grouped by source part, voice, and role.

The bounds are explicit: grid correction at most 0.1 quarter-note beat and one quarter of the chosen melodic grid unit; release-gap extension at most 0.22 beat; resulting melodic duration at least 0.5 beat. The pass uses the compiler's `note_grid`, including an explicitly chosen sixteenth-note melody grid independent of the accompaniment grid. Overlapping lines and marked phrase/articulation/pedal distinctions are excluded. These thresholds are engineering choices, not learned perceptual thresholds. No general accompaniment chord-release union is introduced by this pass.

It copies the input, preserves stable IDs/pitches and existing `original_timing`, records per-field editorial changes, and retains physical-release validity. Unlocked algorithmic edits remain unlocked. The final arrangement must be reassessed for physical feasibility after this pass because extending a melody can extend a held key. The change log identifies this operation separately from transcription and hand assignment.

Focused regression command:

```sh
.venv/bin/python -m pytest backend/tests/test_edition_readability.py -q
```

The 25 initial cases cover immutable faithful mode, explicit disablement, lock defaults, articulations/breaths, accompaniment preservation, repeated attacks, source/voice/phrase separation, overlaps, short notes, manual physical release, triplet pickup alignment, separate melodic quantization, canonical bar durations, and parsed MusicXML/MIDI event agreement. They do not establish perceptual quality or singer/pianist playability.

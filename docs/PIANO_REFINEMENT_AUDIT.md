# Independent piano-refinement checks

Use `scripts/audit_piano_density.py` to compare a saved edition and its refinement without relying on the arranger's own objective or summary. It reads project/note snapshots and canonical score settings; it does not change projects, infer missing notes, or access audio.

```sh
python -m scripts.audit_piano_density \
  --before output/private/before-project.json \
  --after output/private/after-project.json \
  --before-score output/private/before-score.json \
  --after-score output/private/after-score.json \
  --duration 153.08 \
  --output output/private/density-comparison.json
python -m pytest scripts/test_piano_density.py -q
```

Use the actual clip duration and private snapshot paths. Before a candidate has a saved score, omit `--after-score`; the candidate is projected onto the prior score's grid, and its notation statistics are explicitly unavailable.

The audit reports two timing bases: exact saved performance seconds and the selected rational score grid. It independently applies half-up quantization, the separate melodic grid, score transposition, and an explicitly confirmed pedal key-release time. Physical key intervals are end-exclusive: a release at an attack instant occurs first. Duplicate simultaneous events on the same key count as one physical key; vocal cue notes are excluded from piano hand load. No hand assignment or pedal lift is invented.

For each hand, it measures seconds spent at each key count, new-key attack sizes, held-key counts immediately after each attack, four/five-or-more-key durations and attack counts, maximum register span, and dense time intervals. Report both duration and attack distributions: a theoretical five-key maximum does not show whether that load occurs constantly or only once. Conversely, a three-key voicing can still be unplayable if its span is excessive. A regression fixture covers the case where floating-point half-grid rounding previously hid a held-note overlap that the rational score compiler retained.

Exact stable-ID comparisons expose omissions, additions, octave/chromatic changes, onset/release/velocity edits, changed role labels, hand changes, and changes to original timing. Lead, cue, bass, riff, and countermelody retention is reported separately, distinguishing explicit markings from inferred roles. Locked/manual events receive their own report. If no riffs are marked, the audit cannot establish preservation of every characteristic motif. Saved raw onset sets and canonical attack sets are separate: removing a jittered doubling can remove a distinct raw timestamp while retaining the same written attack.

With an actual saved canonical score, the audit also unions its rational note segments by physical hand and pitch, including ties and simultaneous duplicate detections. It compares those intervals exactly with the independently projected saved events, so a compiler hand or duration mismatch cannot pass solely because the arranger's summary looks correct. Notation counts describe sounding voices per staff/measure and dense measures; they do not certify visual layout. Validate MusicXML/MIDI equality and inspect the exact revisioned PDF separately. The same measurement code applies whether the source was an acoustic recording or an AI-generated song; it does not classify that origin.

These checks establish consistency with the saved detected events, not accuracy against the performance. Density reduction is an explicit arrangement change. Repeated motifs may be intentional; fewer inner notes are not evidence of a more natural performance, and random variation should not be presented as detected music. Audio listening and pianist review must be reported independently. Sampled review timbre is documented in `docs/PIANO_PLAYBACK.md` and does not change score content.

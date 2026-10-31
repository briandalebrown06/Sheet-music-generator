"""Generated note fixtures; these test notation, not pianist validation."""
from copy import deepcopy
from fractions import Fraction
import xml.etree.ElementTree as ET

import pytest
music21 = pytest.importorskip("music21")

from backend.app.readability import prepare_readable_notes
from backend.app.score import build_score, fraction, score_to_musicxml, write_score_midi


def event(identifier, start, end, pitch=60, **extra):
    return {"id": identifier, "start": start, "end": end, "pitch": pitch,
            "velocity": 80, "part_id": "part-1", "hand": "right", "provenance": "detected",
            "original_timing": {"start": start, "end": end, "pitch": pitch}, **extra}


def settings(**extra):
    return {"tempo_bpm": 120, "quantization": "sixteenth", "piano_split": True,
            "readability_mode": "accompaniment", **extra}


def model(notes, **extra):
    return build_score(notes, settings(**extra), title="DEVELOPMENT readability fixture")


def events(score):
    return [e for p in score["parts"] for s in p["staves"] for m in s["measures"]
            for v in m["voices"] for e in v["events"]]


def assert_bars(score):
    for part in score["parts"]:
        for staff in part["staves"]:
            for measure in staff["measures"]:
                for voice in measure["voices"]:
                    assert sum((fraction(e["duration"]) for e in voice["events"]), Fraction(0)) == fraction(measure["duration"])


def test_explicit_readability_removes_jitter_voices_without_changing_original():
    notes = [event("c", .001, .687, 60, voice_id="1"),
             event("e", .004, .690, 64, voice_id="2")]
    original = deepcopy(notes)
    before = model(notes)
    result = prepare_readable_notes(notes, settings())
    after = model(result["notes"])
    assert notes == original
    assert [n["original_timing"] for n in result["notes"]] == [n["original_timing"] for n in original]
    assert len(before["parts"][0]["staves"][0]["measures"][0]["voices"]) == 2
    assert len(after["parts"][0]["staves"][0]["measures"][0]["voices"]) == 1
    assert all(e["pitches"] == [60, 64] for e in events(after) if e["kind"] != "rest")
    assert result["changes"] and all(c["pass"] == "notation_readability" for c in result["changes"])
    assert_bars(after)


def test_locked_edits_and_intentional_syncopated_repeated_attacks_survive():
    notes = [event("lock", .017, .687, 60, provenance="edited", voice_id="3"),
             event("offbeat-1", .125, .375, 67), event("offbeat-2", .625, .875, 67)]
    result = prepare_readable_notes(notes, settings())
    assert result["notes"] == notes
    assert [n["start"] for n in result["notes"] if n["pitch"] == 67] == [.125, .625]
    assert len(result["notes"]) == 3


def test_hands_and_staves_are_independent_and_mixed_sources_form_one_piano():
    notes = [event("low-right", 0, 1, 55, hand="right", part_id="strings"),
             event("high-left", 0, 1, 64, hand="left", part_id="guitar")]
    score = model(notes, staff_overrides={"low-right": "bass"})
    assert len(score["parts"]) == 1 and score["parts"][0]["id"] == "piano"
    bass = score["parts"][0]["staves"][1]
    sounding = [e for m in bass["measures"] for v in m["voices"] for e in v["events"] if e["kind"] != "rest"]
    assert sounding[0]["hands"] == {"low-right": "right", "high-left": "left"}
    assert sounding[0]["source_parts"] == {"low-right": "strings", "high-left": "guitar"}
    assert_bars(score)


def test_stable_stems_and_silent_secondary_voice_rests_hidden_not_deleted():
    notes = [event("held", 0, 3, 60), event("upper", .5, 1, 72), event("next", 2, 2.5, 74)]
    score = model(notes)
    staff = score["parts"][0]["staves"][0]
    upper = [e for m in staff["measures"] for v in m["voices"] for e in v["events"] if 72 in e["pitches"]]
    lower = [e for m in staff["measures"] for v in m["voices"] for e in v["events"] if 60 in e["pitches"]]
    assert all(e["stem_direction"] == "up" for e in upper)
    assert all(e["stem_direction"] == "down" for e in lower)
    assert any(e.get("hidden") for e in events(score) if e["kind"] == "rest")
    xml = score_to_musicxml(score)
    assert '<stem>up</stem>' in xml and '<stem>down</stem>' in xml
    assert 'print-object="no"' in xml
    assert_bars(score)
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert all(m.highestTime == 4 for p in parsed.parts for m in p.getElementsByClass(music21.stream.Measure))


def test_pedal_is_explicit_physical_release_xml_and_midi_agree(tmp_path):
    import pretty_midi
    source = [event("pedaled", 0, 3, 60, key_release_seconds=.5, pedal_sustained=True),
              event("later", .75, 1, 64)]
    score = model(source)
    pitched = [e for e in events(score) if e["kind"] != "rest"]
    assert fraction(next(e for e in pitched if 60 in e["pitches"])["duration"]) == 1
    assert score["pedal_spans"] == [{"part_id": "piano", "start": {"numerator": 0, "denominator": 1},
                                     "end": {"numerator": 6, "denominator": 1}}]
    assert len(score["parts"][0]["staves"][0]["measures"]) == 2
    xml = score_to_musicxml(score)
    tree = ET.fromstring(xml)
    assert [p.attrib["type"] for p in tree.findall(".//pedal")] == ["start", "stop"]
    path = tmp_path / "pedal.mid"
    write_score_midi(score, path)
    midi = pretty_midi.PrettyMIDI(str(path))
    assert [(c.number, c.value, c.time) for c in midi.instruments[0].control_changes] == [(64, 127, 0), (64, 0, 3)]
    assert midi.instruments[0].notes[0].end == pytest.approx(.5)
    assert score["source_notes"] == source
    assert_bars(score)


def test_cue_staff_and_manual_fingers_only(tmp_path):
    import pretty_midi
    notes = [event("piano", 0, 1, 60, fingering=1), event("chord", 0, 1, 64, fingering=3),
             event("cue", 0, 1, 76, role="vocal_cue", hand=None)]
    score = model(notes)
    cue = next(p for p in score["parts"] if p["is_cue"])
    assert len(cue["staves"]) == 1
    xml = ET.fromstring(score_to_musicxml(score))
    assert sorted(f.text for f in xml.findall(".//fingering")) == ["1", "3"]
    path = tmp_path / "cue.mid"
    write_score_midi(score, path)
    midi = pretty_midi.PrettyMIDI(str(path))
    assert {n.pitch for i in midi.instruments for n in i.notes} == {60, 64}
    unmarked = model([event("plain", 0, 1)])
    assert "<fingering" not in score_to_musicxml(unmarked)


def test_readable_pickups_triplets_and_exact_transposition():
    notes = [event("a", 0, 1/6, 60), event("b", 1/6, 1/3, 64), event("c", 1/3, .5, 67),
             event("held", .5, 3, 48, hand="left")]
    score = model(notes, quantization="eighth_triplet", pickup_beats="1", first_downbeat_seconds=.5,
                  transpose_semitones=3, key_signature=-3)
    assert_bars(score)
    xml = score_to_musicxml(score)
    assert '<actual-notes>3</actual-notes>' in xml and '<tie type="start"' in xml
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert {int(p.midi) for n in parsed.recurse().notes for p in n.pitches} == {63, 67, 70, 51}
    assert all(p.measure(0).highestTime == 1 for p in parsed.parts)
    assert len({n["id"] for n in score["source_notes"]}) == 4


def test_partial_chord_fingering_is_attached_to_the_requested_pitch_only():
    score = model([event("c", 0, 1, 60), event("e", 0, 1, 64, fingering=3)])
    xml = ET.fromstring(score_to_musicxml(score))
    marked = [node for node in xml.findall('.//note') if node.find('.//fingering') is not None]
    assert len(marked) == 1
    assert marked[0].findtext('pitch/step') == 'E'
    assert marked[0].findtext('.//fingering') == '3'


def test_ledger_diagnostics_rerun_after_exact_transposition_without_revoicing():
    notes = [event("high-locked", 0, 1, 96, locked=True)]
    high = model(notes)
    lower = model(notes, transpose_semitones=-12)
    issue = next(i for i in high['readability']['issues'] if i['code'] == 'ledger_lines')
    assert issue['measure'] == 1 and issue['hand'] == ['right'] and issue['note_ids'] == ['high-locked']
    assert high['readability']['statistics']['maximum_ledger_lines'] == 5
    assert lower['readability']['statistics']['maximum_ledger_lines'] == 2
    assert {p for e in events(lower) for p in e['pitches']} == {84}
    assert lower['source_notes'] == notes


def test_null_lock_respects_existing_edited_note_by_default():
    notes = [event('edited', .017, .687, locked=None, provenance='edited', voice_id='2')]
    assert prepare_readable_notes(notes, settings())['notes'] == notes


def test_vocal_cue_register_clef_and_manual_override_preserve_pitch_and_midi(tmp_path):
    import pretty_midi
    notes = [event("piano", 0, 1, 60), event("low-cue", 0, .5, 46, role="vocal_cue", hand=None),
             event("next-cue", .5, 1, 51, role="vocal_cue", hand=None)]
    auto = model(notes)
    manual = model(notes, vocal_clef="treble")
    assert next(p for p in auto["parts"] if p["is_cue"])["staves"][0]["clef"] == "bass"
    assert next(p for p in manual["parts"] if p["is_cue"])["staves"][0]["clef"] == "treble"
    for name, score in (("auto", auto), ("manual", manual)):
        xml = ET.fromstring(score_to_musicxml(score))
        cue = xml.find("part")
        assert cue.findtext("measure/attributes/clef/sign") == ("F" if name == "auto" else "G")
        parsed = music21.converter.parseData(score_to_musicxml(score), format="musicxml")
        assert {int(p.midi) for n in parsed.recurse().notes for p in n.pitches} == {46, 51, 60}
        path = tmp_path / (name + ".mid")
        write_score_midi(score, path)
        assert [(n.pitch, n.start, n.end) for i in pretty_midi.PrettyMIDI(str(path)).instruments for n in i.notes] == [(60, 0, 1)]
        assert score["source_notes"] == notes


def test_hand_directions_consolidate_voices_repeats_and_explicit_named_pitch_returns(tmp_path):
    import pretty_midi
    notes = [event("bass", 0, .5, 39, hand="left", voice_id="3"),
             event("right-a", 0, .25, 51, voice_id="1"),
             event("right-other", 0, .5, 46, voice_id="2"),
             event("right-repeat", .5, .75, 51, voice_id="1"),
             event("left-return", 1, 1.25, 51, hand="left", voice_id="1"),
             event("right-return", 1.5, 1.75, 51, voice_id="1")]
    score = model(notes, staff_overrides={n["id"]: "bass" for n in notes})
    bass = score["parts"][0]["staves"][1]
    directions = bass["measures"][0]["hand_directions"]
    assert [(fraction(d["offset"]), d["hands"]) for d in directions] == [
        (0, {"right": [46, 51]}), (2, {"left": [51]}), (3, {"right": [51]})]
    words = [n.text for n in ET.fromstring(score_to_musicxml(score)).findall(".//words")]
    assert words.count("R.H.: Bb2, Eb3") == 1
    assert "L.H.: Eb3" in words
    assert any("Hand labels apply" in w for w in words)
    bare = deepcopy(score)
    for part in bare["parts"]:
        for staff in part["staves"]:
            for measure in staff["measures"]:
                measure.pop("hand_directions", None)
    for name, value in (("annotated", score), ("bare", bare)):
        write_score_midi(value, tmp_path / (name + ".mid"))
    assert (tmp_path / "annotated.mid").read_bytes() == (tmp_path / "bare.mid").read_bytes()
    assert score["source_notes"] == notes
    assert_bars(score)


def test_contiguous_single_hand_passage_has_one_mark_and_explicit_return():
    notes = [event("r1", 0, .25, 48), event("r2", .5, .75, 50),
             event("l", 1, 1.25, 40, hand="left")]
    score = model(notes, staff_overrides={n["id"]: "bass" for n in notes})
    directions = score["parts"][0]["staves"][1]["measures"][0]["hand_directions"]
    assert [(fraction(d["offset"]), d["hands"], d["named_pitches"]) for d in directions] == [
        (0, {"right": []}, False), (2, {"left": []}, False)]


def test_cross_hand_tie_restates_assignment_after_barline():
    score = model([event("held-right", 0, 3, 48)], staff_overrides={"held-right": "bass"})
    measures = score["parts"][0]["staves"][1]["measures"]
    assert all(m["hand_directions"] == [{"offset": {"numerator": 0, "denominator": 1},
                                        "hands": {"right": []}, "named_pitches": False}] for m in measures)
    assert_bars(score)


def test_jitter_and_chord_release_alignment_preserve_physical_release_bounds():
    notes = [event('short-press', .11, .5, key_release_seconds=.12, pedal_sustained=True),
             event('release-near-end', 1, 1.01, key_release_seconds=1.005, pedal_sustained=True),
             event('chord-pedal', 2, 2.69, key_release_seconds=2.688, pedal_sustained=True),
             event('chord-peer', 2, 2.68, 64)]
    result = prepare_readable_notes(notes, settings())
    for note in result['notes']:
        assert note['start'] < note['end']
        if note.get('key_release_seconds') is not None:
            assert note['start'] < note['key_release_seconds'] <= note['end']
    assert result['notes'][0]['start'] == .11  # Cannot snap past the held release.
    assert result['notes'][1]['end'] == 1.01  # Cannot shorten sound below key release.
    assert result['notes'][2]['end'] == 2.69  # Chord alignment cannot cross key release.


def test_overlapping_unisons_keep_independent_midi_releases(tmp_path):
    import pretty_midi
    # The short unison's note-off must not terminate the held voice on the same
    # piano key. This also crosses a barline and exercises tied-note assembly.
    score = model([event('held-unison', 0, 3, 60, voice_id='2'),
                   event('short-unison', 0, .5, 60, voice_id='1'),
                   event('reattack', 1, 1.5, 60, voice_id='1'),
                   event('other-key', 0, .5, 64, voice_id='1')])
    path = tmp_path / 'overlapping-unisons.mid'
    write_score_midi(score, path)
    parsed = pretty_midi.PrettyMIDI(str(path))
    assert len(parsed.instruments) == 2
    actual = sorted((n.pitch, round(n.start, 5), round(n.end, 5)) for i in parsed.instruments for n in i.notes)
    assert actual == [(60, 0, .5), (60, 0, 3), (60, 1, 1.5), (64, 0, .5)]
    xml = ET.fromstring(score_to_musicxml(score))
    assert len(xml.findall('.//metronome')) == 1
    assert xml.findtext('part-list/score-part/part-name') == 'Piano'


def test_edited_performance_midi_preserves_unquantized_overlapping_unisons(tmp_path):
    import pretty_midi
    from backend.app.performance import write_performance_midi
    notes = [event('held', .1234, 3.17653, 60), event('short', .62345, 1.12347, 60),
             event('other', .333, 1.334, 64), event('separate-part', .1234, 2.32, 60, part_id='part-2')]
    path = tmp_path / 'edited-performance.mid'
    write_performance_midi(notes, path)
    parsed = pretty_midi.PrettyMIDI(str(path))
    actual = sorted((n.pitch, n.start, n.end) for i in parsed.instruments for n in i.notes)
    expected = sorted((n['pitch'], n['start'], n['end']) for n in notes)
    assert len(parsed.instruments) == 3 and len(actual) == 4
    for got, wanted in zip(actual, expected):
        assert got[0] == wanted[0]
        assert got[1:] == pytest.approx(wanted[1:], abs=.000051)


def test_midi_channel_budget_fails_instead_of_wrapping_or_dropping_notes(tmp_path):
    from backend.app.performance import write_performance_midi
    # Separate physical voices need independent note-offs, irrespective of their
    # track labels; both public MIDI writers honor the actual 15 pitched channels.
    notes = [event(f'overlap-{i}', i * .1, 3 + i * .1, 60, voice_id=str(i+1)) for i in range(16)]
    path = tmp_path / 'too-many-channels.mid'
    with pytest.raises(ValueError, match='more than 15 independent MIDI channels'):
        write_performance_midi(notes, path)
    with pytest.raises(ValueError, match='more than 15 independent MIDI channels'):
        write_score_midi(model(notes), path)
    assert not path.exists()


def test_cross_staff_hand_override_prints_explicit_hand_and_pitch_label():
    score = model([event('rh-on-bass', 0, 1, 55, hand='right'),
                   event('lh-on-bass', 0, 1, 48, hand='left')],
                  staff_overrides={'rh-on-bass': 'bass'})
    xml = ET.fromstring(score_to_musicxml(score))
    assert 'R.H.: G3' in [node.text for node in xml.findall('.//words')]
    issue = next(i for i in score['readability']['issues'] if i['code'] == 'mixed_hands_on_staff')
    assert issue['measure'] == 1 and issue['hand'] == ['left', 'right']
    assert set(issue['note_ids']) == {'rh-on-bass', 'lh-on-bass'}


def test_automatic_voice_rests_only_show_real_staff_silence_but_manual_voices_remain():
    notes = [event('upper', 0, 2, 72), event('lower', 1, 3, 60)]
    automatic = model(notes, tempo_bpm=60)
    staff = automatic['parts'][0]['staves'][0]
    measure = staff['measures'][0]
    visible = [e for v in measure['voices'] for e in v['events'] if e['kind'] == 'rest' and not e.get('hidden')]
    assert [(fraction(e['offset']), fraction(e['duration'])) for e in visible] == [(3, 1)]
    manual = model([{**n, 'voice_id': str(i+1), 'locked': True} for i, n in enumerate(notes)], tempo_bpm=60)
    manual_rests = [e for v in manual['parts'][0]['staves'][0]['measures'][0]['voices'] for e in v['events'] if e['kind'] == 'rest']
    assert manual_rests and all(not e.get('hidden') for e in manual_rests)
    assert any(fraction(e['offset']) < 3 for e in manual_rests)
    assert_bars(automatic)
    assert_bars(manual)


def test_auto_extreme_staff_placement_preserves_hand_pitch_timing_and_manual_override(tmp_path):
    import pretty_midi
    from backend.app.piano import assess_playability
    notes = [event('low-rh', 0, 1, 34, hand='right')]
    original = deepcopy(notes)
    automatic = model(notes)
    manual = model(notes, staff_overrides={'low-rh': 'treble'})
    assert notes == original
    assert automatic['source_notes'] == manual['source_notes'] == notes
    assert automatic['staff_placement_changes'][0]['to_staff'] == 'bass'
    assert automatic['staff_placement_changes'][0]['hand'] == 'right'
    assert not manual['staff_placement_changes']
    sounding = [e for e in events(automatic) if e['kind'] != 'rest']
    assert sounding[0]['hands'] == {'low-rh': 'right'} and sounding[0]['pitches'] == [34]
    assert automatic['readability']['statistics']['maximum_ledger_lines'] < manual['readability']['statistics']['maximum_ledger_lines']
    assert 'R.H.' in [n.text for n in ET.fromstring(score_to_musicxml(automatic)).findall('.//words')]
    for name, score in [('auto', automatic), ('manual', manual)]:
        write_score_midi(score, tmp_path / f'{name}.mid')
    midi_notes = lambda name: [(n.pitch, n.start, n.end) for i in pretty_midi.PrettyMIDI(str(tmp_path / f'{name}.mid')).instruments for n in i.notes]
    assert midi_notes('auto') == midi_notes('manual') == [(34, 0, 1)]
    assert assess_playability(notes, {}, automatic['settings']) == assess_playability(notes, {}, manual['settings'])
    assert_bars(automatic)


def test_visible_automatic_rests_regroup_on_metric_boundaries():
    score = model([event('late', 3, 4, 72)], tempo_bpm=60)
    rests = [e for v in score['parts'][0]['staves'][0]['measures'][0]['voices'] for e in v['events'] if e['kind'] == 'rest' and not e.get('hidden')]
    assert [(fraction(e['offset']), fraction(e['duration'])) for e in rests] == [(0, 2), (2, 1)]
    assert_bars(score)


def test_readability_edits_record_provenance_without_autolocking_or_rewriting_raw_time():
    notes = [event('jitter', .009, .509, 60, hand='right')]
    result = prepare_readable_notes(notes, settings())
    revised = result['notes'][0]
    assert revised['start'] == 0 and revised['end'] == .5
    assert revised['provenance'] == 'edited' and revised['locked'] is False
    assert revised['original_timing'] == notes[0]['original_timing']
    assert notes[0]['provenance'] == 'detected' and 'locked' not in notes[0]
    assert len(result['changes']) == 2


def test_finished_overlap_lanes_compact_without_changing_sounding_events(tmp_path):
    import pretty_midi
    from scripts.evaluate_piano import inspect_exports

    # Three genuinely simultaneous durations need three voices initially.
    # The later isolated line must not hop among them merely because its
    # register resembles an earlier group. These are generated score fixtures.
    notes = [event('low', 0, 1, 60), event('middle', 0, 2, 72),
             event('high', 0, 3, 84), event('later-low', 4, 4.5, 60),
             event('later-middle', 4.5, 5, 72), event('later-high', 5, 6, 84)]
    source = deepcopy(notes)
    score = model(notes, tempo_bpm=60)
    measures = score['parts'][0]['staves'][0]['measures']
    active = lambda m: [v for v in m['voices'] if any(e['kind'] != 'rest' for e in v['events'])]
    assert len(active(measures[0])) == 3
    assert len(active(measures[1])) == 1
    assert notes == source == score['source_notes']
    assert {n for e in events(score) for n in e['source_note_ids']} == {n['id'] for n in notes}
    assert_bars(score)
    xml = score_to_musicxml(score)
    path = tmp_path / 'compacted.mid'
    write_score_midi(score, path)
    assert inspect_exports(score, xml, path)['note_events'] == len(notes)
    parsed = pretty_midi.PrettyMIDI(str(path))
    actual = sorted((n.pitch, n.start, n.end) for i in parsed.instruments for n in i.notes)
    assert actual == sorted((n['pitch'], n['start'], n['end']) for n in notes)


def test_automatic_compaction_keeps_sustained_voice_across_barlines():
    notes = [event('held-line', 0, 9, 60), event('upper-a', 1, 2, 76),
             event('upper-b', 4, 5, 77), event('upper-c', 8, 8.5, 79)]
    score = model(notes, tempo_bpm=60)
    staff = score['parts'][0]['staves'][0]
    held = [(v['id'], e) for m in staff['measures'] for v in m['voices']
            for e in v['events'] if 'held-line' in e['source_note_ids']]
    assert len({voice for voice, _ in held}) == 1
    assert [e['tie'] for _, e in held] == ['start', 'continue', 'stop']
    assert sum(fraction(e['duration']) for _, e in held) == 9
    assert len({e['stem_direction'] for _, e in held}) == 1
    assert_bars(score)


def test_automatic_lane_reuse_never_takes_a_manual_voice():
    notes = [event('manual', 0, 6, 64, voice_id='1', locked=True),
             event('a', 0, 1, 72), event('b', 0, 2, 76),
             event('later-a', 4, 4.5, 72), event('later-b', 4.5, 5, 76)]
    score = model(notes, tempo_bpm=60)
    staff = score['parts'][0]['staves'][0]
    for measure in staff['measures']:
        manual = next(v for v in measure['voices'] if v['id'] == '1')
        assert {nid for e in manual['events'] for nid in e['source_note_ids']} == {'manual'}
        assert all(not e.get('hidden') for e in manual['events'] if e['kind'] == 'rest')
    assert score['source_notes'] == notes
    assert_bars(score)


def test_crossing_equal_direction_beams_use_flags_without_changing_notes(tmp_path):
    from backend.app.score import score_to_stream
    from scripts.evaluate_piano import inspect_exports

    notes = [event('upper-reserved', 1.5, 2, 72, voice_id='1'),
             event('a-high', 0, .5, 63, voice_id='2'),
             event('a-low', .5, 1, 58, voice_id='2'),
             event('b-low', 0, .5, 58, voice_id='3'),
             event('b-high', .5, 1, 63, voice_id='3')]
    score = model(notes, tempo_bpm=60, quantization='eighth')
    before = deepcopy(score)
    rendered = score_to_stream(score)
    measure = rendered.parts[0].measure(1)
    voices = {str(v.id): v for v in measure.voices}
    assert [b.type for n in voices['2'].notes for b in n.beams] == ['start', 'stop']
    assert not [b for n in voices['3'].notes for b in n.beams]
    assert all(n.stemDirection == 'down' for v in ('2', '3') for n in voices[v].notes)
    assert score == before  # Engraving does not rewrite the canonical score.
    path = tmp_path / 'crossing-beams.mid'
    write_score_midi(score, path)
    assert inspect_exports(score, score_to_musicxml(score), path)['note_events'] == len(notes)
    assert_bars(score)


def test_opposite_direction_beams_remain_beamed():
    from backend.app.score import score_to_stream

    score = model([event('a-high', 0, .5, 63, voice_id='1'),
                   event('a-low', .5, 1, 58, voice_id='1'),
                   event('b-low', 0, .5, 58, voice_id='2'),
                   event('b-high', .5, 1, 63, voice_id='2')],
                  tempo_bpm=60, quantization='eighth')
    measure = score_to_stream(score).parts[0].measure(1)
    assert all([b.type for n in v.notes for b in n.beams] == ['start', 'stop'] for v in measure.voices)


def test_same_direction_chord_beams_do_not_use_average_pitch_as_geometry():
    from backend.app.score import score_to_stream

    # These averages never cross (58->61.5 versus 59->63), but chord extent
    # and beam-slope choices can still put the printed beams on top of each
    # other. The conservative policy needs no fabricated page coordinates.
    score = model([event('reserved', 1.5, 2, 72, voice_id='1'),
                   event('a', 0, .5, 58, voice_id='2'),
                   event('chord-low', .5, 1, 58, voice_id='2'),
                   event('chord-high', .5, 1, 65, voice_id='2'),
                   event('b', 0, .5, 59, voice_id='3'),
                   event('b-next', .5, 1, 63, voice_id='3')],
                  tempo_bpm=60, quantization='eighth')
    voices = {str(v.id): v for v in score_to_stream(score).parts[0].measure(1).voices}
    assert [b.type for n in voices['2'].notes for b in n.beams] == ['start', 'stop']
    assert not [b for n in voices['3'].notes for b in n.beams]
    assert_bars(score)

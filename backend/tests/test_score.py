"""Explicit generated event fixtures verify notation, never stand in for inference."""
from copy import deepcopy
from fractions import Fraction
import random
import xml.etree.ElementTree as ET

import pytest

music21 = pytest.importorskip("music21")
from backend.app.score import (ScoreSettings, build_score, default_score_settings,
                               estimate_tempo, fraction, score_to_musicxml, write_score_midi)


def event(identifier, start, end, pitch=60):
    return {"id": identifier, "start": start, "end": end, "pitch": pitch, "velocity": 80,
            "part_id": "part-1", "provenance": "detected", "model_score": .5,
            "original_timing": {"start": start, "end": end, "pitch": pitch, "velocity": 80}}


def score(events, **settings):
    return build_score(events, {"tempo_bpm": 60, **settings}, title="GENERATED NOTATION TEST FIXTURE")


def measures(model, staff=0):
    return model["parts"][0]["staves"][staff]["measures"]


def notes(model):
    return [e for staff in model["parts"][0]["staves"] for m in staff["measures"]
            for voice in m["voices"] for e in voice["events"] if e["kind"] != "rest"]


def assert_consistent(model):
    known = {n["id"] for n in model["source_notes"]}
    seen = set()
    for staff in model["parts"][0]["staves"]:
        previous_end = Fraction(0)
        for measure in staff["measures"]:
            assert fraction(measure["start"]) == previous_end
            expected = fraction(measure["duration"])
            previous_end += expected
            if not measure["is_pickup"]:
                assert expected == fraction(model["bar_duration"])
            for voice in measure["voices"]:
                cursor = Fraction(0)
                for item in voice["events"]:
                    assert fraction(item["offset"]) == cursor
                    duration = fraction(item["duration"])
                    assert duration > 0
                    cursor += duration
                    assert set(item["source_note_ids"]) <= known
                    seen.update(item["source_note_ids"])
                assert cursor == expected
    assert seen == known


def test_barline_notes_have_explicit_ties_and_roundtrip():
    model = score([event("sustain", 3, 9)])
    assert [n["tie"] for n in notes(model)] == ["start", "continue", "stop"]
    assert [fraction(n["duration"]) for n in notes(model)] == [1, 4, 1]
    assert_consistent(model)
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    actual = list(parsed.recurse().notes)
    assert [n.tie.type for n in actual] == ["start", "continue", "stop"]
    assert sum(Fraction(n.quarterLength) for n in actual) == 6


def test_pickup_has_short_measure_followed_by_full_bars():
    model = score([event("pickup", 0, 1), event("first", 1, 3)],
                  first_downbeat_seconds=1, pickup_beats="1")
    assert [(m["number"], fraction(m["duration"])) for m in measures(model)] == [(0, 1), (1, 4)]
    xml = score_to_musicxml(model)
    assert '<measure implicit="yes" number="0">' in xml
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert parsed.parts[0].measure(0).paddingLeft == 3
    assert parsed.parts[0].measure(0).highestTime == 1
    assert parsed.parts[0].measure(1).highestTime == 4
    assert_consistent(model)


def test_pickup_barline_tie_and_empty_input_are_valid():
    model = score([event("cross", 0, 3)], first_downbeat_seconds=1, pickup_beats="1")
    assert [n["tie"] for n in notes(model)] == ["start", "stop"]
    empty = score([])
    assert notes(empty) == []
    assert measures(empty)[0]["voices"][0]["events"][0]["kind"] == "rest"
    assert_consistent(empty)
    music21.converter.parseData(score_to_musicxml(empty), format="musicxml")


def test_rests_fill_gaps_without_subgrid_artifacts():
    model = score([event("a", .02, .96), event("b", 1.03, 1.96), event("c", 3.02, 3.6)])
    all_events = measures(model)[0]["voices"][0]["events"]
    assert all(fraction(e["duration"]) >= Fraction(1, 2) for e in all_events)
    assert any(e["kind"] == "rest" and fraction(e["offset"]) == 2 for e in all_events)
    assert not any(e["kind"] == "rest" and fraction(e["offset"]) == 1 for e in all_events)
    assert_consistent(model)


def test_synchronous_notes_become_chord_not_three_voices():
    model = score([event("c", 0, 2, 60), event("e", .02, 1.98, 64), event("g", .01, 2, 67)])
    assert len(measures(model)[0]["voices"]) == 1
    assert notes(model)[0]["kind"] == "chord"
    assert notes(model)[0]["pitches"] == [60, 64, 67]
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    assert isinstance(list(parsed.recurse().notes)[0], music21.chord.Chord)
    assert_consistent(model)


def test_triplets_keep_exact_fractions_and_musicxml_tuplets():
    model = score([event("a", 0, 1/3), event("b", 1/3, 2/3, 62), event("c", 2/3, 1, 64)],
                  quantization="eighth_triplet")
    assert [fraction(n["duration"]) for n in notes(model)] == [Fraction(1, 3)] * 3
    xml = score_to_musicxml(model)
    assert "<actual-notes>3</actual-notes>" in xml
    assert '<tuplet ' in xml
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert [n.quarterLength for n in parsed.recurse().notes] == [Fraction(1, 3)] * 3
    assert_consistent(model)


def test_overlapping_lines_are_explicit_voices_with_valid_bars():
    model = score([event("low", 0, 5, 48), event("high-a", .5, 1.5, 72), event("high-b", 2, 4.5, 74)])
    assert len(measures(model)[0]["voices"]) == 2
    xml = score_to_musicxml(model)
    assert "<voice>1</voice>" in xml and "<voice>2</voice>" in xml
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert len(parsed.parts[0].measure(1).voices) == 2
    assert_consistent(model)


def test_piano_heuristic_is_correctable_and_exported_as_one_part_two_staves():
    events = [event("low", 0, 2, 48), event("high", 0, 2, 72), event("middle", 2, 4, 60)]
    model = score(events, piano_split=True, staff_overrides={"middle": "bass"})
    assert len(model["parts"]) == 1
    assert [s["clef"] for s in model["parts"][0]["staves"]] == ["treble", "bass"]
    bass_ids = {identifier for m in measures(model, 1) for v in m["voices"]
                for n in v["events"] for identifier in n["source_note_ids"]}
    assert bass_ids == {"low", "middle"}
    xml = ET.fromstring(score_to_musicxml(model))
    assert len(xml.findall("part")) == 1
    assert xml.find("part/measure/attributes/staves").text == "2"
    assert any("not identification" in w for w in model["warnings"])
    assert_consistent(model)


def test_requantization_transposition_and_clef_preserve_original_events():
    source = [event("raw", .21, .71, 48)]
    before = deepcopy(source)
    fine = score(source, quantization="sixteenth", transpose_semitones=12, clef="alto", key_signature=-2)
    coarse = score(source, quantization="quarter")
    assert source == before
    assert fine["source_notes"] == before
    assert notes(fine)[0]["pitches"] == [60]
    assert fine["parts"][0]["staves"][0]["clef"] == "alto"
    assert notes(fine)[0]["offset"] != notes(coarse)[0]["offset"]
    assert "<fifths>-2</fifths>" in score_to_musicxml(fine)
    assert_consistent(fine)


def test_assumptions_and_tempo_estimation_are_explicit():
    regular = [event(str(i), i * .5, i * .5 + .4, 60+i) for i in range(6)]
    assert estimate_tempo(regular)["bpm"] == 120
    assert estimate_tempo(regular)["source"] == "estimated"
    sparse = build_score([event("a", 0, 1)], {}, title="TEST FIXTURE")
    assert sparse["tempo"]["source"] == "assumed"
    assert any("Time signature" in a for a in sparse["assumptions"])
    assert "confidence" not in sparse["tempo"]
    hinted = default_score_settings({"tempo_hint": 90, "time_signature_hint": "3/4"})
    manual = build_score(regular, hinted, title="TEST FIXTURE")
    assert manual["tempo"]["source"] == "manual"
    assert not any("Time signature" in a for a in manual["assumptions"])


@pytest.mark.parametrize("settings", [
    {"pickup_beats": "4"}, {"pickup_beats": "1/3"}, {"pickup_beats": "-1"},
    {"time_signature": "4/3"}, {"tempo_bpm": float("nan")},
    {"first_downbeat_seconds": float("inf")}, {"time_signature": "3/8", "quantization": "quarter"},
])
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValueError):
        ScoreSettings(**settings)


def test_alignment_and_staff_errors_never_silently_drop_notes():
    with pytest.raises(ValueError, match="excludes detected"):
        score([event("a", 0, 1)], first_downbeat_seconds=2)
    with pytest.raises(ValueError, match="no longer present"):
        score([event("a", 0, 1)], piano_split=True, staff_overrides={"missing": "bass"})
    with pytest.raises(ValueError, match="outside MIDI"):
        score([event("a", 0, 1, 120)], transpose_semitones=12)


def test_notation_midi_uses_canonical_timing_and_tempo(tmp_path):
    import pretty_midi
    model = score([event("a", 0, 3, 60), event("b", 3.5, 4, 64)], tempo_bpm=90)
    path = tmp_path / "score.mid"
    write_score_midi(model, path)
    midi = pretty_midi.PrettyMIDI(str(path))
    assert midi.get_tempo_changes()[1][0] == pytest.approx(90, abs=.001)
    actual = midi.instruments[0].notes
    assert len(actual) == 2  # tied fragments are rejoined, not rearticulated
    assert actual[0].end == pytest.approx(3, abs=.001)
    assert actual[1].start == pytest.approx(3.666666, abs=.001)  # 5.5 quarter beats


def test_chord_preserves_per_pitch_dynamics_through_transposition_and_exports(tmp_path):
    import pretty_midi
    fixture = [event("quiet", 0, 1, 60), event("loud", 0, 1, 64),
               event("unison-quiet", 0, 1, 67), event("unison-loud", 0, 1, 67)]
    for item, velocity in zip(fixture, [39, 104, 51, 77]):
        item["velocity"] = velocity
    model = score(fixture, transpose_semitones=2)
    expected = {62: 39, 66: 104, 69: 77}
    written = notes(model)[0]
    assert written["pitch_velocities"] == {str(p): v for p, v in expected.items()}
    assert written["velocity"] == 104  # legacy aggregate remains compatible
    path = tmp_path / "balanced-chord.mid"
    write_score_midi(model, path)
    actual = [n for instrument in pretty_midi.PrettyMIDI(str(path)).instruments for n in instrument.notes]
    assert {n.pitch: n.velocity for n in actual} == expected
    assert len(actual) == 3  # only the same-pitch doubling collapses
    xml = score_to_musicxml(model)
    parsed = music21.converter.parseData(xml, format="musicxml")
    exported_chord = next(iter(parsed.recurse().notes))
    assert {int(n.pitch.midi): n.volume.velocity for n in exported_chord.notes} == expected


def test_tied_chord_midi_keeps_attack_velocities(tmp_path):
    import pretty_midi
    quiet, loud = event("quiet", 3, 6, 60), event("loud", 3, 6, 64)
    quiet["velocity"], loud["velocity"] = 43, 99
    model = score([quiet, loud])
    fragments = notes(model)
    assert [e["tie"] for e in fragments] == ["start", "stop"]
    # A later tied fragment never causes a new attack or overwrites its dynamic.
    fragments[-1]["pitch_velocities"] = {"60": 120, "64": 20}
    path = tmp_path / "tied-chord.mid"
    write_score_midi(model, path)
    actual = [n for instrument in pretty_midi.PrettyMIDI(str(path)).instruments for n in instrument.notes]
    assert sorted((n.pitch, n.velocity, n.start, n.end) for n in actual) == [(60, 43, 3, 6), (64, 99, 3, 6)]


def test_legacy_score_event_velocity_still_exports(tmp_path):
    import pretty_midi
    model = score([event("a", 0, 1, 60), event("b", 0, 1, 64)])
    for item in notes(model):
        item.pop("pitch_velocities")
        item["velocity"] = 71
    path = tmp_path / "legacy.mid"
    write_score_midi(model, path)
    assert [n.velocity for n in pretty_midi.PrettyMIDI(str(path)).instruments[0].notes] == [71, 71]
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    assert [n.volume.velocity for n in next(iter(parsed.recurse().notes)).notes] == [71, 71]


@pytest.mark.parametrize("grid", ["quarter", "eighth", "sixteenth", "eighth_triplet", "sixteenth_triplet"])
def test_deterministic_varied_events_preserve_rational_measure_invariants(grid):
    randomizer = random.Random(281)
    generated = []
    for index in range(40):
        start = randomizer.randrange(0, 100) / 4
        generated.append(event(f"generated-{index}", start, start + randomizer.randrange(1, 24)/4,
                               randomizer.randrange(42, 80)))
    model = score(generated, quantization=grid)
    assert_consistent(model)
    # Every event spanning adjacent measures carries a continuation tie.
    for staff in model["parts"][0]["staves"]:
        appearances = {}
        for measure in staff["measures"]:
            for voice in measure["voices"]:
                for item in voice["events"]:
                    if item["kind"] != "rest":
                        appearances.setdefault(tuple(item["source_note_ids"]), []).append(item)
        for group in appearances.values():
            if len(group) > 1:
                assert group[0]["tie"] == "start" and group[-1]["tie"] == "stop"
                assert all(item["tie"] == "continue" for item in group[1:-1])
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    for part in parsed.parts:
        for measure in part.getElementsByClass(music21.stream.Measure):
            assert Fraction(measure.highestTime) == 4


def test_compound_meter_with_triplet_grid_keeps_three_quarter_beats():
    model = score([event("a", 0, 1/3), event("b", 2.5, 3.5, 67)], time_signature="6/8", quantization="eighth_triplet")
    assert all(fraction(m["duration"]) == 3 for m in measures(model))
    assert_consistent(model)
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    assert parsed.parts[0].measure(1).highestTime == 3


def test_compound_meter_beams_follow_meter_in_later_measures():
    fixture = [event(f"eighth-{i}", i / 2, (i + 1) / 2, 60 + i % 5) for i in range(12)]
    model = score(fixture, time_signature="6/8", quantization="eighth")
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    second = list(parsed.parts[0].measure(2).recurse().notes)
    assert len(second) == 6
    assert [n.beams.getTypes() for n in second] == [["start"], ["continue"], ["stop"],
                                                  ["start"], ["continue"], ["stop"]]


def test_coincident_same_pitch_detection_is_reported_without_losing_provenance():
    model = score([event("first", 0, 1), event("second", .05, 1.05)])
    assert notes(model)[0]["source_note_ids"] == ["first", "second"]
    assert any("same-pitch detection" in warning for warning in model["warnings"])
    assert_consistent(model)


@pytest.mark.parametrize("fifths,midi,name", [(-7, 59, "C-4"), (7, 60, "B#3"), (7, 65, "E#4")])
def test_key_signature_spelling_keeps_exact_sounding_pitch(fifths, midi, name):
    model = score([event("spelling", 0, 1, midi)], key_signature=fifths)
    parsed = music21.converter.parseData(score_to_musicxml(model), format="musicxml")
    pitch = next(iter(parsed.recurse().notes)).pitch
    assert pitch.midi == midi and pitch.nameWithOctave == name


def test_single_score_title_is_not_repeated_as_a_movement_subtitle():
    model = score([event("title", 0, 1)])
    xml = ET.fromstring(score_to_musicxml(model))
    assert xml.find("work/work-title").text == model["title"]
    assert xml.find("movement-title") is None


def test_empty_piano_staff_does_not_expand_pickup_rest_to_full_bar():
    model = score([event("low-pickup", 0, 1, 48)], piano_split=True,
                  pickup_beats="1", first_downbeat_seconds=1)
    xml = score_to_musicxml(model)
    parsed = music21.converter.parseData(xml, format="musicxml")
    assert len(parsed.parts) == 2  # two staves of the single exported XML part
    for part in parsed.parts:
        assert part.measure(0).highestTime == 1
        assert part.measure(1).highestTime == 4
    assert_consistent(model)

"""Generated melodic fixtures, not evidence of human transcription accuracy."""
from copy import deepcopy
from fractions import Fraction

import pytest

from backend.app.edition_readability import prepare_edition_notes
from backend.app.score import build_score, fraction, score_to_musicxml, write_score_midi

SETTINGS = {"tempo_bpm": 120, "quantization": "eighth", "piano_split": True,
            "readability_mode": "accompaniment"}


def note(identifier, start, end, pitch=60, **extras):
    return {"id": identifier, "start": start, "end": end, "pitch": pitch, "velocity": 80,
            "part_id": "vocal", "voice_id": "1", "role": "melody", "hand": "right",
            "provenance": "detected", "original_timing": {"start": start, "end": end, "pitch": pitch}, **extras}


def prepare(notes, mode="solo_piano", **settings):
    return prepare_edition_notes(notes, {**SETTINGS, **settings}, mode=mode)


def test_explicit_melodic_extension_and_grid_edit_are_audited_and_original_is_immutable():
    source = [note("a", .012, .41, key_release_seconds=.41), note("b", .5, 1, 64)]
    before = deepcopy(source)
    result = prepare(source)
    edited = result["notes"][0]
    assert source == before
    assert (edited["start"], edited["end"], edited["key_release_seconds"]) == (0, .5, .5)
    assert edited["original_timing"] == before[0]["original_timing"]
    assert edited["pitch"] == 60 and edited["id"] == "a"
    assert edited["provenance"] == "edited" and edited["locked"] is False
    assert {c["field"] for c in result["changes"]} == {"start", "end", "key_release_seconds"}
    assert all(c["pass"] == "edition_melodic_duration" for c in result["changes"])


@pytest.mark.parametrize("mode,enabled", [("faithful", True), ("solo_piano", False)])
def test_faithful_and_disabled_pass_preserve_every_field(mode, enabled):
    source = [note("a", .012, .41), note("b", .5, 1, 64)]
    result = prepare_edition_notes(source, SETTINGS, mode=mode, enabled=enabled)
    assert result["notes"] == source and result["changes"] == []
    assert result["notes"] is not source and not result["settings"]["enabled"]


@pytest.mark.parametrize("extra", [{"locked": True}, {"locked": None, "provenance": "edited"},
                                   {"provenance": "manually_added"}, {"articulation": "staccato"},
                                   {"breath_after": True}, {"phrase_end": True}, {"fingering": 4},
                                   {"pedal_sustained": True, "key_release_seconds": .3}])
def test_locks_and_explicit_interpretation_are_unchanged(extra):
    source = [note("a", .012, .41, **extra), note("b", .5, 1, 64)]
    assert prepare(source)["notes"][0] == source[0]


def test_accompaniment_articulation_and_independent_holds_are_never_normalized():
    source = [note("bass", .012, 1.41, role="bass", hand="left"),
              note("harmony", .5, .91, 64, role="harmony"),
              note("riff", .012, .41, 67, role="riff"),
              note("unknown", .5, .91, 72, role=None)]
    assert prepare(source, mode="accompaniment")["notes"] == source


def test_repeated_attacks_keep_their_ids_onsets_and_release_gap():
    source = [note("a", 0, .41), note("b", .5, 1)]
    assert prepare(source)["notes"] == source


@pytest.mark.parametrize("extra", [{"voice_id": "2"}, {"part_id": "other"},
                                   {"phrase_id": "next"}, {"phrase_start": True}, {"breath_before": True}])
def test_line_and_phrase_boundaries_are_not_bridged(extra):
    source = [note("a", 0, .41), note("b", .5, 1, 64, **extra)]
    assert prepare(source)["notes"][0] == source[0]


def test_large_gaps_short_notes_and_overlapping_melody_are_unchanged():
    source = [note("short", 0, .11), note("after-short", .25, .61, 64),
              note("long-gap", 1, 1.41, 67), note("overlap", 1.25, 1.75, 72)]
    assert prepare(source)["notes"] == source


def test_off_grid_short_neighbor_cannot_create_an_invented_grid_attack():
    source = [note("long", 0, .91), note("short", 1.02, 1.1, 64)]
    assert prepare(source)["notes"] == source


def test_manual_physical_release_is_preserved_without_inferred_pedal():
    source = [note("a", .012, .41, key_release_seconds=.3), note("b", .5, 1, 64)]
    assert prepare(source)["notes"][0] == source[0]


def test_triplet_pickup_alignment_and_existing_raw_lineage_survive():
    source = [note("a", .01, .4, original_timing={"start": .023, "end": .397, "pitch": 60}),
              note("b", .5, 1, 64)]
    result = prepare(source, quantization="eighth_triplet", pickup_beats="1", first_downbeat_seconds=.5)
    assert result["notes"][0]["start"] == 0
    assert result["notes"][0]["end"] == .5
    assert result["notes"][0]["original_timing"] == source[0]["original_timing"]


def test_separate_melodic_grid_matches_compiler_without_changing_instrumental_grid():
    source = [note("a", .132, .63), note("b", .75, 1.25, 64),
              note("harmony", .132, .63, 48, role="harmony", hand="left")]
    fine = prepare(source, melody_quantization="sixteenth")
    assert (fine["notes"][0]["start"], fine["notes"][0]["end"]) == (.125, .625)
    assert fine["notes"][2] == source[2]
    assert fine["settings"]["quantization"] == "eighth"
    assert fine["settings"]["melody_quantization"] == "sixteenth"
    assert prepare(source)["notes"][0] == source[0]


def test_current_edited_melody_roundtrips_xml_and_midi_with_valid_measures(tmp_path):
    music21 = pytest.importorskip("music21")
    pretty_midi = pytest.importorskip("pretty_midi")
    source = [note("a", .012, .41), note("b", .5, 1, 64), note("held", 1, 2.41, 67),
              note("last", 2.5, 3, 69)]
    result = prepare(source)
    score = build_score(result["notes"], SETTINGS, title="DEVELOPMENT melodic duration fixture")
    for part in score["parts"]:
        for staff in part["staves"]:
            for measure in staff["measures"]:
                for voice in measure["voices"]:
                    assert sum((fraction(e["duration"]) for e in voice["events"]), Fraction(0)) == fraction(measure["duration"])
    parsed = music21.converter.parseData(score_to_musicxml(score), format="musicxml").stripTies()
    xml_events = sorted((int(n.pitch.midi), float(n.offset) / 2, float(n.quarterLength) / 2)
                        for p in parsed.parts for n in p.flatten().notes if n.isNote)
    path = tmp_path / "edited.mid"
    write_score_midi(score, path)
    midi = pretty_midi.PrettyMIDI(str(path))
    midi_events = sorted((n.pitch, n.start, n.end - n.start) for i in midi.instruments for n in i.notes)
    assert midi_events == xml_events == [(60, 0, .5), (64, .5, .5), (67, 1, 1.5), (69, 2.5, .5)]


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="Choose faithful"):
        prepare([], mode="unspecified")

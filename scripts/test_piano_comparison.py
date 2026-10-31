"""Generated notes verify the comparison logic, not musical recognizability."""
from copy import deepcopy
import json

import pytest

from scripts.compare_piano_arrangements import compare_arrangements, extract_notes, main


def note(identifier, pitch=60, start=0, end=1, **fields):
    return {"id": identifier, "pitch": pitch, "start": start, "end": end,
            "hand": "right", "locked": False, "provenance": "detected", "velocity": 80,
            "original_timing": {"pitch": pitch, "start": start, "end": end}, **fields}


def rational(numerator, denominator=1):
    return {"numerator": numerator, "denominator": denominator}


def score(pitches, offset=rational(1, 3), tie=None, **fields):
    return {"settings": {"time_signature": "4/4", "transpose_semitones": 0}, "tempo": {"bpm": 120},
            "origin_seconds": 0, "bar_duration": rational(4), "grid": rational(1, 3),
            "parts": [{"staves": [{"measures": [{"number": 1, "voices": [{"events": [
                {"kind": "chord", "offset": offset, "pitches": pitches, "tie": tie}]}]}]}]}], **fields}


def test_exact_changes_and_counts_cannot_hide_omissions_or_repeated_notes():
    before = [note("a"), note("b", 64), note("omitted", 67)]
    after = [note("a", 72), note("b", 65, .0000000001, 1.1, hand="left"), note("added", 67)]
    report = compare_arrangements(before, after)
    assert report["ids"] == {"retained": ["a", "b"], "omitted": ["omitted"], "added": ["added"]}
    assert report["summary"]["octave_changes"] == report["summary"]["chromatic_changes"] == 1
    assert report["onset_changes"][0]["delta_seconds"] == "1E-10"
    assert report["release_changes"][0]["fields"]["end"]["delta_seconds"] == "0.1"
    assert report["summary"]["hand_changes"] == 1
    assert report["changes_by_original_hand"]["right"]["pitch_changes"] == 2
    assert not report["raw_onset_multiset"]["exact"]
    assert not report["human_recognizability_validated"]
    assert report["canonical_score"]["available"] is False


def test_locked_default_and_explicit_inferred_protection_report_metadata_separately():
    before = [note("locked", provenance="edited", locked=None), note("explicit", role="riff"),
              note("inferred", role="bass", arrangement_provenance={"inferred_role": "bass"})]
    after = deepcopy(before)
    after[0]["fingering"] = 3
    after[1]["hand"] = "left"
    after[2]["pitch"] += 12
    report = compare_arrangements(before, after)
    assert report["locked_notes"]["note_ids"] == ["locked"]
    assert not report["locked_notes"]["all_fields_exact"]
    assert report["locked_notes"]["pitch_timing_pedal_exact"]
    riff = report["protected_roles"]["riff"]["explicit"]
    assert riff["count"] == 1 and not riff["all_fields_exact"] and riff["pitch_timing_pedal_exact"]
    assert report["protected_roles"]["bass"]["explicit"]["count"] == 0
    assert not report["protected_roles"]["bass"]["inferred"]["pitch_timing_pedal_exact"]


def test_canonical_multisets_use_rational_onsets_and_counts_skip_cues_and_tie_continuations():
    before = score([60, 64, 67])
    after = score([72, 76, 79], rational(2, 6))
    cue = deepcopy(before["parts"][0]); cue["is_cue"] = True
    after["parts"].append(cue)
    continuation = {"kind": "note", "offset": rational(2), "pitches": [60], "tie": "stop"}
    after["parts"][0]["staves"][0]["measures"][0]["voices"][0]["events"].append(continuation)
    result = compare_arrangements([], [], before, after)["canonical_score"]
    assert result["all_pitch_class_onset_multisets_exact"]
    after["parts"][0]["staves"][0]["measures"][0]["voices"][0]["events"][0]["pitches"].append(72)
    result = compare_arrangements([], [], before, after)["canonical_score"]
    assert not result["all_pitch_class_onset_multisets_exact"]
    assert result["measures"][0]["pitch_class_onset_multiset"]["added"] == [{"offset_beats": "1/3", "pitch_class": 0, "count": 1}]


def test_changing_rhythm_or_measure_mapping_is_visible():
    before, after = score([60]), score([60], rational(1, 2))
    after["settings"]["time_signature"] = "3/4"
    after["tempo"]["bpm"] = 100
    result = compare_arrangements([], [], before, after)["canonical_score"]
    assert result["all_pitch_class_multisets_exact"]
    assert not result["all_onset_multisets_exact"]
    assert "time_signature" in result["settings_changes"]
    assert "tempo" in result["time_mapping_changes"]


def test_physical_releases_original_times_and_missing_metadata_are_not_ignored():
    before = [note("a", key_release_seconds=.5, pedal_sustained=True, role=None)]
    after = deepcopy(before)
    after[0]["key_release_seconds"] = .6
    after[0]["original_timing"]["start"] = .1
    del after[0]["role"]
    report = compare_arrangements(before, after)
    assert report["summary"]["release_changes"] == 1
    assert report["original_timing_changed_ids"] == ["a"]
    assert not report["all_changed_fields"][0]["fields"]["role"]["after_present"]


def test_empty_identity_and_supported_input_shapes(tmp_path):
    notes = [note("one")]
    for document in (notes, {"notes": notes}, {"project": {"notes_json": json.dumps(notes)}},
                     {"score": score([60], source_notes=notes)}):
        assert extract_notes(document) == notes
    with pytest.raises(ValueError, match="Duplicate"):
        compare_arrangements(notes + notes, notes)
    with pytest.raises(ValueError, match="notes/source_notes"):
        extract_notes(score([60]))
    report = compare_arrangements([], [])
    assert report["summary"]["before_notes"] == 0
    assert report["raw_onset_multiset"]["exact"]


def test_cli_full_report_and_exact_protection_failure(tmp_path, capsys):
    before = [note("locked", locked=True)]
    after = deepcopy(before); after[0]["hand"] = "left"
    first, second, output = [tmp_path / name for name in ("before.json", "after.json", "report.json")]
    first.write_text(json.dumps(before)); second.write_text(json.dumps(after))
    result = main(["--before", str(first), "--after", str(second), "--output", str(output), "--require-exact-protected"])
    assert result == 2
    report = json.loads(output.read_text())
    assert not report["summary"]["locked_notes_exact"]
    assert json.loads(capsys.readouterr().out)["hand_changes"] == 1

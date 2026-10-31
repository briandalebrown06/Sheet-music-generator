"""Cleanup/MIDI unit tests plus opt-in real raw-artifact inference integration.

Manually authored event arrays below are labeled cleanup fixtures, never
application transcription. The opt-in integration invokes the actual model.
"""
from __future__ import annotations

import json
import os
import wave
from copy import deepcopy

import pytest

from backend.app.notes import cleanup_notes, cleanup_settings, detected_notes
from backend.app.performance import write_performance_midi


def model_evidence(onset=0.0, activation=0.8):
    import numpy as np
    output = {"note": np.full((200, 88), activation), "onset": np.full((200, 88), onset)}
    return output, np.arange(200) / 100


def test_stable_detected_identity_and_immutable_original_timing():
    fixture = [(0.1, 0.7, 60, 0.75, None), (1.0, 1.4, 64, 0.5, [0, 1])]
    original = deepcopy(fixture)
    first = detected_notes(fixture, "job-fixture")
    assert first == detected_notes(fixture, "job-fixture")
    assert first[0]["id"] != first[1]["id"]
    assert first[0]["source_note_ids"] == [first[0]["id"]]
    assert first[0]["provenance"] == "detected"
    assert first[0]["part_id"] == "part-1"
    assert first[0]["model_score"] == 0.75
    assert first[0]["score_timing"] is None
    first[0]["start"] = 0.2  # Simulates an edit of the working copy, not raw data.
    assert first[0]["original_timing"]["start"] == 0.1
    assert fixture == original


def test_short_artifact_cleanup_is_configurable_and_preserves_raw_fixture():
    fixture = [(0.0, 0.04, 60, 0.5), (0.2, 0.7, 64, 0.8)]
    original = deepcopy(fixture)
    notes, audit = cleanup_notes(fixture, "job")
    assert [note["pitch"] for note in notes] == [64]
    assert len(audit["removed_short_note_ids"]) == 1
    assert len(cleanup_notes(fixture, "job", {"minimum_note_seconds": 0})[0]) == 2
    assert fixture == original


def test_weak_onset_continuous_fragments_merge_without_retiming_original():
    output, times = model_evidence()
    fixture = [(0.0, 0.5, 60, 0.7), (0.52, 1.0, 60, 0.8)]
    notes, audit = cleanup_notes(fixture, "job", model_output=output, frame_times=times)
    assert len(notes) == 1
    assert notes[0]["start"] == 0 and notes[0]["end"] == 1
    assert notes[0]["original_timing"]["end"] == 0.5
    assert len(notes[0]["source_note_ids"]) == 2
    assert audit["merged_source_note_groups"] == [notes[0]["source_note_ids"]]


def test_repeated_notes_and_chords_not_merged_by_proximity_alone():
    output, times = model_evidence()
    fixture = [(0.0, 0.5, 60, 0.7), (0.52, 1.0, 60, 0.8), (0.52, 1.0, 64, 0.8)]
    output["onset"][51:54, 60 - 21] = 0.8
    notes, _ = cleanup_notes(fixture, "job", model_output=output, frame_times=times)
    assert len(notes) == 3
    # Missing evidence must retain repeats, including overlapping decay tails.
    assert len(cleanup_notes(fixture, "job")[0]) == 3
    overlap = [(0, 0.6, 60, 0.8), (0.5, 1, 60, 0.8)]
    assert len(cleanup_notes(overlap, "job")[0]) == 2


def test_silent_gap_or_frame_rise_blocks_fragment_merge():
    fixture = [(0, 0.5, 60, 0.7), (0.52, 1, 60, 0.8)]
    output, times = model_evidence()
    output["note"][50, 39] = 0.1
    assert len(cleanup_notes(fixture, "job", model_output=output, frame_times=times)[0]) == 2
    output, times = model_evidence()
    output["note"][50, 39] = 0.4
    assert len(cleanup_notes(fixture, "job", model_output=output, frame_times=times)[0]) == 2


def test_disable_merge_and_clip_bounds_preserve_raw_snapshot():
    output, times = model_evidence()
    fixture = [(0, 0.5, 60, 0.8), (0.52, 1.1, 60, 0.8)]
    notes, _ = cleanup_notes(fixture, "job", {"merge_gap_seconds": 0}, output, times, clip_duration=1)
    assert len(notes) == 2 and notes[1]["end"] == 1
    assert notes[1]["original_timing"]["end"] == 1.1
    assert cleanup_notes([(1.1, 1.3, 60, 0.8)], "job", clip_duration=1)[0] == []


@pytest.mark.parametrize("settings", [{"minimum_note_seconds": -1}, {"merge_gap_seconds": 0.3},
                                      {"minimum_note_seconds": float("nan")}, {"unknown": 1}])
def test_invalid_cleanup_is_rejected(settings):
    with pytest.raises(ValueError):
        cleanup_settings(settings)


def test_performance_midi_roundtrip_retains_unquantized_seconds_and_parts(tmp_path):
    pretty_midi = pytest.importorskip("pretty_midi")
    notes = detected_notes([(0.12345, 0.87654, 60, 0.7), (0.50234, 1.23456, 64, 0.6)], "job")
    notes[1]["part_id"] = "part-2"
    path = tmp_path / "performance.mid"
    write_performance_midi(notes, path)
    restored = pretty_midi.PrettyMIDI(str(path))
    assert len(restored.instruments) == 2
    assert [instrument.name for instrument in restored.instruments] == ["part-1", "part-2"]
    for expected, instrument in zip(notes, restored.instruments):
        actual = instrument.notes[0]
        assert actual.pitch == expected["pitch"]
        assert actual.start == pytest.approx(expected["start"], abs=0.0001)
        assert actual.end == pytest.approx(expected["end"], abs=0.0001)
    write_performance_midi([], path)
    assert pretty_midi.PrettyMIDI(str(path)).instruments == []


@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS") != "1", reason="Opt-in actual Basic Pitch inference")
def test_real_analysis_preserves_raw_artifacts_and_stable_schema(tmp_path):
    import numpy as np
    import pretty_midi
    from backend.app.engine import BasicPitchEngine
    from backend.tests.test_real_analysis import fixture_audio
    source = tmp_path / "DEVELOPMENT-FIXTURE.wav"
    source.write_bytes(fixture_audio())
    stages = []
    result = BasicPitchEngine().analyze(source, tmp_path / "analysis", "fixture-job", stage_callback=stages.append)
    assert {60, 64}.issubset({note["pitch"] for note in result["notes"]})
    output = tmp_path / "analysis"
    with np.load(output / "raw-model-output.npz", allow_pickle=False) as activations:
        assert set(activations.files) == {"note", "onset", "contour"}
        assert activations["note"].shape[0] > 0
    raw = json.loads((output / "raw-note-events.json").read_text())
    assert raw["note_events"] and len(raw["note_ids"]) == len(raw["note_events"])
    assert pretty_midi.PrettyMIDI(str(output / "raw-performance.mid")).instruments
    assert result["manifest"]["model_sha256"]
    assert result["manifest"]["windowing"]["overlap_frames"] == 30
    assert result["notes"] == json.loads((output / "notes.json").read_text())["notes"]
    assert "Preserving raw model output" in stages
    assert all(note["end"] <= 3 and note["score_timing"] is None for note in result["notes"])

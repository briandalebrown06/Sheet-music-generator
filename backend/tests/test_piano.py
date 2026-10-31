"""Explicit event fixtures for arrangement constraints, never fake inference."""
from copy import deepcopy
import uuid

import pytest
from pydantic import ValidationError

from backend.app.piano import PianoSettings, arrange_piano, assess_playability


def event(label, pitch, start=0., end=.45, **extra):
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "piano-development-fixture:" + label))
    return {"id": identifier, "pitch": pitch, "start": start, "end": end,
            "velocity": 80, "part_id": "fixture-piano", "voice_id": None,
            "provenance": "detected", "model_score": .5,
            "source_note_ids": [identifier], "original_timing": {"pitch": pitch, "start": start, "end": end},
            **extra}


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_default_reach_and_finger_limits_are_explicit_and_bounded():
    profile = PianoSettings()
    assert profile.left_reach == profile.right_reach == 9
    assert profile.max_keys_per_hand == 5
    for invalid in ({"left_reach": 4}, {"right_reach": 13}, {"max_keys_per_hand": 6}, {"max_jump_semitones_per_second": float("nan")}):
        with pytest.raises(ValidationError):
            PianoSettings(**invalid)


def test_oversized_one_hand_chord_is_actually_repaired_without_changing_source():
    source = [event(str(pitch), pitch, hand="right") for pitch in (48, 60, 64, 67, 72)]
    original = deepcopy(source)
    assert "hand_reach" in codes(assess_playability(source))
    result = arrange_piano(source)
    assert source == original
    assert result["changes"] and result["arrangement"]
    assert result["report"]["summary"]["passed_automated_constraints"]
    assert {n["pitch"] % 12 for n in result["notes"]} == {n["pitch"] % 12 for n in source}
    assert any(n["pitch"] == 48 for n in result["notes"])
    assert all(n["start"] == 0 for n in result["notes"]), "Do not disguise repair as an automatic arpeggio"
    assert all(n["source_note_ids"] and n["original_timing"] for n in result["notes"])
    assert all(n["locked"] is False for n in result["notes"])


def test_wide_separation_between_two_hands_is_not_a_stretch_error():
    notes = [event("left", 36, hand="left"), event("right", 96, hand="right")]
    assert assess_playability(notes)["summary"]["passed_automated_constraints"]


def test_held_keys_count_against_new_chord_and_reassignment_repairs_conflict():
    held = event("held-bass", 48, 0, 2, hand="left", role="bass")
    notes = [held] + [event(f"next-{p}", p, .5, 1, hand="left") for p in (64, 67, 71)]
    report = assess_playability(notes, {}, {"tempo_bpm": 120})
    assert "held_note_conflict" in codes(report)
    result = arrange_piano(notes, {}, {"tempo_bpm": 120})
    assert result["report"]["summary"]["passed_automated_constraints"]
    retained_bass = next(n for n in result["notes"] if n["id"] == held["id"])
    assert retained_bass["pitch"] == held["pitch"] and retained_bass["end"] == held["end"]
    assert all(n["hand"] == "right" for n in result["notes"] if n["id"] != held["id"])


def test_fast_reachable_chords_need_phrase_repair_and_do_not_cross_alternating_hands():
    notes = [event(f"{t}-{p}", p, t, t + .1, hand="right")
             for t, pitches in ((0, (60, 64, 67)), (.125, (84, 88, 91)), (.25, (60, 64, 67)))
             for p in pitches]
    settings = {"tempo_bpm": 240, "quantization": "sixteenth"}
    assert "rapid_jump" in codes(assess_playability(notes, {}, settings))
    result = arrange_piano(notes, {}, settings)
    assert result["report"]["summary"]["passed_automated_constraints"]
    assert len(result["notes"]) == len(notes)
    assert {n["start"] for n in result["notes"]} == {0, .125, .25}
    assert all(n["hand"] == ("right" if n["start"] == .125 else "left") for n in result["notes"])
    assert {n["pitch"] for n in result["notes"]} == {60, 64, 67, 84, 88, 91}


def test_pedal_release_is_distinct_from_sounding_duration_but_cannot_excuse_chord():
    held = event("resonance", 48, 0, 4, hand="left", key_release_seconds=.25)
    next_note = event("next", 64, 1, 1.5, hand="left")
    assert "held_note_conflict" in codes(assess_playability([held, next_note]))
    pedal = {**held, "pedal_sustained": True}
    assert "held_note_conflict" not in codes(assess_playability([pedal, next_note]))
    chord = [event(str(p), p, 0, 4, hand="right", key_release_seconds=.25, pedal_sustained=True) for p in (60, 64, 79)]
    assert "hand_reach" in codes(assess_playability(chord))
    assert pedal["end"] == 4, "The assessment must not erase sounding sustain"


def test_keyboard_shape_is_more_than_pitch_span_and_five_key_count():
    notes = [event(str(p), p, hand="right") for p in (60, 61, 62, 63, 69)]
    report = assess_playability(notes)
    assert report["summary"]["max_right_span"] == 9 and report["summary"]["max_right_keys"] == 5
    assert "hand_reach" not in codes(report) and "finger_load" not in codes(report)
    assert "keyboard_shape" in codes(report)


def test_neighboring_single_notes_can_use_different_fingers_without_a_hand_jump():
    notes = [event("neighbor-a", 60, 0, .1, hand="right"), event("neighbor-b", 67, .125, .225, hand="right")]
    report = assess_playability(notes, {}, {"tempo_bpm": 240, "quantization": "sixteenth"})
    assert "rapid_jump" not in codes(report)


def test_dense_mixed_input_reduces_secondary_doublings_preserves_marked_riff_and_bass():
    pitches = (36, 48, 55, 60, 64, 67, 72, 76, 79, 84, 88, 91, 96, 100)
    notes = [event(f"mixed-{i}", p, part_id=f"instrument-{i % 4}",
                   role="bass" if p == 36 else "riff" if p == 84 else "texture") for i, p in enumerate(pitches)]
    result = arrange_piano(notes)
    assert len(result["notes"]) < len(notes)
    assert result["report"]["summary"]["passed_automated_constraints"]
    assert {36, 84} <= {n["pitch"] for n in result["notes"]}
    assert {n["pitch"] % 12 for n in result["notes"]} == {n["pitch"] % 12 for n in notes}
    assert any(c["action"] == "omit" for c in result["changes"])
    assert any(e["field"] == "pitch" for c in result["changes"] for e in c.get("edits", []))


def test_locked_user_notes_remain_exact_even_when_constraints_are_impossible():
    source = [event("locked-low", 36, 0, 2, hand="left", provenance="edited", locked=None),
              event("locked-high", 72, 0, 2, hand="left", provenance="manually_added")]
    result = arrange_piano(source)
    assert "hand_reach" in codes(result["report"])
    assert len(result["notes"]) == 2
    for n, original in zip(sorted(result["notes"], key=lambda n:n["pitch"]), source):
        assert all(n[k] == original[k] for k in ("pitch", "start", "end", "hand"))
        assert n["locked"]
    assert result["report"]["issues"][0]["measure"] == 1
    assert set(result["report"]["issues"][0]["note_ids"]) == {n["id"] for n in source}


def test_transposition_is_exact_in_assessment_and_revoice_is_separate():
    source = [event("upper", 103, hand="right"), event("upper-inner", 99, hand="right")]
    original = deepcopy(source)
    settings = {"transpose_semitones": 12}
    assert "keyboard_range" in codes(assess_playability(source, {}, settings))
    assert source == original
    result = arrange_piano(source, {}, settings)
    # The automatically inferred lowest note is protected bass; explicit
    # range issues can remain and are reported rather than silently transposed.
    assert all((n["pitch"] - next(o["pitch"] for o in source if o["id"] == n["id"])) % 12 == 0 for n in result["notes"])
    assert source == original
    assert result["report"]["transpose_semitones"] == 12
    assert all("keyboard_range" == i["code"] or "measure" in i for i in result["report"]["issues"])


def test_auto_bass_articulation_preserves_pitch_onset_and_never_invents_pedal():
    source = [event("first-bass", 48, 0, 2, hand="left"), event("first-right", 72, 0, .4, hand="right"),
              event("next-bass", 36, .5, 1, hand="left"), event("next-right", 76, .5, 1, hand="right", locked=True)]
    result = arrange_piano(source)
    assert not result["report"]["summary"]["error_count"]
    before_by_id = {n["id"]: n for n in source}
    bass = [n for n in result["notes"] if n.get("role") == "bass"]
    assert len(bass) == 2
    assert all((n["pitch"], n["start"]) == (before_by_id[n["id"]]["pitch"], before_by_id[n["id"]]["start"]) for n in bass)
    assert not any(n.get("pedal_sustained") for n in result["notes"])


def test_vocal_cue_is_optional_and_never_assigned_to_a_pianist_hand():
    source = [event("piano", 48, hand="left"), event("vocal", 76, role="vocal_cue")]
    hidden = arrange_piano(source)
    shown = arrange_piano(source, {"include_vocal_cue": True})
    assert len(hidden["notes"]) == 1 and len(shown["notes"]) == 2
    cue = next(n for n in shown["notes"] if n.get("role") == "vocal_cue")
    assert cue.get("hand") is None
    assert shown["report"]["summary"]["vocal_cue_notes_excluded_from_hand_checks"] == 1
    locked = arrange_piano([{**source[1], "locked": True}])
    assert len(locked["notes"]) == 1, "A locked user cue cannot be silently discarded by a default setting"


def test_constraint_report_measure_is_current_attack_not_old_held_note():
    notes = [event("old", 36, 0, 8, hand="left", locked=True),
             event("current", 60, 5, 6, hand="left", locked=True)]
    report = assess_playability(notes, {}, {"tempo_bpm": 60})
    held = next(i for i in report["issues"] if i["code"] == "held_note_conflict")
    assert held["measure"] == 2 and held["hand"] == "left"


def test_repeated_original_attacks_survive_and_algorithmic_edits_are_revoiceable():
    notes = [event(str(i), 60, i * .5, i * .5 + .4) for i in range(4)]
    first = arrange_piano(notes)
    second = arrange_piano(first["notes"], {"left_reach": 7, "right_reach": 7})
    assert len(first["notes"]) == len(second["notes"]) == 4
    assert [n["start"] for n in second["notes"]] == [0, .5, 1, 1.5]
    assert not second["locked_note_ids"]


def test_duplicate_attacks_are_audited_and_unsupported_values_rejected():
    source = [event("same-a", 60, hand="right"), event("same-b", 60, hand="right")]
    assert "duplicate_attack" in codes(assess_playability(source))
    assert len(arrange_piano(source)["notes"]) == 1
    for invalid in ({"start": float("nan")}, {"hand": "third"}, {"key_release_seconds": 7}, {"role": "invented"}):
        with pytest.raises(ValueError):
            assess_playability([{**event("invalid", 60), **invalid}])


def test_manual_finger_cannot_hold_one_key_and_attack_a_different_key():
    notes = [event("finger-held", 48, 0, 2, hand="left", fingering=1, locked=True),
             event("finger-new", 52, 1, 1.5, hand="left", fingering=1, locked=True)]
    report = assess_playability(notes)
    conflict = next(i for i in report["issues"] if i["code"] == "manual_finger_conflict")
    assert conflict["hand"] == "left" and conflict["finger"] == 1
    assert set(conflict["note_ids"]) == {n["id"] for n in notes}
    arranged = arrange_piano(notes)
    assert "manual_finger_conflict" in codes(arranged["report"])
    assert all(n["fingering"] == 1 and n["hand"] == "left" for n in arranged["notes"])
    assert not arranged["report"]["summary"]["hard_constraints_passed"]


def test_fast_triplet_attacks_are_not_collapsed_by_attack_tolerance():
    notes = [event(f"fast-triplet-{i}", 60, i / 30, (i + 1) / 30, hand="right") for i in range(6)]
    settings = {"tempo_bpm": 300, "quantization": "sixteenth_triplet"}
    assert "duplicate_attack" not in codes(assess_playability(notes, {}, settings))
    arranged = arrange_piano(notes, {}, settings)
    assert len(arranged["notes"]) == len(notes)
    assert [n["start"] for n in arranged["notes"]] == [n["start"] for n in notes]


def inferred(label, pitch, start, end, hand, role="bass", **extra):
    """Explicit editorial role fixture, not a model-generated interpretation."""
    return event(label, pitch, start, end, hand=hand, role=role, locked=False,
                 arrangement_provenance={"inferred_role": role}, **extra)


def test_targeted_phrase_repair_preserves_attacks_roots_roles_voices_and_unrelated_notes():
    settings = {"tempo_bpm": 120, "quantization": "eighth"}
    notes = [inferred(f"line-{i}", pitch, i * .5, i * .5 + .25, hand, voice_id="bass-line")
             for i, (pitch, hand) in enumerate(((44, "left"), (56, "right"), (45, "left"), (57, "right"), (46, "left")))]
    notes += [event("unrelated-locked", 84, 8, 8.5, hand="right", role="riff", locked=True,
                    fingering=2, custom_editor_metadata={"keep": "exact"})]
    original = deepcopy(notes)
    assert "line_reassignment" in codes(assess_playability(notes, {}, settings))
    arranged = arrange_piano(notes, {}, settings)
    assert arranged["preserve_timing"] is True
    assert arranged["report"]["summary"]["passed_automated_constraints"]
    assert len(arranged["notes"]) == len(notes) and notes == original
    by_id = {n["id"]: n for n in arranged["notes"]}
    for n in notes:
        changed = by_id[n["id"]]
        for field in ("id", "start", "end", "velocity", "role", "voice_id", "original_timing", "source_note_ids"):
            assert changed.get(field) == n.get(field)
        assert changed["pitch"] % 12 == n["pitch"] % 12
    assert by_id[notes[-1]["id"]] == notes[-1]
    assert all(e["field"] == "hand" for c in arranged["changes"] for e in c["edits"]), "Prefer hand-only repairs when pitches already work"
    reopened = arrange_piano(arranged["notes"], {}, settings)
    assert reopened["notes"] == arranged["notes"] and not reopened["changes"]
    assert reopened["refinement"]["candidate_evaluations"] == 0


def test_targeted_repair_moves_coordinated_chord_octaves_without_flattening_rhythm():
    notes = [event("preceding-fixed", 65, 0, .25, hand="left", role="riff", locked=True),
             event("right-fixed", 76, 0, 1, hand="right", role="countermelody", locked=True),
             inferred("jump-bass", 41, .25, .5, "left"),
             inferred("jump-third", 44, .25, .5, "left", "harmony")]
    settings = {"tempo_bpm": 120, "quantization": "eighth"}
    assert codes(assess_playability(notes, {}, settings)) == {"rapid_jump"}
    result = arrange_piano(notes, {}, settings)
    assert result["report"]["summary"]["passed_automated_constraints"]
    assert [(n["pitch"], n["start"], n["end"]) for n in result["notes"][-2:]] == [(53, .25, .5), (56, .25, .5)]
    assert result["notes"][:2] == notes[:2]
    assert all(c["action"] == "modify" for c in result["changes"])


def test_targeted_release_is_explicit_bounded_and_only_used_when_pitch_options_fail():
    notes = [event("fixed-opening", 65, 0, .25, hand="left", role="riff", locked=True),
             event("fixed-upper", 71, 0, 2, hand="right", role="countermelody", locked=True),
             event("fixed-upper-octave", 80, 0, 2, hand="right", role="harmony", locked=True),
             inferred("auto-held", 58, .25, 1, "left"),
             event("marked-root", 41, 1, 1.5, hand="left", role="bass")]
    settings = {"tempo_bpm": 120, "quantization": "eighth"}
    assert "rapid_jump" in codes(assess_playability(notes, {}, settings))
    result = arrange_piano(notes, {}, settings)
    assert result["report"]["summary"]["passed_automated_constraints"]
    changed = next(n for n in result["notes"] if n["id"] == notes[3]["id"])
    assert changed["end"] == .75 and changed["pitch"] == 58 and changed["start"] == .25
    assert not changed.get("pedal_sustained") and changed.get("key_release_seconds") is None
    assert changed["original_timing"] == notes[3]["original_timing"]
    assert next(n for n in result["notes"] if n["id"] == notes[4]["id"])["pitch"] == 41
    assert any(e["field"] == "end" and "no pedal" in e["reason"] for c in result["changes"] for e in c["edits"])


def test_targeted_locked_or_manually_fingered_jump_remains_honestly_reported():
    notes = [event("manual-first", 44, 0, .25, hand="left", role="riff", fingering=1),
             event("manual-next", 80, .25, .5, hand="left", role="riff", locked=True)]
    result = arrange_piano(notes, {}, {"tempo_bpm": 120, "quantization": "eighth"})
    assert result["notes"] == notes and result["changes"] == []
    assert "rapid_jump" in codes(result["report"])
    assert not result["report"]["summary"]["passed_automated_constraints"]


def test_targeted_search_budget_returns_unchanged_constraints_and_honest_pending_review(monkeypatch):
    import backend.app.piano as piano
    notes = [inferred(str(i), pitch, i * .5, i * .5 + .25, hand)
             for i, (pitch, hand) in enumerate(((44, "left"), (56, "right"), (45, "left"), (57, "right")))]
    before = assess_playability(notes, {}, {"tempo_bpm": 120})
    monkeypatch.setattr(piano, "_REFINEMENT_EVALUATIONS", 1)
    result = arrange_piano(notes, {}, {"tempo_bpm": 120})
    assert result["notes"] == notes and result["report"] == before
    assert result["refinement"]["budget_exhausted"]
    assert result["refinement"]["candidate_evaluations"] <= 1


def test_comfortable_density_target_is_separate_from_five_finger_limit_and_counts_held_keys():
    notes = [event("held-density", 60, 0, 1, hand="right", role="harmony")]
    notes += [event(f"attack-density-{pitch}", pitch, .5, 1, hand="right", role="harmony") for pitch in (62, 64, 67)]
    report = assess_playability(notes, {}, {"tempo_bpm": 120})
    assert report["summary"]["hard_constraints_passed"]
    assert report["density"]["target_keys_per_hand"] == 3 and report["density"]["max_keys_per_hand"] == 5
    assert report["density"]["max_right_keys"] == 4 and report["density"]["over_target_frames"] == 1
    assert report["density"]["excess_key_frames"] == 1 and not report["density"]["target_met"]
    assert PianoSettings().target_keys_per_hand == 3
    for invalid in (0, 6):
        with pytest.raises(ValidationError):
            PianoSettings(target_keys_per_hand=invalid)


def test_texture_reduction_preserves_recurring_attacks_and_protected_roots_melody_and_raw_timing():
    from backend.app.piano import refine_piano_texture
    notes = []
    for i in range(4):
        at = i * .5
        notes += [event(f"riff-root-{i}", 36, at, at + .25, hand="left", role="bass"),
                  event(f"riff-lead-{i}", 69, at, at + .25, hand="right", role="melody")]
        notes += [event(f"riff-inner-{i}-{pitch}", pitch, at, at + .25, hand="right", role="harmony") for pitch in (60, 62, 64, 67)]
    original = deepcopy(notes)
    result = refine_piano_texture(notes, {}, {"tempo_bpm": 120})
    assert notes == original
    assert result["density_before"]["max_right_keys"] == 5
    assert result["density_after"]["target_met"] and result["density_after"]["max_right_keys"] == 3
    assert result["report"]["summary"]["passed_automated_constraints"]
    assert {n["start"] for n in result["notes"]} == {0, .5, 1, 1.5}
    assert [n for n in result["notes"] if n["role"] in {"bass", "melody"}] == [n for n in notes if n["role"] in {"bass", "melody"}]
    patterns = [{n["pitch"] for n in result["notes"] if n["hand"] == "right" and n["start"] == i * .5} for i in range(4)]
    assert all(pattern == patterns[0] for pattern in patterns), "Recurring figure must receive coherent voicing decisions"
    source = {n["id"]: n for n in notes}
    assert all(n == source[n["id"]] for n in result["notes"]), "Omission-only repair must not touch retained note metadata or timing"
    assert all(source[c["note_id"]]["role"] == "harmony" for c in result["changes"] if c["action"] == "omit_optional_inner_voice")


def test_density_target_reports_protected_exceptions_instead_of_discarding_manual_or_musical_lines():
    from backend.app.piano import refine_piano_texture
    roles = ("bass", "riff", "countermelody", "melody")
    notes = [event(f"protected-density-{pitch}", pitch, hand="right", role=role, locked=True)
             for pitch, role in zip((60, 62, 64, 67), roles)]
    result = refine_piano_texture(notes)
    assert result["notes"] == notes and not result["changes"]
    assert result["report"]["summary"]["hard_constraints_passed"]
    assert not result["density_after"]["target_met"]
    assert result["density_after"]["protected_exceptions"][0]["protected_keys"] == 4
    assert result["density_after"]["protected_exceptions"][0]["note_ids"] == sorted(n["id"] for n in notes)


def test_density_articulation_keeps_unique_earlier_attack_and_never_invents_pedal():
    from backend.app.piano import refine_piano_texture
    notes = [event("single-held-inner", 60, 0, 1, hand="right", role="harmony")]
    notes += [event(f"protected-arrival-{pitch}", pitch, .5, 1, hand="right", role="melody", locked=True) for pitch in (62, 64, 67)]
    result = refine_piano_texture(notes, {}, {"tempo_bpm": 120})
    assert len(result["notes"]) == len(notes) and result["density_after"]["target_met"]
    held = next(n for n in result["notes"] if n["id"] == notes[0]["id"])
    assert held["start"] == 0 and held["end"] == .5
    assert held["original_timing"] == notes[0]["original_timing"]
    assert not held.get("pedal_sustained") and held.get("key_release_seconds") is None
    assert result["notes"][1:] == notes[1:]


def test_density_reduction_honors_budget_and_reopening_is_idempotent(monkeypatch):
    import backend.app.piano as piano
    notes = [event(f"budget-density-{pitch}", pitch, hand="right", role="harmony") for pitch in (60, 62, 64, 67)]
    first = piano.refine_piano_texture(notes)
    reopened = piano.refine_piano_texture(first["notes"])
    assert reopened["notes"] == first["notes"] and not reopened["changes"]
    monkeypatch.setattr(piano, "_TEXTURE_AUDIT_LIMIT", 0)
    bounded = piano.refine_piano_texture(notes)
    assert bounded["notes"] == notes and not bounded["density_after"]["target_met"]
    assert bounded["refinement"]["density_budget_exhausted"]


def test_movement_repair_transposes_two_preceding_gestures_as_one_phrase():
    from backend.app.piano import repair_piano_movement
    notes = [event("phrase-prefix", 64, 0, .3, hand="right", role="bass", locked=True)]
    for label, at, bass, inner in (("first", .3, 54, ()), ("upper", .6, 63, (66,)),
                                    ("turn", .9, 60, ()), ("arrival", 1.2, 44, (51,))):
        notes.append(inferred(f"{label}-root", bass, at, at + .3, "left"))
        notes += [event(f"{label}-inner-{p}", p, at, at + .3, hand="left", role="harmony") for p in inner]
        if label in {"first", "turn"}:
            notes += [event(f"{label}-right-{p}", p, at, at + .3, hand="right", role="harmony") for p in (63, 66)]
        if label == "arrival":
            notes += [event(f"arrival-right-{p}", p, at, at + .3, hand="right", role="harmony") for p in (54, 56)]
    settings = {"tempo_bpm": 100, "quantization": "eighth"}
    assert codes(assess_playability(notes, {}, settings)) == {"rapid_jump"}
    result = repair_piano_movement(notes, {}, settings)
    assert result["report"]["summary"]["passed_automated_constraints"]
    source = {n["id"]: n for n in notes}
    changed = [n for n in result["notes"] if n["pitch"] != source[n["id"]]["pitch"]]
    assert len(changed) == 3
    assert {n["start"] for n in changed} == {.6, .9}
    assert all(n["pitch"] == source[n["id"]]["pitch"] - 12 for n in changed)
    assert all((n["start"], n["end"], n["hand"]) == (source[n["id"]]["start"], source[n["id"]]["end"], source[n["id"]]["hand"]) for n in result["notes"])


def test_octave_root_inner_voice_exchange_carries_repeated_bass_without_changing_timing_or_roles():
    from backend.app.piano import repair_piano_movement
    notes = [event("fixed-upper-root", 70, 0, .3, hand="right", role="bass", locked=True),
             event("fixed-lower-root", 44, .9, 1.2, hand="left", role="bass", locked=True),
             event("fixed-return-root", 56, 3, 3.3, hand="right", role="bass", locked=True),
             inferred("exchange-root-a", 39, 3.3, 3.9, "left"),
             inferred("exchange-root-b", 39, 3.9, 4.5, "left"),
             event("exchange-inner", 51, 3.3, 3.9, hand="right", role="harmony"),
             event("support-a", 46, 3.3, 3.9, hand="left", role="harmony", locked=True),
             event("support-b", 46, 3.9, 4.5, hand="left", role="harmony", locked=True)]
    settings = {"tempo_bpm": 100, "quantization": "eighth"}
    assert codes(assess_playability(notes, {}, settings)) == {"line_reassignment"}
    result = repair_piano_movement(notes, {}, settings)
    assert result["report"]["summary"]["passed_automated_constraints"]
    before = {n["id"]: n for n in notes}
    after = {n["id"]: n for n in result["notes"]}
    for n in notes:
        repaired = after[n["id"]]
        assert (repaired["start"], repaired["end"], repaired["role"], repaired["pitch"] % 12) == (n["start"], n["end"], n["role"], n["pitch"] % 12)
        if n.get("locked"):
            assert repaired == n
    assert all(after[n["id"]]["pitch"] == 51 and after[n["id"]]["hand"] == "right" for n in notes[3:5])
    assert after[notes[5]["id"]]["pitch"] == 39 and after[notes[5]["id"]]["hand"] == "left"
    assert sorted(n["pitch"] for n in notes if n["start"] == 3.3) == sorted(n["pitch"] for n in result["notes"] if n["start"] == 3.3), "The first exchanged chord retains its exact sounding keys"
    assert len(after) == len(before) and result["report"]["density"]["target_met"]


@pytest.mark.parametrize("pickup,downbeat", [("0", .017627), ("1/2", .317627)])
def test_canonical_half_grid_release_with_nonzero_origin_cannot_hide_held_key_conflict(pickup, downbeat):
    from fractions import Fraction
    from backend.app.piano import _timeline, refine_piano_texture
    from backend.app.score import ScoreSettings, build_score, fraction
    # The decimal release lies exactly halfway between eighth-grid boundaries.
    # Binary subtraction used to round it down, concealing the longer tied hold.
    notes = [event("rational-held-low", 59, 131.717627, 132.467627, hand="right", role="harmony"),
             event("rational-held-high", 67, 131.717627, 132.467627, hand="right", role="harmony"),
             event("rational-lead", 77, 132.467627, 132.917627, hand="right", role="melody", locked=True)]
    settings = ScoreSettings(tempo_bpm=100, first_downbeat_seconds=downbeat, pickup_beats=pickup,
                             quantization="eighth", melody_quantization="sixteenth", readability_mode="accompaniment",
                             piano_split=True, transpose_semitones=2)
    original = deepcopy(notes)
    canonical = build_score(notes, settings, title="Generated rational-boundary regression")
    bounds, pitches = {}, {}
    for part in canonical["parts"]:
        for staff in part["staves"]:
            for measure in staff["measures"]:
                for voice in measure["voices"]:
                    for item in voice["events"]:
                        if item["kind"] == "rest":
                            continue
                        start = fraction(measure["start"]) + fraction(item["offset"])
                        end = start + fraction(item["duration"])
                        for identifier in item["source_note_ids"]:
                            old = bounds.get(identifier, (start, end))
                            bounds[identifier] = (min(old[0], start), max(old[1], end))
                        for identifier, pitch in item.get("source_pitches", {}).items():
                            pitches[identifier] = pitch
    assert bounds[notes[0]["id"]][1] == Fraction(221)
    lead_attack = bounds[notes[2]["id"]][0]
    assert lead_attack == Fraction(883, 4)
    assert all(bounds[n["id"]][0] <= lead_attack < bounds[n["id"]][1] for n in notes)
    timeline, _ = _timeline(notes, settings)
    for n in timeline:
        onset, release = bounds[n["id"]]
        assert n["_start"] == float(onset * Fraction(3, 5))
        assert n["_release"] == float(release * Fraction(3, 5))
    report = assess_playability(notes, {}, settings)
    conflict = next(i for i in report["issues"] if i["code"] == "held_note_conflict")
    assert conflict["span_semitones"] == 18
    assert report["density"]["max_right_keys"] == 3, "A reachable key count must not hide an impossible span"
    assert notes == original
    repaired = refine_piano_texture(notes, {}, settings)
    assert repaired["report"]["summary"]["passed_automated_constraints"]
    assert len(repaired["notes"]) == 3, "Explicit secondary releases preserve the attacks instead of erasing this figure"
    assert next(n for n in repaired["notes"] if n["id"] == notes[2]["id"]) == notes[2]
    assert all(n["original_timing"] == original[index]["original_timing"] for index, n in enumerate(repaired["notes"]))
    repaired_score = build_score(repaired["notes"], settings, title="Repaired rational-boundary regression")
    assert repaired_score["source_notes"][0]["end"] < notes[0]["end"]

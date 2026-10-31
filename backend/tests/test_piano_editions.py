"""Known event fixtures for editions, not fabricated model inference."""
from copy import deepcopy

import pytest

from backend.app.piano_editions import ENGINE_VERSION, prepare_edition
from backend.tests.test_piano import event

SCORE = {"tempo_bpm": 120, "quantization": "eighth", "piano_split": True, "readability_mode": "accompaniment"}


def melody_fixture():
    return [event(f"vocal-{i}", pitch, i * .5, i * .5 + .4,
                  source_note_ids=[f"raw-vocal-{i}"], part_id="vocal-source") for i, pitch in enumerate((72, 74, 76, 72))]


def instrumental_fixture():
    return [event(f"instrumental-{t}-{pitch}", pitch, t, t + .4, part_id="other-source",
                  source_note_ids=[f"raw-instrumental-{t}-{pitch}"])
            for t in (0, .5, 1, 1.5) for pitch in (36, 48, 55, 60, 64, 67, 72, 76)]


def test_faithful_preserves_every_detected_performance_even_when_not_playable():
    source = instrumental_fixture()
    source[0].update(hand="right", locked=True, voice_id="2", custom_edit={"keep": "exact"})
    original = deepcopy(source)
    result = prepare_edition(source, melody_fixture(), "faithful", {}, SCORE)
    assert result["engine"] == ENGINE_VERSION
    assert result["edition_mode"] == "faithful" and not result["arrangement"]
    assert result["preserved_performance"] and result["preserve_timing"]
    assert source == original
    assert len(result["notes"]) == len(source)
    for old, new in zip(source, result["notes"]):
        for key in ("id", "pitch", "start", "end", "velocity", "voice_id", "part_id", "provenance", "locked", "source_note_ids", "original_timing", "custom_edit"):
            assert new.get(key) == old.get(key)
    assert result["notes"][0]["hand"] == "right"
    assert {n["edition_source"] for n in result["notes"]} == {"instrumental"}
    assert all(change["action"] == "display_hand" for change in result["changes"])
    assert any("does not promise piano playability" in item for item in result["limitations"])


def test_singer_accompaniment_keeps_vocal_cue_separate_and_optional():
    source, vocal = instrumental_fixture(), melody_fixture()
    without = prepare_edition(source, vocal, "accompaniment", {}, SCORE)
    with_cue = prepare_edition(source, vocal, "accompaniment", {"include_vocal_cue": True}, SCORE)
    assert not any(n.get("edition_source") == "vocal" for n in without["notes"])
    cues = [n for n in with_cue["notes"] if n.get("role") == "vocal_cue"]
    assert len(cues) == len(vocal) and all(n["hand"] is None for n in cues)
    assert with_cue["report"]["summary"]["vocal_cue_notes_excluded_from_hand_checks"] == len(vocal)
    assert [(n["pitch"], n["start"], n["end"]) for n in cues] == [(n["pitch"], n["start"], n["end"]) for n in vocal]
    missing = prepare_edition(source, [], "accompaniment", {"include_vocal_cue": True}, SCORE)
    assert any("no cue was invented" in text for text in missing["limitations"])


def test_solo_carries_real_melody_and_reduces_competing_instrumental_events():
    source, vocal = instrumental_fixture(), melody_fixture()
    source_before, vocal_before = deepcopy(source), deepcopy(vocal)
    solo = prepare_edition(source, vocal, "solo_piano", {}, SCORE)
    assert source == source_before and vocal == vocal_before
    lead = [n for n in solo["notes"] if n.get("role") == "melody"]
    assert len(lead) == len(vocal)
    assert [(n["pitch"], n["start"], n["end"], n["velocity"]) for n in lead] == [(n["pitch"], n["start"], n["end"], n["velocity"]) for n in vocal]
    assert all(n["hand"] == "right" and n["edition_role"] == "lead_melody" for n in lead)
    assert all(not n.get("locked") for n in lead), "Algorithmic protection must not become an unrequested user lock"
    assert len(solo["notes"]) < len(source) + len(vocal), "Do not concatenate two dense parts"
    retained_bass = [n for n in solo["notes"] if n.get("role") == "bass"]
    assert [n["pitch"] for n in retained_bass] == [36] * 4
    assert [n["start"] for n in retained_bass] == [0, .5, 1, 1.5]
    lookup = {n["id"]: n for n in source + vocal}
    for n in solo["notes"]:
        raw = lookup[n["source_note_id"]]
        assert n["pitch"] % 12 == raw["pitch"] % 12
        assert n["start"] == raw["start"]
        assert n["original_timing"] == raw["original_timing"]
        assert n["source_note_ids"] == raw["source_note_ids"]
    assert any(c["action"].startswith("omit") for c in solo["changes"])
    assert solo["report"]["summary"]["hard_constraints_passed"]


def test_namespaced_vocal_ids_are_stable_across_editions_without_overwriting_raw_ids():
    source, vocal = instrumental_fixture()[:1], melody_fixture()[:1]
    vocal[0]["id"] = source[0]["id"]  # independent sources may use identical IDs
    cue = prepare_edition(source, vocal, "accompaniment", {"include_vocal_cue": True}, SCORE)
    solo = prepare_edition(source, vocal, "solo_piano", {}, SCORE)
    cue_note = next(n for n in cue["notes"] if n["edition_source"] == "vocal")
    solo_note = next(n for n in solo["notes"] if n["edition_source"] == "vocal")
    assert cue_note["id"] == solo_note["id"] != source[0]["id"]
    assert cue_note["source_note_id"] == source[0]["id"]
    assert cue_note["source_note_ids"] == solo_note["source_note_ids"] == ["raw-vocal-0"]
    assert len({n["id"] for n in solo["notes"]}) == len(solo["notes"])


def test_overlapping_vocal_candidates_are_audited_but_locked_notes_are_never_dropped():
    vocal = [event("lead-low-confidence", 48, 0, 2, velocity=20),
             event("lead-1", 72, 0, .4), event("lead-2", 74, .5, .9),
             event("lead-locked", 76, 1, 1.4, provenance="edited", locked=None, fingering=3)]
    result = prepare_edition([], vocal, "solo_piano", {}, SCORE)
    assert result["melody_selection"]["omitted_candidates"] == 1
    assert result["melody_selection"]["retained_notes"] == 3
    locked = next(n for n in result["notes"] if n["source_note_id"] == vocal[-1]["id"])
    assert locked["pitch"] == 76 and locked["start"] == 1 and locked["end"] == 1.4
    assert locked["locked"] is None and locked["provenance"] == "edited" and locked["fingering"] == 3
    assert locked["id"] in result["locked_note_ids"]
    assert any(c["action"] == "omit_vocal_candidate" for c in result["changes"])
    assert any("heuristic lead-line selection" in text for text in result["limitations"])


def test_clean_melody_large_leaps_and_original_timing_are_not_silently_recomposed():
    vocal = [event("low", 24, 0, .2), event("high", 96, .25, .45), event("return", 24, .5, .7)]
    result = prepare_edition([], vocal, "solo_piano", {}, SCORE)
    assert result["melody_selection"]["omitted_candidates"] == 0
    assert [(n["pitch"], n["start"], n["end"]) for n in result["notes"]] == [(n["pitch"], n["start"], n["end"]) for n in vocal]
    assert result["report"]["summary"]["requires_review"] or len({n["hand"] for n in result["notes"]}) == 2, "A hand transfer may resolve the leap; never rewrite its pitches"


def test_locked_source_edits_survive_even_when_the_protected_melody_conflicts():
    source = [event("locked-source", 40, 0, 2, hand="right", role="riff", locked=True,
                    voice_id="2", custom_edit={"pedal": "manual"})]
    vocal = [event("vocal", 76, 0, 1)]
    result = prepare_edition(source, vocal, "solo_piano", {}, SCORE)
    retained = next(n for n in result["notes"] if n["edition_source"] == "instrumental")
    for key, value in source[0].items():
        assert retained[key] == value
    assert not result["report"]["summary"]["hard_constraints_passed"]
    assert len(result["notes"]) == 2


def test_edition_errors_do_not_invent_missing_melody_or_accept_unbounded_input():
    with pytest.raises(ValueError, match="separately transcribed"):
        prepare_edition(instrumental_fixture(), [], "solo_piano", {}, SCORE)
    with pytest.raises(ValueError, match="Choose faithful"):
        prepare_edition([], [], "generic_chords", {}, SCORE)
    with pytest.raises(ValueError, match="10,000"):
        prepare_edition([event("too-many", 60)] * 10001, [], "faithful", {}, SCORE)


def test_inferred_bass_doubling_is_revoiced_below_lead_with_attack_and_root_retained():
    source = [event("bass-doubling", 53, 0, .5)]
    vocal = [event("same-sung-pitch", 53, 0, .5)]
    result = prepare_edition(source, vocal, "solo_piano", {}, SCORE)
    bass = next(n for n in result["notes"] if n["edition_source"] == "instrumental")
    lead = next(n for n in result["notes"] if n["edition_source"] == "vocal")
    assert (bass["pitch"], bass["start"], bass["end"]) == (41, 0, .5)
    assert (lead["pitch"], lead["start"], lead["end"]) == (53, 0, .5)
    assert bass["original_timing"] == source[0]["original_timing"]
    assert any(e.get("before") == 53 and e.get("after") == 41
               for change in result["changes"] for e in change.get("edits", []))
    assert result["report"]["summary"]["passed_automated_constraints"]
    explicit = prepare_edition([{**source[0], "role": "bass"}], vocal, "solo_piano", {}, SCORE)
    preserved = next(n for n in explicit["notes"] if n["edition_source"] == "instrumental")
    assert preserved["pitch"] == 53, "A user's explicitly marked bass register is not silently rewritten"
    assert any(i["code"] == "duplicate_attack" for i in explicit["report"]["issues"])


def test_vocal_sixteenth_grid_is_used_by_the_same_hand_audit_without_changing_performance():
    from backend.app.piano import assess_playability
    from backend.app.score import build_score, score_to_musicxml
    from music21 import converter
    vocal = [event("quick-sung-low", 53, .16, .24, hand="right", role="melody"),
             event("quick-sung-high", 63, .28, .4, hand="right", role="melody")]
    settings = {**SCORE, "tempo_bpm": 100}
    assert any(i["code"] == "hand_reach" for i in assess_playability(vocal, {}, settings)["issues"])
    settings["melody_quantization"] = "sixteenth"
    assert assess_playability(vocal, {}, settings)["summary"]["passed_automated_constraints"]
    score = build_score(vocal, settings, title="Synthetic vocal-grid fixture")
    reopened = converter.parseData(score_to_musicxml(score), format="musicxml")
    pitches = [n.pitch.midi for n in reopened.recurse().notes]
    assert pitches == [53, 63]
    assert [(n["start"], n["end"]) for n in vocal] == [(.16, .24), (.28, .4)]


def test_secondary_conflict_reduction_handles_two_note_span_without_deleting_melody_or_bass():
    from backend.app.piano import PianoSettings, assess_playability
    from backend.app.piano_editions import _remove_secondary_conflicts
    from backend.app.score import ScoreSettings
    source = [event(f"held-inner-{pitch}", pitch, 0, 1, hand="right", role="harmony") for pitch in (53, 55, 59, 62)]
    source += [event("protected-lead", 65, .5, 1, hand="right", role="melody"),
               event("protected-bass", 36, 0, 1, hand="left", role="bass")]
    settings = ScoreSettings(**SCORE)
    assert not assess_playability(source, {}, settings)["summary"]["hard_constraints_passed"]
    result, changes, evaluations = _remove_secondary_conflicts(source, PianoSettings(), settings)
    assert assess_playability(result, {}, settings)["summary"]["hard_constraints_passed"]
    assert {n["id"] for n in source[-2:]} <= {n["id"] for n in result}
    assert all(n in source for n in result)
    assert 1 < len(changes) <= 24 and evaluations <= 96
    assert all(next(n for n in source if n["id"] == c["note_id"])["role"] == "harmony" for c in changes)


def test_final_intro_transfer_preserves_vocal_pitch_and_timing_and_short_fragment_guard():
    from backend.app.piano import PianoSettings, assess_playability
    from backend.app.piano_editions import _finish_edition_motion
    from backend.app.score import ScoreSettings
    settings = ScoreSettings(**SCORE)
    notes = [event("intro-inner", 58, 0, .25, hand="left", role="harmony", locked=True),
             event("intro-vocal", 47, .5, .75, hand="right", role="melody")]
    assert any(i["code"] == "crossed_positions" for i in assess_playability(notes, {}, settings)["issues"])
    result, changes, audits = _finish_edition_motion(notes, PianoSettings(), settings)
    assert assess_playability(result, {}, settings)["summary"]["passed_automated_constraints"]
    assert [(n["pitch"], n["start"], n["end"]) for n in result] == [(n["pitch"], n["start"], n["end"]) for n in notes]
    assert result[-1]["hand"] == "left" and changes and audits <= 32
    # Deleting a short inner fragment can expose an even larger subsequent leap.
    guarded = [event("upper-prior", 76, 0, .25, hand="right", role="harmony"),
               event("short-inner", 60, .25, .27, hand="right", role="harmony"),
               event("following-lead", 51, .5, .7, hand="right", role="melody")]
    before = assess_playability(guarded, {}, settings)
    output, edits, _ = _finish_edition_motion(guarded, PianoSettings(), settings)
    assert {n["id"] for n in output} == {n["id"] for n in guarded}, "A short fragment is not blindly dropped into a larger vocal leap"
    assert not edits and assess_playability(output, {}, settings) == before


def test_explicit_solo_melody_octave_retains_identity_raw_pitch_and_rhythm():
    source = [event("register-source", 36, 0, 2, hand="left", role="bass", locked=True)]
    vocal = [event("register-vocal-a", 53, 0, .4), event("register-vocal-b", 55, .5, .9)]
    original = deepcopy(vocal)
    normal = prepare_edition(source, vocal, "solo_piano", {}, SCORE)
    upper = prepare_edition(source, vocal, "solo_piano", {}, SCORE, melody_octave_shift=12)
    lower = prepare_edition(source, vocal, "solo_piano", {}, SCORE, melody_octave_shift=-12)
    assert vocal == original
    normal_lead = [n for n in normal["notes"] if n.get("role") == "melody"]
    for result, shift in ((upper, 12), (lower, -12)):
        lead = [n for n in result["notes"] if n.get("role") == "melody"]
        assert [n["id"] for n in lead] == [n["id"] for n in normal_lead]
        assert result["melody_octave_shift"] == shift
        for raw, arranged in zip(vocal, lead):
            assert arranged["pitch"] == raw["pitch"] + shift
            assert arranged["source_note_id"] == raw["id"]
            assert arranged["source_note_ids"] == raw["source_note_ids"]
            assert arranged["original_timing"] == raw["original_timing"]
            assert (arranged["start"], arranged["end"], arranged["velocity"]) == (raw["start"], raw["end"], raw["velocity"])
            assert arranged["provenance"] == "edited" and arranged["locked"] is False
            assert arranged["arrangement_provenance"]["melody_octave_shift"] == shift
        retained = next(n for n in result["notes"] if n["edition_source"] == "instrumental")
        assert all(retained[key] == value for key, value in source[0].items())
        assert any("not diagnosed as an octave error" in edit.get("reason", "") for c in result["changes"] for edit in c.get("edits", []))
    assert normal["melody_octave_shift"] == 0


def test_melody_register_never_silently_transposes_locked_manual_edits():
    for metadata in ({"locked": True}, {"provenance": "edited", "locked": None}, {"provenance": "manually_added"}):
        vocal = [event("locked-register", 53, **metadata)]
        with pytest.raises(ValueError, match="locked or manual vocal edits"):
            prepare_edition([], vocal, "solo_piano", {}, SCORE, melody_octave_shift=12)
        assert prepare_edition([], vocal, "solo_piano", {}, SCORE)["notes"][0]["pitch"] == 53
    unlocked = [event("explicitly-unlocked", 53, provenance="edited", locked=False)]
    assert prepare_edition([], unlocked, "solo_piano", {}, SCORE, melody_octave_shift=12)["notes"][0]["pitch"] == 65


def test_melody_register_rejects_invalid_modes_and_midi_overflow():
    for shift in (1, 24, True, "12"):
        with pytest.raises(ValueError, match="-12, 0, or 12"):
            prepare_edition([], melody_fixture(), "solo_piano", {}, SCORE, melody_octave_shift=shift)
    with pytest.raises(ValueError, match="solo piano arrangement choice"):
        prepare_edition([], melody_fixture(), "accompaniment", {}, SCORE, melody_octave_shift=12)
    with pytest.raises(ValueError, match="MIDI pitch range"):
        prepare_edition([], [event("overflow", 120)], "solo_piano", {}, SCORE, melody_octave_shift=12)

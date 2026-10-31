"""Distinct, reversible piano editions built only from supplied detected events.

A faithful transcription is not automatically a playable piano arrangement. The
other editions are explicitly editorial arrangements: source events, omissions,
register changes and hand assignments remain auditable. No harmony is generated.
"""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass

from .piano import (ENGINE_VERSION as PIANO_ENGINE, PROTECTED_ROLES,
                    _locked, _settings, _timeline, arrange_piano, assess_playability, repair_piano_movement, refine_piano_texture)

ENGINE_VERSION = "scorecraft-piano-editions-v3+" + PIANO_ENGINE
EDITION_MODES = {"faithful", "accompaniment", "solo_piano"}
_VOCAL_NAMESPACE = uuid.UUID("3b473237-fb86-53d8-a689-53d17f024f36")
_EDITION_LIMIT = 10000
_SECONDARY_AUDIT_LIMIT = 512
_SECONDARY_OMISSION_LIMIT = 32
_LIMITATIONS = [
    "Transcription estimates performed notes; the piano editions are authored arrangements, not newly detected piano performances.",
    "Automated hand constraints and phrase heuristics do not establish idiomatic writing or pianist approval.",
    "No harmony or accompaniment is invented: every retained event comes from the supplied instrumental or vocal transcription.",
    "Model errors, leakage and missing notes remain possible; listen against the source and review each edition.",
]


def _tag_source(notes, source, edition_role):
    result = copy.deepcopy(notes)
    for note in result:
        note.setdefault("source_note_id", str(note["id"]))
        note["edition_source"] = source
        note["edition_role"] = edition_role
        note.setdefault("original_timing", {key: note[key] for key in ("start", "end", "pitch")})
        note.setdefault("source_note_ids", [str(note["id"])])
    return result


def _vocal_events(notes, cue=False):
    events = _tag_source(notes, "vocal", "vocal_cue" if cue else "lead_melody")
    for note in events:
        # The same vocal event retains its edition ID between cue and solo modes,
        # while being distinct from an instrumental event with an identical ID.
        note["id"] = str(uuid.uuid5(_VOCAL_NAMESPACE, str(note["id"])))
        note["part_id"] = "detected-vocal-melody"
        note["role"] = "vocal_cue" if cue else "melody"
        note["hand"] = None if cue else note.get("hand") or "right"
    return events


@dataclass(frozen=True)
class _MelodyPath:
    value: float
    end: float
    pitch: int | None
    parent: "_MelodyPath | None" = None
    note: dict | None = None


def _select_melody(events):
    """An eight-path continuity search only when vocal candidates overlap.

    A clean monophonic transcription passes through exactly. Conflicting unlocked
    polyphonic detections are selected as a lead-line hypothesis, never relabeled
    as confirmed sung melody. Explicit locked/manual notes always survive.
    """
    events = sorted(events, key=lambda n: (n["start"], n["pitch"], n["id"]))
    if all(previous["end"] <= following["start"] + .04 for previous, following in zip(events, events[1:])):
        return events, []
    locked = [n for n in events if _locked(n)]
    optional = [n for n in events if not _locked(n)]
    conflict_ids = set()
    for n in optional:
        if any(min(n["end"], fixed["end"]) - max(n["start"], fixed["start"]) > .04 for fixed in locked):
            conflict_ids.add(n["id"])
    optional = [n for n in optional if n["id"] not in conflict_ids]
    states = [_MelodyPath(0., -math.inf, None)]
    for note in optional:
        expanded = list(states)
        for state in states:
            if note["start"] < state.end - .04:
                continue
            duration = float(note["end"] - note["start"])
            reward = 2 + min(duration, .75) * 2 + (note.get("velocity") or 64) / 127
            # The supplied model score is deliberately not treated as accuracy.
            gap = note["start"] - state.end
            transition = 0. if state.pitch is None or gap > 2 else abs(note["pitch"] - state.pitch) * .045
            expanded.append(_MelodyPath(state.value + reward - transition, note["end"], note["pitch"], state, note))
        expanded.sort(key=lambda path: (-path.value, path.end, path.pitch or -1, path.note["id"] if path.note else ""))
        unique = {}
        for path in expanded:
            key = (path.end, path.pitch)
            if key not in unique:
                unique[key] = path
            if len(unique) == 8:
                break
        states = list(unique.values())
    winner = max(states, key=lambda path: path.value)
    retained = {n["id"] for n in locked}
    while winner.note is not None:
        retained.add(winner.note["id"])
        winner = winner.parent
    chosen = [n for n in events if n["id"] in retained]
    omitted = [n for n in events if n["id"] not in retained]
    return chosen, omitted


def _prioritize_instrumental(notes, score):
    """Assign only missing editorial roles while considering already-held bass.

    Register and continuity identify a bass candidate. A new upper attack during
    a held low bass is harmony, not another bass merely because it is the lowest
    newly attacked key. Explicit user roles and all locked events are preserved.
    """
    timeline, _ = _timeline(notes, score)
    by_id = {n["id"]: n for n in notes}
    pitches = sorted(n["pitch"] for n in timeline)
    boundary = min(60, pitches[max(0, len(pitches) // 3 - 1)]) if pitches else 48
    active_bass = []
    moments = {}
    for n in timeline:
        moments.setdefault(n["_start"], []).append(n)
    for moment, group in moments.items():
        active_bass = [n for n in active_bass if n["_release"] > moment + 1e-8]
        existing = [n for n in group if n.get("role") == "bass"]
        candidates = [n for n in group if not n.get("role") and not n["_locked"] and n["pitch"] <= boundary]
        lowest = min(candidates, key=lambda n: (n["pitch"], -(n.get("velocity") or 64), n["id"])) if candidates else None
        if lowest and active_bass and lowest["pitch"] > min(n["pitch"] for n in active_bass) + 7:
            lowest = None
        for n in group:
            original = by_id[n["id"]]
            if original.get("role") or n["_locked"]:
                continue
            role = "bass" if n is lowest and not existing else "harmony"
            original["role"] = role
            previous = original.get("arrangement_provenance") or {}
            original["arrangement_provenance"] = {**previous, "inferred_role": role,
                "role_assumption": "Editorial lower-register/held-bass continuity heuristic, not detected instrumentation"}
            if role == "bass":
                active_bass.append(n)
        active_bass.extend(existing)
    return notes



def _clear_lead_register(source, melody, score):
    """Revoice detected material beneath the protected lead before allocation.

    This is an explicit editorial change, not a correction to model detections.
    Inferred bass roots retain identity/attack; marked riffs, bass and locked
    notes are never moved here. No substitute chord or accompaniment is added.
    """
    instrumental, _ = _timeline(source, score)
    lead, _ = _timeline(melody, score)
    by_id = {n["id"]: n for n in source}
    changes, omitted = [], set()
    for event in instrumental:
        if event["_locked"] or (event["_role"] in PROTECTED_ROLES and not
                (event["_role"] == "bass" and event.get("_inferred_role"))):
            continue
        overlapping = [n for n in lead if min(n["_release"], event["_release"]) > max(n["_start"], event["_start"]) + 1e-8]
        if not overlapping:
            continue
        ceiling = min(n["_pitch"] for n in overlapping) - (5 if event["_role"] == "bass" else 2)
        shift = 0
        while event["_pitch"] + shift > ceiling and shift > -24:
            shift -= 12
        if event["_pitch"] + shift < 21 or event["pitch"] + shift < 0:
            continue  # preserve low roots and expose any real remaining conflict
        if event["_pitch"] + shift > ceiling and event["_role"] not in PROTECTED_ROLES:
            omitted.add(event["id"])
            changes.append({"note_id": event["id"], "action": "omit_secondary_register",
                            "reason": "Secondary instrumental texture remains above the protected vocal lead after two octave alternatives; retain its source for review"})
            continue
        if shift:
            note = by_id[event["id"]]
            edit = {"field": "pitch", "before": note["pitch"], "after": note["pitch"] + shift,
                    "reason": "Place detected accompaniment below the sung lead; octave revoicing preserves root/pitch class and attack"}
            previous = note.get("arrangement_provenance") or {}
            note["pitch"] += shift
            note["arrangement_provenance"] = {**previous, "kind": "derived_accompaniment",
                                              "changes": list(previous.get("changes", [])) + [edit]}
            note["provenance"], note["locked"] = "edited", False
            changes.append({"note_id": event["id"], "action": "modify", "edits": [edit]})
    return [n for n in source if n["id"] not in omitted], changes


def _remove_secondary_conflicts(notes, piano, score, *, include_movement=False,
                                max_audits=_SECONDARY_AUDIT_LIMIT, max_omissions=_SECONDARY_OMISSION_LIMIT):
    """Resolve coarse-grid holds competing with the finer protected vocal line.

    Shared by two passes, at most 32 implicated secondary events may be omitted
    with 512 full-score audits TOTAL. Evaluate the first repairable passage's
    candidates rather than rescanning every later conflict on every iteration.
    Every removal must improve the unchanged assessment; bass, riffs, melody,
    manual-fingered and locked notes remain ineligible.
    """
    result, changes, evaluations = notes, [], 0
    report = assess_playability(result, piano, score)
    def cost(item):
        severity = sum(max(0., issue.get("span_semitones", 0) - issue.get("reach_limit", 0)) +
                       max(0., issue.get("required_translation_semitones", 0) - issue.get("allowed_semitones", 0))
                       for issue in item["issues"])
        return (item["summary"]["error_count"], item["summary"]["warning_count"], round(severity, 5))
    while len(changes) < max_omissions and evaluations < max_audits:
        eligible = {n["id"]: n for n in result if n.get("role") in {"harmony", "texture"}
                    and not _locked(n) and n.get("fingering") is None}
        issues = sorted((i for i in report["issues"] if i["severity"] == "error" or
                         i["code"] in ({"duplicate_attack", "held_key_reattack", "rapid_jump"} if include_movement else
                                       {"duplicate_attack", "held_key_reattack"})),
                        key=lambda i: (i["severity"] != "error", i["measure"], i["code"]))
        best, visited = None, set()
        for issue in issues:
            candidates = [eligible[nid] for nid in issue["note_ids"] if nid in eligible and nid not in visited]
            for note in candidates:
                if evaluations >= max_audits:
                    break
                visited.add(note["id"])
                remaining = [n for n in result if n["id"] != note["id"]]
                checked = assess_playability(remaining, piano, score)
                evaluations += 1
                if cost(checked) >= cost(report):
                    continue
                rank = (cost(checked), note["end"] - note["start"], note.get("velocity") or 64, note["id"])
                if best is None or rank < best[0]:
                    best = (rank, note, remaining, checked)
            if best is not None or evaluations >= max_audits:
                break
        if best is None:
            break
        _, note, result, report = best
        changes.append({"note_id": note["id"], "action": "omit_competing_texture",
                        "reason": "Remove only implicated secondary instrumental texture after hand/register alternatives: it conflicts with the protected sung line, duplicates a held key, or requires an avoidable rapid jump"})
    return result, changes, evaluations


def _finish_edition_motion(notes, piano, score):
    """One final bounded audit of crossings and shortened secondary fragments.

    An omission is rejected if it makes the next vocal attack harder. A hand
    transfer never alters the protected melody pitch or performance timing.
    """
    result, changes, evaluations = notes, [], 0
    report = assess_playability(result, piano, score)
    def cost(item):
        excess = sum(max(0., issue.get("required_translation_semitones", 0) - issue.get("allowed_semitones", 0))
                     for issue in item["issues"])
        return (item["summary"]["error_count"], item["summary"]["warning_count"], round(excess, 5))
    for issue in list(report["issues"]):
        if evaluations >= 32 or issue["code"] not in {"crossed_positions", "hand_crossing", "rapid_jump"}:
            continue
        candidates = [n for n in result if n["id"] in issue["note_ids"] and not _locked(n) and n.get("fingering") is None]
        best = None
        for note in candidates:
            if evaluations >= 32:
                break
            omit = issue["code"] == "rapid_jump"
            if omit and (note.get("role") not in {"harmony", "texture"} or note["end"] - note["start"] > .05):
                continue
            hand = "right" if note.get("hand") == "left" else "left"
            candidate = ([n for n in result if n["id"] != note["id"]] if omit else
                         [{**n, "hand": hand} if n["id"] == note["id"] else n for n in result])
            checked = assess_playability(candidate, piano, score)
            evaluations += 1
            if cost(checked) >= cost(report):
                continue
            rank = (cost(checked), note["id"])
            if best is None or rank < best[0]:
                best = (rank, note, omit, candidate, checked, hand)
        if best is None:
            continue
        _, note, omit, result, report, hand = best
        if omit:
            changes.append({"note_id": note["id"], "action": "omit_short_arrangement_fragment",
                            "reason": "Remove a secondary fragment shorter than 50ms after reduction only when the full-score audit confirms the following lead attack also remains playable"})
        else:
            changed = next(n for n in result if n["id"] == note["id"])
            edit = {"field": "hand", "before": note.get("hand"), "after": hand,
                    "reason": "Transfer an unlocked phrase note to avoid crossing; detected pitch and performance timing remain exact"}
            provenance = changed.get("arrangement_provenance") or {}
            changed["arrangement_provenance"] = {**provenance, "kind": "derived_accompaniment", "engine": ENGINE_VERSION,
                                                 "changes": list(provenance.get("changes", [])) + [edit]}
            changed["provenance"], changed["locked"] = "edited", False
            changes.append({"note_id": note["id"], "action": "modify", "edits": [edit]})
    return result, changes, evaluations

def _faithful(source, piano, score):
    before = assess_playability(source, piano, score)
    # Temporarily protect every performance event. The allocator may choose a
    # hand for an unassigned note, but cannot omit, octave-shift or articulate it.
    protected = [dict(note, locked=True) for note in source]
    placed = arrange_piano(protected, piano, score)
    allocation = {n["id"]: n.get("hand") for n in placed["notes"]}
    output, changes = [], []
    for original in source:
        note = copy.deepcopy(original)
        if not _locked(note) or note.get("hand") is None:
            note["hand"] = allocation[note["id"]]
        if note.get("hand") != original.get("hand"):
            changes.append({"note_id": note["id"], "action": "display_hand", "edits": [
                {"field": "hand", "before": original.get("hand"), "after": note["hand"],
                 "reason": "Heuristic display allocation only; all detected pitches and performance times remain exact"}]})
        output.append(note)
    return {"notes": output, "changes": changes, "before_report": before,
            "report": assess_playability(output, piano, score), "settings": piano.model_dump(),
            "arrangement": False, "preserved_performance": True, "preserve_timing": True,
            "locked_note_ids": [n["id"] for n in output if _locked(n)],
            "method": "Exact detected instrumental events with optional heuristic hand placement; score quantization remains separate",
            "limitations": ["Faithful means faithful to this saved transcription, including its errors; it does not promise piano playability."]}


def prepare_edition(source_notes: list[dict], melody_notes: list[dict] | None, edition_mode: str,
                    piano_settings=None, score_settings=None, *, melody_octave_shift: int = 0) -> dict:
    """Return an independent edition; neither source list is modified.

    Caller preserves source project IDs and raw timing and saves a new project.
    `faithful` excludes the vocal source; `accompaniment` adds a cue only when
    explicitly enabled; `solo_piano` requires an actual vocal transcription.
    """
    if edition_mode not in EDITION_MODES:
        raise ValueError("Choose faithful, accompaniment, or solo_piano.")
    if type(melody_octave_shift) is not int or melody_octave_shift not in {-12, 0, 12}:
        raise ValueError("Melody register shift must be -12, 0, or 12 semitones.")
    if melody_octave_shift and edition_mode != "solo_piano":
        raise ValueError("Melody register shifting is an explicit solo piano arrangement choice only.")
    melody_notes = melody_notes or []
    if melody_octave_shift and any(_locked(n) for n in melody_notes):
        raise ValueError("Melody register shifting would change locked or manual vocal edits. Keep the original register or explicitly unlock those notes first.")
    if len(source_notes) + len(melody_notes) > _EDITION_LIMIT:
        raise ValueError("Piano editions support at most 10,000 combined input notes; select a shorter passage.")
    piano, score = _settings(piano_settings, score_settings)
    # Validate each independent source before introducing namespaced IDs.
    _timeline(source_notes, score)
    _timeline(melody_notes, score)
    source = _tag_source(source_notes, "instrumental", "instrumental_transcription" if edition_mode == "faithful" else "accompaniment")
    limitations = list(_LIMITATIONS)
    selection = {"input_notes": len(melody_notes), "retained_notes": 0, "omitted_candidates": 0,
                 "method": "Not used in this edition"}
    if edition_mode == "faithful":
        result = _faithful(source, piano, score)
    elif edition_mode == "accompaniment":
        result = arrange_piano(source, piano, score)
        result["preserved_performance"] = False
        if piano.include_vocal_cue and melody_notes:
            cue = _vocal_events(melody_notes, cue=True)
            result["notes"].extend(cue)
            result["notes"].sort(key=lambda n: (n["start"], n["pitch"], n["id"]))
            result["report"] = assess_playability(result["notes"], piano, score)
            selection.update(retained_notes=len(cue), method="Separate detected vocal cue, excluded from piano hand checks")
        elif piano.include_vocal_cue:
            limitations.append("The vocal cue was requested but no detected vocal notes were supplied; no cue was invented.")
        result["method"] = "Singer accompaniment reduced from detected instrumental events; optional vocal cue stays separate from both piano hands"
    else:
        if not melody_notes:
            raise ValueError("Solo piano requires a separately transcribed vocal melody; no sung melody will be invented.")
        vocal = _vocal_events(melody_notes)
        register_shift_changes = []
        if melody_octave_shift:
            for n in vocal:
                shifted_pitch = n["pitch"] + melody_octave_shift
                if not 0 <= shifted_pitch <= 127:
                    raise ValueError("The requested melody register exceeds MIDI pitch range; choose another register.")
                edit = {"field": "pitch", "before": n["pitch"], "after": shifted_pitch,
                        "reason": "Explicit solo piano melody register choice; original detected vocal pitch is retained in original_timing, not diagnosed as an octave error"}
                n["pitch"] = shifted_pitch
                previous = n.get("arrangement_provenance") or {}
                n["arrangement_provenance"] = {**previous, "kind": "derived_accompaniment", "engine": ENGINE_VERSION,
                                                "melody_octave_shift": melody_octave_shift,
                                                "changes": list(previous.get("changes", [])) + [edit]}
                n["provenance"], n["locked"] = "edited", False
                register_shift_changes.append({"note_id": n["id"], "action": "modify", "edits": [edit]})
            limitations.append(f"The solo piano lead is explicitly placed {melody_octave_shift:+d} semitones from the detected vocal register; the source recording and original pitch remain unchanged.")
        melody, omitted = _select_melody(vocal)
        if not melody:
            raise ValueError("No vocal melody events are available for the solo piano edition.")
        previous_roles = {n["id"]: n.get("role") for n in source}
        source = _prioritize_instrumental(source, score)
        role_changes = [{"note_id": n["id"], "action": "modify", "edits": [{"field": "role",
                         "before": previous_roles[n["id"]], "after": n.get("role"),
                         "reason": "Editorial lower-register and held-bass continuity priority, not detected instrumentation"}]}
                        for n in source if n.get("role") != previous_roles[n["id"]]]
        source, register_changes = _clear_lead_register(source, melody, score)
        source_timeline, _ = _timeline(source, score)
        melody_timeline, _ = _timeline(melody, score)
        melody_attacks = {(n["_start"], n["_pitch"]) for n in melody_timeline}
        doubled = {n["id"] for n in source_timeline if not n["_locked"] and
                   n.get("role") not in PROTECTED_ROLES and (n["_start"], n["_pitch"]) in melody_attacks}
        accompaniment = [n for n in source if n["id"] not in doubled]
        locks = {n["id"]: n.get("locked") for n in melody}
        # A protected, audible lead occupies its hand during the phrase search.
        # The allocator must simplify competing source material around it rather
        # than concatenating two dense scores and calling that an arrangement.
        protected_melody = [{**n, "locked": True} for n in melody]
        result = arrange_piano(accompaniment + protected_melody, piano, score)
        result["changes"] = register_shift_changes + role_changes + register_changes + result["changes"]
        melody_by_id = {n["id"]: n for n in melody}
        for n in result["notes"]:
            if n["id"] in melody_by_id:
                original = melody_by_id[n["id"]]
                # Remove only the temporary algorithmic lock, preserving an
                # explicit user lock or the edited/manual default exactly.
                if "locked" in original:
                    n["locked"] = locks[n["id"]]
                else:
                    n.pop("locked", None)
                n["provenance"] = original.get("provenance", "detected")
                n["edition_role"] = "lead_melody"
        result["notes"], conflict_changes, conflict_evaluations = _remove_secondary_conflicts(result["notes"], piano, score)
        result["changes"].extend(conflict_changes)
        # Once the lead survives reduction, a hand transfer is permitted for an
        # unlocked vocal event. Its exact pitch and raw timing remain protected
        # by the melody role; the fixed assessor decides whether transfer helps.
        refined = repair_piano_movement(result["notes"], piano, score)
        result["notes"] = refined["notes"]
        result["changes"].extend(refined["changes"])
        result["notes"], final_conflicts, final_audits = _remove_secondary_conflicts(
            result["notes"], piano, score, include_movement=True,
            max_audits=max(0, _SECONDARY_AUDIT_LIMIT - conflict_evaluations),
            max_omissions=max(0, _SECONDARY_OMISSION_LIMIT - len(conflict_changes)))
        result["changes"].extend(final_conflicts)
        result["notes"], finish_changes, finish_audits = _finish_edition_motion(result["notes"], piano, score)
        result["changes"].extend(finish_changes)
        result["edition_refinement"] = {"secondary_conflict_audits": conflict_evaluations + final_audits,
                                         "secondary_conflict_omissions": len(conflict_changes) + len(final_conflicts),
                                         "secondary_audit_limit": _SECONDARY_AUDIT_LIMIT,
                                         "secondary_omission_limit": _SECONDARY_OMISSION_LIMIT,
                                         "secondary_budget_exhausted": (conflict_evaluations + final_audits >= _SECONDARY_AUDIT_LIMIT or
                                                                         len(conflict_changes) + len(final_conflicts) >= _SECONDARY_OMISSION_LIMIT),
                                         "final_movement": refined["refinement"],
                                         "finish_audits": finish_audits, "finish_changes": len(finish_changes)}
        result["changes"].extend({"note_id": n["id"], "source_note_id": n["source_note_id"],
            "action": "omit_vocal_candidate", "reason": "Continuity-based lead-line hypothesis rejected an overlapping unlocked vocal candidate"} for n in omitted)
        result["changes"].extend({"note_id": nid, "action": "omit_instrumental_doubling",
            "reason": "Remove an unlocked same-key instrumental attack already carried by the detected vocal melody"} for nid in sorted(doubled))
        result["report"] = assess_playability(result["notes"], piano, score)
        result["preserved_performance"] = False
        result["method"] = "Detected vocal lead protected through a bounded two-hand phrase reduction of actual instrumental bass/harmony; no generated harmony"
        result["locked_note_ids"] = [n["id"] for n in result["notes"] if _locked(n)]
        selection.update(retained_notes=len(melody), omitted_candidates=len(omitted),
                         method="Eight-path pitch-continuity selection for overlapping vocal candidates; clean monophonic events remain exact")
        if omitted:
            limitations.append("Overlapping vocal candidates required heuristic lead-line selection; review the retained melody against the vocal recording.")
        limitations.append("The protected vocal line can leave unresolved hand conflicts with locked source notes; these remain visible for manual correction.")
    if edition_mode != "faithful":
        texture = refine_piano_texture(result["notes"], piano, score)
        result["notes"], result["report"] = texture["notes"], texture["report"]
        result["changes"].extend(texture["changes"])
        result["density_before"], result["density_after"] = texture["density_before"], texture["density_after"]
        result["texture_refinement"] = texture["refinement"]
        result["method"] += "; optional inner voices simplified toward the explicit held-key target while protected lines and attacks remain"
    result.update(engine=ENGINE_VERSION, edition_mode=edition_mode, melody_octave_shift=melody_octave_shift,
                  source_counts={"instrumental": len(source_notes), "vocal": len(melody_notes)}, melody_selection=selection,
                  limitations=list(dict.fromkeys(limitations + result.get("limitations", []))))
    return result

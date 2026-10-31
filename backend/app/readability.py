"""Explicit, reversible rhythmic cleanup for an accompaniment revision.

This pass never infers pitches, pedal, finger numbers, or musical importance.
It only aligns small timing discrepancies, keeping intentional offbeats and
repeated attacks. The original input and its original_timing remain untouched.
"""
from __future__ import annotations

import copy
import math
import statistics
from collections import defaultdict
from fractions import Fraction


def _locked(note: dict) -> bool:
    return bool(note["locked"]) if note.get("locked") is not None else note.get("provenance") in {"edited", "manually_added"}


def prepare_readable_notes(notes: list[dict], score_settings) -> dict:
    from .score import GRIDS, ScoreSettings, estimate_tempo

    settings = score_settings if isinstance(score_settings, ScoreSettings) else ScoreSettings.model_validate(score_settings)
    result = copy.deepcopy(notes)
    tempo = settings.tempo_bpm or estimate_tempo(result)["bpm"]
    grid_seconds = float(GRIDS[settings.quantization]) * 60 / tempo
    tolerance = min(0.04, grid_seconds * 0.20)
    origin = settings.first_downbeat_seconds - float(Fraction(settings.pickup_beats)) * 60 / tempo
    changes = []

    def change(note, field, value, reason):
        before = note.get(field)
        if isinstance(value, float):
            value = round(value, 9)
        if before == value or (isinstance(before, (float, int)) and isinstance(value, (float, int)) and abs(before - value) < 1e-8):
            return
        if field in {"start", "end", "key_release_seconds"}:
            start = value if field == "start" else note["start"]
            end = value if field == "end" else note["end"]
            release = value if field == "key_release_seconds" else note.get("key_release_seconds")
            if start >= end or (release is not None and not start < release <= end):
                return
        note[field] = value
        note["provenance"] = "edited"
        # This is an algorithmic edit in the derived arrangement, not a newly
        # locked user correction. Keep subsequent readability steps reversible.
        note["locked"] = False
        changes.append({"note_ids": [note["id"]], "field": field, "before": before,
                        "after": value, "reason": reason, "pass": "notation_readability"})

    for note in result:
        if _locked(note):
            continue
        for field in ("start", "end", "key_release_seconds"):
            value = note.get(field)
            if value is None:
                continue
            snapped = origin + math.floor((float(value) - origin) / grid_seconds + .5) * grid_seconds
            if snapped < 0 or abs(snapped - value) > tolerance:
                continue
            # Never erase an attack or create a zero-length event. Very short
            # events are handled explicitly by the upstream arrangement pass.
            if field == "start" and snapped >= note["end"] - 1e-7:
                continue
            if field != "start" and snapped <= note["start"] + 1e-7:
                continue
            change(note, field, snapped, "Aligned sub-grid performance jitter; intentional offbeats remain on their selected grid.")
        if note.get("voice_id") is not None:
            change(note, "voice_id", None, "Rebuild automatic accompaniment voices by register and overlap; locked voice assignments remain intact.")

    # Release discrepancies among truly simultaneous attacks cause gratuitous
    # voices. Align only nearby releases; long independent held notes survive.
    groups = defaultdict(list)
    for note in result:
        group = "vocal-cue" if note.get("role") == "vocal_cue" else "piano"
        groups[(group, note.get("hand"), round(note["start"], 7))].append(note)
    for group in groups.values():
        for field in ("end", "key_release_seconds"):
            candidates = [note for note in group if note.get(field) is not None]
            if len(candidates) < 2:
                continue
            values = [float(note[field]) for note in candidates]
            if max(values) - min(values) > tolerance:
                continue
            locked = [float(note[field]) for note in candidates if _locked(note)]
            target = locked[0] if locked and max(locked) - min(locked) < 1e-8 else statistics.median(values)
            for note in candidates:
                if not _locked(note) and target > note["start"]:
                    change(note, field, target, "Aligned near-identical chord releases to avoid unnecessary notation voices.")

    return {"notes": result, "changes": changes,
            "settings": {"quantization": settings.quantization, "timing_tolerance_seconds": tolerance},
            "limitations": ["Timing cleanup is a bounded heuristic, not inferred expressive intent.",
                            "Repeated attacks, locked edits, offbeat groove and independent long holds are preserved.",
                            "No finger numbers, pedal or clef changes are guessed."]}


def assess_notation(score: dict) -> dict:
    """Describe remaining density/ledger concerns; not a visual-collision proof."""
    issues = []
    statistics = {"visible_rests": 0, "hidden_rests": 0, "tied_fragments": 0,
                  "sounding_events": 0, "maximum_voices_per_staff": 0,
                  "maximum_ledger_lines": 0}
    staff_lines = {"treble": (30, 38), "bass": (18, 26), "alto": (24, 32), "tenor": (22, 30)}
    # Diatonic keyboard positions; enharmonic spelling can shift this estimate
    # by one step, so this is a review cue, not an exact engraving measurement.
    steps = (0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 6)
    for part in score["parts"]:
        for staff in part["staves"]:
            bottom, top = staff_lines[staff["clef"]]
            for measure in staff["measures"]:
                sounding_voices = []
                ledgers = []
                mixed_hands = []
                for voice in measure["voices"]:
                    sounding = [e for e in voice["events"] if e["kind"] != "rest"]
                    if sounding:
                        sounding_voices.append(voice)
                    for event in voice["events"]:
                        if event["kind"] == "rest":
                            statistics["hidden_rests" if event.get("hidden") else "visible_rests"] += 1
                            continue
                        statistics["sounding_events"] += 1
                        statistics["tied_fragments"] += event.get("tie") is not None
                        if len(set(event.get("hands", {}).values())) > 1:
                            mixed_hands.append(event)
                        for pitch in event["pitches"]:
                            diatonic = (pitch // 12 - 1) * 7 + steps[pitch % 12]
                            lines = max(0, (bottom - diatonic) // 2, (diatonic - top) // 2)
                            statistics["maximum_ledger_lines"] = max(statistics["maximum_ledger_lines"], lines)
                            if lines >= 4:
                                ledgers.append((event, pitch, lines))
                statistics["maximum_voices_per_staff"] = max(statistics["maximum_voices_per_staff"], len(sounding_voices))
                if len(sounding_voices) > 2:
                    issues.append({"code": "dense_voices", "severity": "warning", "measure": measure["number"],
                                   "part_id": part["id"], "staff": staff["id"], "hand": None,
                                   "note_ids": sorted({n for v in sounding_voices for e in v["events"] for n in e["source_note_ids"]}),
                                   "detail": f"{len(sounding_voices)} independent voices remain on this staff; inspect the held lines and rest placement."})
                if ledgers:
                    issues.append({"code": "ledger_lines", "severity": "warning", "measure": measure["number"],
                                   "part_id": part["id"], "staff": staff["id"],
                                   "hand": sorted({h for e, _, _ in ledgers for h in e.get("hands", {}).values()}) or None,
                                   "note_ids": sorted({n for e, _, _ in ledgers for n in e["source_note_ids"]}),
                                   "detail": f"Up to {max(item[2] for item in ledgers)} ledger lines remain; review the staff/clef or explicitly revoice this passage."})
                if mixed_hands:
                    issues.append({"code": "mixed_hands_on_staff", "severity": "warning", "measure": measure["number"],
                                   "part_id": part["id"], "staff": staff["id"], "hand": ["left", "right"],
                                   "note_ids": sorted({n for event in mixed_hands for n in event["source_note_ids"]}),
                                   "detail": "This staff contains a chord split between hands. Follow the explicit R.H./L.H. pitch labels; changing staff did not change the assigned hand."})
    return {"issues": issues, "statistics": statistics,
            "limitations": ["These counts are not a visual collision test or human readability assessment.",
                            "Ledger counts estimate diatonic register; enharmonic spelling can change the count."]}

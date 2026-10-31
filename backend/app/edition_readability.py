"""Conservative, audited melodic durations for explicitly derived editions.

This is an editorial operation, not a new inference result. It never selects a
melody from register, changes pitch, or supplies expression/phrase markings.
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict
from fractions import Fraction

VERSION = "melodic-duration-1"
MODES = {"faithful", "accompaniment", "solo_piano"}
MELODIC_ROLES = {"melody", "vocal_cue"}


def _locked(note: dict) -> bool:
    explicit = note.get("locked")
    return bool(explicit) if explicit is not None else note.get("provenance") in {"edited", "manually_added"}


def _marked(note: dict) -> bool:
    # These optional annotations may arrive from a user or imported score. Do
    # not manufacture them, or reinterpret an explicit articulation as jitter.
    return any(note.get(field) for field in (
        "articulation", "articulations", "breath", "breath_after", "breath_before", "phrase_end", "phrase_start",
        "pedal_sustained", "fingering",
    ))


def prepare_edition_notes(notes: list[dict], score_settings, *, mode: str, enabled: bool = True) -> dict:
    """Return a copy with bounded melodic grid/release edits and an audit trail.

    Same-line means an explicit melodic role, source part, and voice. Ambiguous
    polyphony, locks, repeated-pitch release gaps, and marked phrase boundaries
    are preserved. This pass must run before the final physical-playability
    assessment because a longer notated melody can hold a piano key longer.
    """
    from .score import ScoreSettings, estimate_tempo, note_grid

    if mode not in MODES:
        raise ValueError("Choose faithful, accompaniment, or solo_piano.")
    settings = score_settings if isinstance(score_settings, ScoreSettings) else ScoreSettings.model_validate(score_settings)
    result = copy.deepcopy(notes)
    tempo = settings.tempo_bpm or estimate_tempo(notes)["bpm"]
    seconds_per_beat = 60 / tempo
    # This pass only processes melody/cue roles, which share the compiler's
    # selected melodic grid. Instrumental quantization remains independent.
    grid = float(note_grid(settings, {"role": "melody"})) * seconds_per_beat
    origin = settings.first_downbeat_seconds - float(Fraction(settings.pickup_beats)) * seconds_per_beat
    jitter = min(.1 * seconds_per_beat, grid * .25)
    maximum_gap = .22 * seconds_per_beat
    minimum_duration = .5 * seconds_per_beat
    changes = []
    metadata = {"version": VERSION, "enabled": bool(enabled and mode != "faithful"), "mode": mode,
                "tempo_bpm": tempo, "quantization": settings.quantization,
                "melody_quantization": settings.melody_quantization,
                "maximum_grid_correction_beats": jitter / seconds_per_beat,
                "maximum_melodic_gap_beats": .22, "minimum_melodic_duration_beats": .5}
    limitations = [
        "Duration normalization is an explicit arrangement edit, not a detected articulation or phrase.",
        "Only identified melody/cue lines are considered; accompaniment rests and independent holds are unchanged.",
        "No notes, dynamics, slurs, lyrics, breath marks or pedal instructions are invented.",
    ]
    if not metadata["enabled"]:
        return {"notes": result, "changes": changes, "settings": metadata, "limitations": limitations}

    lines = defaultdict(list)
    for note in result:
        if note.get("role") in MELODIC_ROLES:
            lines[(note.get("part_id"), note.get("voice_id"), note["role"])].append(note)

    def snapped(value):
        candidate = origin + math.floor((value - origin) / grid + .5) * grid
        return round(candidate, 9) if candidate >= 0 and abs(candidate - value) <= jitter + 1e-9 else value

    for line in lines.values():
        line.sort(key=lambda n: (n["start"], n["end"], n["id"]))
        # Use immutable neighbors: one edit must not enlarge the next edit's
        # tolerance or conceal a polyphonic overlap in the source line.
        originals = copy.deepcopy(line)
        targets = []
        for index, original in enumerate(originals):
            previous = originals[index - 1] if index else None
            following = originals[index + 1] if index + 1 < len(originals) else None
            physical = original.get("key_release_seconds")
            eligible = (not _locked(original) and not _marked(original)
                        and not (previous and previous["end"] > original["start"] + 1e-9)
                        and not (following and following["start"] < original["end"] - 1e-9)
                        and not (physical is not None and abs(physical - original["end"]) > 1e-9))
            start, end = snapped(original["start"]), snapped(original["end"])
            if previous and start < previous["end"] - 1e-9:
                start = original["start"]
            if following and end > following["start"]:
                end = original["end"]
            if following and (following["pitch"] == original["pitch"] or _marked(following)
                              or following.get("phrase_id") != original.get("phrase_id")):
                end = original["end"]
            eligible = eligible and end - start >= minimum_duration - 1e-9
            targets.append((start, end) if eligible else None)

        for index, note in enumerate(line):
            original = originals[index]
            following = originals[index + 1] if index + 1 < len(originals) else None
            if targets[index] is None:
                continue
            # A separate physical release can describe manual pedal or another
            # articulation; leave that distinction completely unchanged.
            physical = original.get("key_release_seconds")
            start, end = targets[index]
            next_start = ((targets[index + 1][0] if targets[index + 1] else following["start"])
                          if following else None)
            if next_start is not None and end > next_start:
                end = original["end"]
            repeated = following is not None and following["pitch"] == original["pitch"]
            if repeated:
                end = original["end"]

            same_phrase = (following is not None and not _marked(following)
                           and not following.get("breath_before") and not following.get("phrase_start")
                           and original.get("phrase_id") == following.get("phrase_id"))
            original_gap = following["start"] - original["end"] if following else math.inf
            next_on_grid = next_start is not None and abs((next_start - origin) / grid - round((next_start - origin) / grid)) < 1e-7
            joined = (same_phrase and not repeated and next_on_grid
                      and 1e-9 < original_gap <= maximum_gap + 1e-9
                      and end - start >= minimum_duration - 1e-9
                      and next_start > end and next_start - end <= maximum_gap + 1e-9)
            if joined:
                end = next_start
            if end <= start or end - start < minimum_duration - 1e-9:
                continue

            updates = {"start": start, "end": end}
            if physical is not None:
                updates["key_release_seconds"] = end
            for field, after in updates.items():
                before = original[field]
                if abs(before - after) <= 1e-9:
                    continue
                note.setdefault("original_timing", {key: original[key] for key in ("start", "end", "pitch")})
                note[field] = after
                note["provenance"], note["locked"] = "edited", False
                changes.append({"note_ids": [note["id"]], "field": field, "before": before,
                                "after": after, "pass": "edition_melodic_duration", "version": VERSION,
                                "reason": ("Extended an identified melodic release across a sub-beat gap within its line."
                                           if joined and field != "start" else
                                           "Aligned an identified melodic timing within the selected grid tolerance.")})

    return {"notes": result, "changes": changes, "settings": metadata, "limitations": limitations}

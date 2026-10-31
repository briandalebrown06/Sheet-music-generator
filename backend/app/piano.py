"""A reversible piano reduction and a conservative, sequence-aware hand audit.

This module creates an ARRANGEMENT from supplied events. It does not infer hands,
pedal, harmony or finger numbers from audio. Its geometric and movement costs are
product heuristics, not a biomechanical model or a claim of pianist validation.
The caller keeps the original project and persists this result as a new revision.
"""
from __future__ import annotations

import copy
import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .score import GRIDS, ScoreSettings, bar_length, estimate_tempo, note_grid, _snap


class PianoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    profile: Literal["comfortable_accompaniment"] = "comfortable_accompaniment"
    left_reach: int = Field(default=9, ge=5, le=12)
    right_reach: int = Field(default=9, ge=5, le=12)
    max_keys_per_hand: int = Field(default=5, ge=1, le=5)
    target_keys_per_hand: int = Field(default=3, ge=1, le=5)
    max_jump_semitones: float = Field(default=12, ge=3, le=24)
    max_jump_semitones_per_second: float = Field(default=28, ge=8, le=60)
    attack_tolerance_seconds: float = Field(default=.035, ge=0, le=.08)
    include_vocal_cue: bool = False


ENGINE_VERSION = "scorecraft-piano-phrase-v4"

HANDS = ("left", "right")
PROTECTED_ROLES = {"bass", "riff", "countermelody", "melody"}
ROLES = PROTECTED_ROLES | {"harmony", "texture", "vocal_cue"}
LIMITATIONS = [
    "Automated constraint compliance is not pianist review or a guarantee of comfort.",
    "Hand movement and keyboard-shape estimates are conservative heuristics; no finger numbers are generated.",
    "Without an explicit pedal-sustained flag and physical key-release time, sounding notes are treated as physically held.",
    "Bass/texture priorities inferred from register are editorial assumptions; mark important riffs, bass lines and locked edits before arranging.",
    "This pass does not identify octave transcription errors or recover isolated piano from a mixed-instrument stem.",
]


def _settings(piano_settings, score_settings):
    piano = piano_settings if isinstance(piano_settings, PianoSettings) else PianoSettings.model_validate(piano_settings or {})
    score = score_settings if isinstance(score_settings, ScoreSettings) else ScoreSettings.model_validate(score_settings or {})
    return piano, score


def _locked(note):
    explicit = note.get("locked")
    return bool(explicit if explicit is not None else note.get("provenance") in {"edited", "manually_added"})


def _keyboard(pitch: int) -> tuple[float, float]:
    # Coordinates in approximate white-key widths, with black keys at a deeper
    # playing plane. These are deliberately NOT hand-size measurements.
    positions = (0., .6, 1., 1.6, 2., 3., 3.6, 4., 4.6, 5., 5.6, 6.)
    black = pitch % 12 in {1, 3, 6, 8, 10}
    return (pitch // 12 * 7 + positions[pitch % 12], .75 if black else 0.)


def _shape_cost(pitches: list[int], hand: str) -> tuple[bool, float]:
    return _cached_shape(tuple(sorted(set(pitches))), hand)


@lru_cache(maxsize=16384)
def _cached_shape(keys: tuple[int, ...], hand: str) -> tuple[bool, float]:
    """Internal ordered finger-capacity search; never outputs guessed fingering.

    Unlike span/count alone, this considers uneven gaps and black/white depth.
    Finger-gap capacities below are intentionally configurable-profile heuristics
    in approximate key-width units, not measured human anatomical limits.
    """
    if len(keys) <= 1:
        return True, 0.
    if len(keys) > 5:
        return False, 20. * (len(keys) - 5)
    adjacent_capacity = (3.3, 2.2, 2.0, 2.2)  # thumb-index ... ring-little
    best = float("inf")
    for fingers in itertools.combinations(range(5), len(keys)):
        ordered = fingers if hand == "right" else tuple(reversed(fingers))
        penalty = 0.
        for a, b, fa, fb in zip(keys, keys[1:], ordered, ordered[1:]):
            xa, ya = _keyboard(a)
            xb, yb = _keyboard(b)
            distance = math.hypot(xb - xa, yb - ya)
            capacity = sum(adjacent_capacity[min(fa, fb):max(fa, fb)])
            penalty += max(0., distance - capacity) ** 2
        # The thumb on a black key adds a mild cost, not an impossibility claim.
        for pitch, finger in zip(keys, ordered):
            if finger == 0 and _keyboard(pitch)[1]:
                penalty += .015
        best = min(best, penalty)
    return best < .08, best


def _timeline(notes, score):
    if len(notes) > 10000:
        raise ValueError("Piano arrangement supports at most 10,000 input notes; select a shorter passage.")
    bpm = Fraction(str(score.tempo_bpm or estimate_tempo(notes)["bpm"]))
    tempo = float(bpm)
    origin = Fraction(str(score.first_downbeat_seconds)) - Fraction(score.pickup_beats) * 60 / bpm
    def snap(seconds, grid):
        # Use the compiler's exact decimal-to-rational timeline and half-up
        # quantizer. Binary float ties can shorten a held key by a whole grid
        # step and falsely clear a reach conflict in the exported notation.
        return _snap((Fraction(str(seconds)) - origin) * bpm / 60, grid)
    result = []
    ids = set()
    for index, raw in enumerate(notes):
        note = copy.deepcopy(raw)
        identifier = str(note.get("id", f"event-{index}"))
        if identifier in ids:
            raise ValueError("Piano input contains duplicate stable note IDs.")
        ids.add(identifier)
        note["id"] = identifier
        if any(not math.isfinite(float(note[key])) for key in ("start", "end", "pitch")):
            raise ValueError("Piano input must contain finite pitches and times.")
        if note["end"] <= note["start"] or note["start"] < 0 or not 0 <= int(note["pitch"]) <= 127 or float(note["pitch"]) != int(note["pitch"]):
            raise ValueError("Piano input must contain positive durations and MIDI pitches 0–127.")
        if note.get("hand") not in (*HANDS, None):
            raise ValueError("Hand must be left, right, or unassigned.")
        if note.get("role") not in (*ROLES, None):
            raise ValueError("Unknown accompaniment note role.")
        if note.get("fingering") is not None and (not isinstance(note["fingering"], int) or not 1 <= note["fingering"] <= 5):
            raise ValueError("Manual piano fingering must be a finger number from 1 to 5.")
        release = note.get("key_release_seconds")
        if release is not None and (not math.isfinite(float(release)) or not note["start"] < release <= note["end"] + 1e-8):
            raise ValueError("Physical key release must follow the attack and stay within the sounding duration.")
        physical = release if note.get("pedal_sustained") and release is not None else note["end"]
        grid = note_grid(score, note)
        start = snap(note["start"], grid)
        end = max(snap(physical, grid), start + grid)
        if start < 0:
            raise ValueError("This downbeat/pickup alignment excludes notes before the score starts; move the downbeat earlier or increase the pickup.")
        note.update(_start=float(start * 60 / bpm), _release=float(end * 60 / bpm),
                    _pitch=int(note["pitch"]) + score.transpose_semitones, _locked=_locked(note),
                    _role=note.get("role"), _physical_seconds=float(physical))
        note["_inferred_role"] = bool(note.get("role") and note.get("arrangement_provenance", {}).get("inferred_role") == note.get("role"))
        result.append(note)
    result.sort(key=lambda n: (n["_start"], n["_pitch"], n["id"]))
    return result, tempo


def _groups(timeline, tolerance):
    result = []
    for note in timeline:
        if not result or note["_start"] - result[-1][0]["_start"] > tolerance + 1e-9:
            result.append([])
        result[-1].append(note)
    return result


def _measure(seconds, score, bpm):
    beats = seconds * bpm / 60
    pickup = float(Fraction(score.pickup_beats))
    return 0 if beats < pickup - 1e-8 else int(math.floor((beats - pickup + 1e-8) / float(bar_length(score.time_signature)))) + 1


def _hand_metrics(notes, hand, piano):
    keys = sorted({n["_pitch"] for n in notes if n.get("hand") == hand})
    span = keys[-1] - keys[0] if keys else 0
    shape, shape_cost = _shape_cost(keys, hand)
    maximum = piano.left_reach if hand == "left" else piano.right_reach
    penalty = 800 * max(0, span - maximum) + 1200 * max(0, len(keys) - piano.max_keys_per_hand)
    penalty += 600 * (not shape) + 20 * shape_cost
    penalty += 1800 * sum(not 21 <= pitch <= 108 for pitch in keys)
    penalty += 2000 * len(_manual_finger_conflicts(notes, hand))
    return {"span": span, "keys": len(keys), "shape_ok": shape, "shape_cost": shape_cost,
            "center": (keys[0] + keys[-1]) / 2 if keys else None,
            "anchor_slack": max(0., (maximum - span) / 2), "penalty": penalty}


def _manual_finger_conflicts(notes, hand):
    assigned = defaultdict(list)
    for note in notes:
        if note.get("hand") == hand and note.get("fingering") is not None:
            assigned[note["fingering"]].append(note)
    return [(finger, assigned_notes) for finger, assigned_notes in assigned.items()
            if len({note["_pitch"] for note in assigned_notes}) > 1]


def _movement(previous, current, moment, piano):
    if not previous or current["center"] is None:
        return None
    distance = abs(current["center"] - previous["center"])
    # A single note does not pin the palm above that key: neighboring notes may
    # use other fingers without moving the hand. Subtract both feasible anchor
    # intervals before estimating the translation required between key sets.
    translation = max(0., distance - current.get("anchor_slack", 0.) - previous.get("anchor_slack", 0.))
    interval = max(.001, moment - previous["start"])
    free = max(0., moment - previous["release"])
    # A small transition allowance avoids declaring every legato change
    # impossible. Fast IOIs still restrict the time that can be used to move.
    available = min(interval, max(.12, free + .12))
    allowed = min(piano.max_jump_semitones + piano.max_jump_semitones_per_second * max(0., free - .15),
                  3 + piano.max_jump_semitones_per_second * available)
    excess = max(0., translation - allowed)
    return {"distance_semitones": round(distance, 3), "available_seconds": round(available, 4),
            "required_translation_semitones": round(translation, 3), "allowed_semitones": round(allowed, 3), "excess": excess}


def _line(note):
    if note.get("voice_id") is not None or note.get("_role") in PROTECTED_ROLES:
        return f"{note.get('part_id', 'part-1')}:{note.get('voice_id') or note.get('_role')}"
    return None



def _density_summary(piano):
    return {"target_keys_per_hand": piano.target_keys_per_hand, "max_keys_per_hand": piano.max_keys_per_hand,
            "max_left_keys": 0, "max_right_keys": 0, "over_target_frames": 0,
            "left_over_target_frames": 0, "right_over_target_frames": 0, "excess_key_frames": 0,
            "target_met": True, "protected_exceptions": [], "dense_passages": [], "details_truncated": False}


def _capture_density(density, notes, hand, metrics, measure):
    keys = metrics["keys"]
    density[f"max_{hand}_keys"] = max(density[f"max_{hand}_keys"], keys)
    excess = keys - density["target_keys_per_hand"]
    if excess <= 0:
        return
    protected = {n["_pitch"] for n in notes if n["_locked"] or n.get("fingering") is not None or n.get("_role") in PROTECTED_ROLES}
    density["target_met"] = False
    density["over_target_frames"] += 1
    density[f"{hand}_over_target_frames"] += 1
    density["excess_key_frames"] += excess
    passage = {"measure": measure, "hand": hand, "note_ids": sorted(n["id"] for n in notes),
               "key_count": keys, "protected_keys": len(protected)}
    if len(density["dense_passages"]) < 256:
        density["dense_passages"].append(passage)
    else:
        density["details_truncated"] = True
    if len(protected) > density["target_keys_per_hand"] and len(density["protected_exceptions"]) < 256:
        density["protected_exceptions"].append({**passage, "detail": "Protected melody, bass, marked figures or manual notes exceed the editorial target; retain them for explicit review."})

def assess_playability(notes: list[dict], piano_settings=None, score_settings=None) -> dict:
    """Assess the intended score timing/pitches without modifying supplied notes."""
    piano, score = _settings(piano_settings, score_settings)
    timeline, bpm = _timeline(notes, score)
    issues, active, previous, line_history = [], [], {}, defaultdict(list)
    maxima = {"left": {"span": 0, "keys": 0}, "right": {"span": 0, "keys": 0}}
    density = _density_summary(piano)
    cue_count = sum(n.get("role") == "vocal_cue" for n in timeline)
    # Do not collapse adjacent rendered attacks at very fast triplet grids.
    tolerance = min(piano.attack_tolerance_seconds, min((float(note_grid(score, n)) for n in timeline), default=float(GRIDS[score.quantization])) * 60 / bpm * .49)
    groups = _groups([n for n in timeline if n.get("role") != "vocal_cue"], tolerance)
    def issue(code, hand, group, detail, severity="error", **values):
        issues.append({"code": code, "severity": severity, "measure": _measure(max(n["_start"] for n in group), score, bpm),
                       "hand": hand, "note_ids": sorted({n["id"] for n in group}), "detail": detail, **values})
    for group in groups:
        moment = group[0]["_start"]
        active = [n for n in active if n["_release"] > moment + 1e-8]
        unknown = [n for n in group if n.get("hand") is None]
        if unknown:
            issue("hand_unassigned", "unassigned", unknown,
                  "These notes have no physical hand assignment. The provisional audit uses a pitch split only; staff placement does not establish playability.")
        for n in group:
            if n.get("hand") is None:
                n["hand"] = "left" if n["_pitch"] < score.split_pitch else "right"
            if not 21 <= n["_pitch"] <= 108:
                issue("keyboard_range", n["hand"], [n], f"Written MIDI pitch {n['_pitch']} lies outside the 88-key piano range 21–108.")
            if n.get("key_release_seconds") is not None and not n.get("pedal_sustained"):
                issue("unconfirmed_pedal_release", n["hand"], [n], "An early key release is not marked pedal-sustained; the audit conservatively holds the key until the sounding note ends.", "warning")
            line = _line(n)
            if line and (not line_history[line] or line_history[line][-1][1] != n["hand"]):
                line_history[line].append((moment, n["hand"], n))
        duplicate_attacks = defaultdict(list)
        for n in group:
            duplicate_attacks[n["_pitch"]].append(n)
        for pitch, duplicates in duplicate_attacks.items():
            if len(duplicates) > 1:
                issue("duplicate_attack", "both", duplicates, f"Written pitch {pitch} is duplicated at one attack. One piano key cannot express independent simultaneous parts; consolidate or remove the redundant doubling.", "warning")
        combined = active + group
        for hand in HANDS:
            played = [n for n in combined if n["hand"] == hand]
            attacked = [n for n in group if n["hand"] == hand]
            if not attacked:
                continue
            held = [n for n in active if n["hand"] == hand]
            metrics = _hand_metrics(played, hand, piano)
            _capture_density(density, played, hand, metrics, _measure(moment, score, bpm))
            reach = piano.left_reach if hand == "left" else piano.right_reach
            maxima[hand]["span"] = max(maxima[hand]["span"], metrics["span"])
            maxima[hand]["keys"] = max(maxima[hand]["keys"], metrics["keys"])
            if metrics["span"] > reach:
                issue("held_note_conflict" if held else "hand_reach", hand, played,
                      f"{metrics['span']}-semitone span exceeds this hand's {reach}-semitone limit" + (" while earlier notes must remain physically held." if held else "."),
                      span_semitones=metrics["span"], reach_limit=reach)
            if metrics["keys"] > piano.max_keys_per_hand:
                issue("finger_load", hand, played, f"{metrics['keys']} independently pressed keys exceed this hand's {piano.max_keys_per_hand}-key limit.", key_count=metrics["keys"])
            if not metrics["shape_ok"] and metrics["keys"] <= 5:
                issue("keyboard_shape", hand, played, "Uneven intervals and black/white-key geometry do not fit the profile's ordered finger-capacity heuristic, despite the pitch-span/key-count checks.")
            for finger, assigned in _manual_finger_conflicts(played, hand):
                issue("manual_finger_conflict", hand, assigned,
                      f"Manual finger {finger} is assigned to different keys that must be pressed or held simultaneously. Correct the fingering or hand assignment.", finger=finger)
            duplicated = [n for n in attacked if any(old["_pitch"] == n["_pitch"] for old in held)]
            if duplicated:
                issue("held_key_reattack", hand, held + duplicated, "A key is attacked while another notated event requires that same hand to keep it held. Resolve the overlapping articulation.", "warning")
            movement = _movement(previous.get(hand), metrics, moment, piano)
            if movement and movement["excess"] > 1e-7:
                issue("rapid_jump", hand, attacked, f"A {movement['distance_semitones']:g}-semitone register change needs about {movement['required_translation_semitones']:g} semitones of hand travel with {movement['available_seconds']:g} s available; the profile allows {movement['allowed_semitones']:g}. Movement is a heuristic requiring review.", "warning", **{k:v for k,v in movement.items() if k != "excess"})
            previous[hand] = {"center": metrics["center"], "anchor_slack": metrics["anchor_slack"], "start": moment, "release": max(n["_release"] for n in played), "notes": attacked}
        left, right = [n for n in combined if n["hand"] == "left"], [n for n in combined if n["hand"] == "right"]
        if left and right and max(n["_pitch"] for n in left) > min(n["_pitch"] for n in right):
            issue("hand_crossing", "both", combined, "The assigned hand ranges overlap or cross. This may be intentional but needs pianist review.", "warning")
        elif all(h in previous for h in HANDS) and previous["left"]["center"] > previous["right"]["center"] and moment - min(previous[h]["start"] for h in HANDS) <= 2 * 60 / bpm:
            issue("crossed_positions", "both", previous["left"]["notes"] + previous["right"]["notes"],
                  "The hands' recent positions cross during this alternating passage, although the attacks do not overlap. Review unnecessary crossing and coordination.", "warning")
        active.extend(group)
    for history in line_history.values():
        for index in range(3, len(history)):
            window = history[index-3:index+1]
            if window[-1][0] - window[0][0] <= 8 * 60 / bpm:
                issue("line_reassignment", "both", [x[2] for x in window], "This marked musical line changes hand three times within eight beats. Review coordination and phrase continuity.", "warning")
    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = len(issues) - error_count
    return {"schema_version": 1, "settings": piano.model_dump(), "tempo_bpm": bpm,
            "transpose_semitones": score.transpose_semitones, "issues": issues, "density": density,
            "summary": {"passed_automated_constraints": not issues, "requires_review": bool(issues),
                        "hard_constraints_passed": error_count == 0, "pianist_review": "not_performed",
                        "error_count": error_count, "warning_count": warning_count, "note_count": len(timeline),
                        "vocal_cue_notes_excluded_from_hand_checks": cue_count,
                        "max_left_span": maxima["left"]["span"], "max_right_span": maxima["right"]["span"],
                        "max_left_keys": maxima["left"]["keys"], "max_right_keys": maxima["right"]["keys"]},
            "limitations": LIMITATIONS}


@dataclass
class _State:
    cost: float
    held: list
    positions: dict
    lines: dict
    parent: "_State | None" = None
    placed: list | None = None
    released: list | None = None
    removed: list | None = None


def _importance(note, pitch_classes):
    if note["_locked"]:
        return 10000
    role = note["_role"]
    if role in PROTECTED_ROLES:
        return 200
    if note.get("_redundant_attack"):
        return 4
    value = 32 if role == "harmony" else 18
    if pitch_classes[note["_pitch"] % 12] == 1:
        value += 45  # preserve chord pitch-class identity before octave doubling
    return value


def _options(note, score):
    hands = [note["hand"]] if note["_locked"] and note.get("hand") in HANDS else HANDS
    shifts = [0] if note["_locked"] or note["_role"] in PROTECTED_ROLES else [0, -12, 12, -24, 24]
    result = []
    for shift in shifts:
        pitch = note["_pitch"] + shift
        if shift and (not 21 <= pitch <= 108 or not 0 <= pitch - score.transpose_semitones <= 127):
            continue
        for hand in hands:
            candidate = dict(note, hand=hand, _pitch=pitch)
            cost = abs(shift) / 12 * 9 + (2 if note.get("hand") in HANDS and note["hand"] != hand else 0)
            if note["_role"] == "bass" and hand == "right":
                cost += 3  # preference, never a forced pitch cutoff
            result.append((candidate, cost))
    return result


def _occupancy_cost(notes, piano):
    cost = sum(_hand_metrics(notes, hand, piano)["penalty"] for hand in HANDS)
    left = [n["_pitch"] for n in notes if n["hand"] == "left"]
    right = [n["_pitch"] for n in notes if n["hand"] == "right"]
    if left and right:
        cost += 75 * max(0, max(left) - min(right))
    # One piano key cannot be two independent simultaneously held notes.
    duplicates = len(notes) - len({n["_pitch"] for n in notes})
    return cost + 80 * duplicates


def _position_cost(held, placed, positions, moment, piano):
    """Include motion while pruning note choices, not just after a chord is built."""
    combined = held + placed
    total = _occupancy_cost(combined, piano)
    centers = {hand: position["center"] for hand, position in positions.items()}
    for hand in HANDS:
        if not any(n["hand"] == hand for n in placed):
            continue
        metrics = _hand_metrics(combined, hand, piano)
        move = _movement(positions.get(hand), metrics, moment, piano)
        if move:
            total += move["distance_semitones"] * .25 + move["excess"] * 50
        centers[hand] = metrics["center"]
    if all(hand in centers for hand in HANDS):
        total += 75 * max(0., centers["left"] - centers["right"])
    return total


def _extend(state, group, piano, score):
    moment = group[0]["_start"]
    held = [n for n in state.held if n["_release"] > moment + 1e-8]
    variants = [(held, [], 0.)]
    new_bass_attack = any(n["_role"] == "bass" for n in group)
    releasable = [n for n in held if not n["_locked"] and (n["_role"] not in PROTECTED_ROLES or
                  (new_bass_attack and n["_role"] == "bass" and n.get("_inferred_role")))]
    if releasable:
        # Deliberate secondary-texture articulation, never invented pedal.
        kept = [n for n in held if n not in releasable]
        variants.append((kept, [{"id": n["id"], "at": moment, "bass_articulation": n["_role"] == "bass",
                                "pedal": bool(n.get("pedal_sustained") and n.get("key_release_seconds") is not None)}
                               for n in releasable], 10. * len(releasable)))
    pitch_classes = Counter(n["_pitch"] % 12 for n in group)
    prioritized = sorted(group, key=lambda n: (-_importance(n, pitch_classes), n["_pitch"], n["id"]))
    results = []
    for retained, releases, release_cost in variants:
        partial = [(release_cost, [], [])]
        for note in prioritized:
            next_partial = []
            for edit_cost, placed, removed in partial:
                for chosen, option_cost in _options(note, score):
                    extra = option_cost
                    line = _line(chosen)
                    if line and line in state.lines and state.lines[line] != chosen["hand"]:
                        extra += 8
                    current = placed + [chosen]
                    next_partial.append((edit_cost + extra, current, removed))
                if not note["_locked"] and note["_role"] not in PROTECTED_ROLES:
                    next_partial.append((edit_cost + _importance(note, pitch_classes), placed, removed + [note]))
            # A bounded beam retains competing registers/hands, so the next
            # attacks can choose a different phrase path from a greedy chord.
            next_partial.sort(key=lambda row: (row[0] + _position_cost(retained, row[1], state.positions, moment, piano),
                                              tuple((n["id"], n["hand"], n["_pitch"]) for n in row[1])))
            partial = next_partial[:10]
        for edit_cost, placed, removed in partial:
            combined = retained + placed
            cost = state.cost + edit_cost + _occupancy_cost(combined, piano)
            positions = dict(state.positions)
            lines = dict(state.lines)
            for hand in HANDS:
                attacks = [n for n in placed if n["hand"] == hand]
                if not attacks:
                    continue
                played = [n for n in combined if n["hand"] == hand]
                metrics = _hand_metrics(played, hand, piano)
                move = _movement(state.positions.get(hand), metrics, moment, piano)
                if move:
                    cost += move["distance_semitones"] * .25 + move["excess"] * 50
                positions[hand] = {"center": metrics["center"], "anchor_slack": metrics["anchor_slack"], "start": moment, "release": max(n["_release"] for n in played)}
            if all(h in positions for h in HANDS):
                cost += 75 * max(0., positions["left"]["center"] - positions["right"]["center"])
            for n in placed:
                if _line(n):
                    lines[_line(n)] = n["hand"]
            results.append(_State(cost, combined, positions, lines, state, placed, releases, removed))
    return results


def arrange_piano(notes: list[dict], piano_settings=None, score_settings=None) -> dict:
    """Return a derived accompaniment; original events are never mutated.

    A bounded beam spans successive attacks, retaining held-key obligations and
    previous hand positions. Search candidates redistribute, octave-revoice inner
    material, remove doublings/secondary textures, and release secondary held
    textures explicitly. Important marked roles and locked edits cannot be deleted.
    It does not add accompaniment, roll chords, or shift attacks into arpeggios.
    """
    piano, score = _settings(piano_settings, score_settings)
    before = assess_playability(notes, piano, score)
    timeline, bpm = _timeline(notes, score)
    # Reopening/revoicing an already allocated, physically valid accompaniment
    # must not run a fresh reduction that changes unrelated passages or drops notes.
    if before["summary"]["hard_constraints_passed"] and all(
            n.get("role") == "vocal_cue" or n.get("role") in ROLES and n.get("hand") in HANDS for n in timeline):
        return repair_piano_movement(notes, piano, score)
    original_by_id = {n["id"]: n for n in timeline}
    cues = [n for n in timeline if n.get("role") == "vocal_cue"]
    tolerance = min(piano.attack_tolerance_seconds, min((float(note_grid(score, n)) for n in timeline), default=float(GRIDS[score.quantization])) * 60 / bpm * .49)
    groups = _groups([n for n in timeline if n.get("role") != "vocal_cue"], tolerance)
    for group in groups:
        bass_id = min(group, key=lambda n: (n["_pitch"], -(n.get("velocity") or 64), n["id"]))["id"]
        for n in group:
            if n["_role"] is None:
                n["_role"] = "bass" if len(group) > 1 and n["id"] == bass_id else "harmony"
                n["_inferred_role"] = True
        same_pitch = defaultdict(list)
        for n in group:
            same_pitch[n["_pitch"]].append(n)
        for duplicates in same_pitch.values():
            if len(duplicates) > 1:
                primary = max(duplicates, key=lambda n: (n["_locked"], n["_role"] in PROTECTED_ROLES, n.get("velocity") or 64, n["_release"]))
                for n in duplicates:
                    n["_redundant_attack"] = n is not primary
    states = [_State(0., [], {}, {})]
    for group in groups:
        expanded = [candidate for state in states for candidate in _extend(state, group, piano, score)]
        expanded.sort(key=lambda state: state.cost)
        # Deduplicate physically equivalent phrase states while retaining the
        # cheapest source/edit lineage. Six alternatives bound long inputs.
        unique = {}
        for state in expanded:
            key = (tuple(sorted((n["id"], n["hand"], n["_pitch"], round(n["_release"], 7)) for n in state.held)),
                   tuple((h, round(p["center"], 3)) for h,p in sorted(state.positions.items())))
            if key not in unique:
                unique[key] = state
            if len(unique) >= 6:
                break
        states = list(unique.values())
    winner = min(states, key=lambda state: state.cost)
    chosen, removed, releases = [], [], []
    cursor = winner
    while cursor.parent is not None:
        chosen.extend(cursor.placed or [])
        removed.extend(cursor.removed or [])
        releases.extend(cursor.released or [])
        cursor = cursor.parent
    chosen.sort(key=lambda n: (n["start"], n["_pitch"], n["id"]))
    release_by_id = {}
    for release in releases:
        current = release_by_id.get(release["id"])
        if current is None or release["at"] < current["at"]:
            release_by_id[release["id"]] = release
    changes, output = [], []
    origin = score.first_downbeat_seconds - float(Fraction(score.pickup_beats)) * 60 / bpm
    retained_cues = [n for n in cues if piano.include_vocal_cue or n["_locked"]]
    for n in chosen + retained_cues:
        original = original_by_id[n["id"]]
        result = {k: copy.deepcopy(v) for k,v in n.items() if not k.startswith("_")}
        result["pitch"] = n["_pitch"] - score.transpose_semitones
        result["locked"] = n["_locked"]
        result.setdefault("source_note_ids", [n["id"]])
        result.setdefault("original_timing", {k: original[k] for k in ("start", "end", "pitch")})
        edits = []
        if original.get("role") is None and not n["_locked"]:
            result["role"] = n["_role"]
            edits.append({"field": "role", "before": None, "after": n["_role"],
                          "reason": "Editorial register-based priority assumption, not a detected instrument or musical role"})
        if original.get("hand") != result.get("hand") and result.get("role") != "vocal_cue":
            edits.append({"field": "hand", "before": original.get("hand"), "after": result["hand"], "reason": "Sequence-aware two-hand allocation"})
        if result["pitch"] != original["pitch"]:
            edits.append({"field": "pitch", "before": original["pitch"], "after": result["pitch"], "reason": "Octave revoicing of inner material; pitch class retained"})
        release = release_by_id.get(n["id"])
        if release and not n["_locked"]:
            seconds = min(float(original["end"]), release["at"] + origin)
            if seconds > float(original["start"]) + 1e-8:
                field = "key_release_seconds" if release["pedal"] else "end"
                reason = ("Articulate the inferred bass line at its next attack; original bass pitch/onset retained; no pedal was inferred"
                          if release.get("bass_articulation") else "Release secondary texture for the following attack; no pedal was inferred")
                edits.append({"field": field, "before": original.get(field), "after": seconds, "reason": reason})
                result[field] = seconds
        if edits:
            result["arrangement_provenance"] = {"kind": "derived_accompaniment", "source_note_ids": list(result["source_note_ids"]), "changes": edits,
                                                "inferred_role": n["_role"] if n.get("_inferred_role") else None}
            # Algorithmic edits must remain revoiceable, unlike locked user edits.
            if not n["_locked"]:
                result["provenance"] = "edited"
                result["locked"] = False
            changes.append({"note_id": n["id"], "action": "modify", "measure": _measure(n["_start"], score, bpm), "edits": edits})
        output.append(result)
    for n in removed:
        changes.append({"note_id": n["id"], "source_note_ids": n.get("source_note_ids", [n["id"]]), "action": "omit",
                        "measure": _measure(n["_start"], score, bpm), "reason": "Remove a redundant doubling or secondary texture to fit two physical hands"})
    if not piano.include_vocal_cue:
        for n in cues:
            if n["_locked"]:
                continue
            changes.append({"note_id": n["id"], "action": "exclude_vocal_cue", "measure": _measure(n["_start"], score, bpm),
                            "reason": "Vocal cue is excluded from the pianist's accompaniment; original source retained"})
    output.sort(key=lambda n: (n["start"], n["pitch"], n["id"]))
    refinement = repair_piano_movement(output, piano, score)
    output, after = refinement["notes"], refinement["report"]
    changes.extend(refinement["changes"])
    return {"notes": output, "settings": piano.model_dump(), "report": after, "before_report": before,
            "changes": changes, "arrangement": True, "engine": ENGINE_VERSION, "refinement": refinement["refinement"], "preserve_timing": False,
            "method": "Bounded phrase beam reduction followed by whole-score-verified hand and register refinement; no generated accompaniment or guessed pedal",
            "locked_note_ids": [n["id"] for n in timeline if n["_locked"]]}


# Fixed work limits keep this deterministic even on the maximum 10,000-note
# input. Exhaustion leaves a truthful audit; it never relaxes a constraint.
_REFINEMENT_EVALUATIONS = 12000
_REFINEMENT_ACCEPTED = 48
_REFINEMENT_CONTEXT_NOTES = 256


def _audit_cost(report):
    counts = Counter(item["code"] for item in report["issues"] if item["severity"] != "error")
    other = sum(count for code, count in counts.items() if code not in {"rapid_jump", "line_reassignment"})
    excess = sum(max(0., item.get("required_translation_semitones", 0) - item.get("allowed_semitones", 0))
                 for item in report["issues"] if item["code"] == "rapid_jump")
    return (report["summary"]["error_count"], other, report["summary"]["warning_count"], round(excess, 5))


def _refinement_mutable(note, octave=False):
    if note["_locked"] or note.get("fingering") is not None or note.get("role") == "vocal_cue":
        return False
    # Explicitly marked bass/riff/countermelody pitches remain exact. Register-
    # inferred bass may move an octave while retaining its identity and roots.
    return not octave or note["_role"] not in PROTECTED_ROLES or (note["_role"] == "bass" and note.get("_inferred_role"))


def _refinement_candidates(issue, timeline, score, bpm):
    """Small musical neighborhoods, never arbitrary note-ID combinations."""
    by_id = {n["id"]: n for n in timeline}
    affected = [by_id[nid] for nid in issue["note_ids"] if nid in by_id]
    if not affected:
        return
    first, last = min(n["_start"] for n in affected), max(n["_start"] for n in affected)
    grid_seconds = float(GRIDS[score.quantization]) * 60 / bpm
    if issue["code"] == "line_reassignment":
        selected = affected
    else:
        moments = sorted({n["_start"] for n in timeline})
        nearby = [t for t in moments if first - 1.5 * 60 / bpm <= t <= last + 2 * grid_seconds]
        selected = [n for n in timeline if n["_start"] in nearby]
    selected = [n for n in selected if _refinement_mutable(n)]
    # Singles plus simultaneous subsets and contiguous repeated-note gestures.
    # Those gestures allow a phrase to keep one hand instead of repairing only
    # its last attack and creating another jump immediately before it.
    bundles = [[n] for n in selected]
    attacks = defaultdict(list)
    repeats = defaultdict(list)
    for n in selected:
        attacks[(n["_start"], n.get("hand"))].append(n)
        repeats[(n["_pitch"], n.get("hand"))].append(n)
    for group in attacks.values():
        for length in range(2, min(3, len(group)) + 1):
            bundles.extend(list(subset) for subset in itertools.combinations(group[:5], length))
    for group in repeats.values():
        if 1 < len(group) <= 4:
            bundles.append(group)
    if issue["code"] == "rapid_jump":
        # A register transition often spans two preceding gestures. Moving just
        # the last chord relocates the jump; a uniform phrase octave preserves
        # the actual intervals and rhythm across both attacks.
        for hand in HANDS:
            previous = sorted({n["_start"] for n in selected if n.get("hand") == hand and n["_start"] < first})
            for length in (2, 3):
                for start in range(max(0, len(previous) - length + 1)):
                    moments = set(previous[start:start + length])
                    bundle = [n for n in selected if n.get("hand") == hand and n["_start"] in moments]
                    if 1 < len(bundle) <= 6:
                        bundles.append(bundle)
    seen = set()
    for bundle in bundles:
        for shift in (0, -12, 12, -24, 24):
            if shift and any(not _refinement_mutable(n, octave=True) for n in bundle):
                continue
            for flip in (False, True):
                if not shift and not flip:
                    continue
                patch = {}
                for n in bundle:
                    pitch = n["pitch"] + shift
                    if not 21 <= pitch + score.transpose_semitones <= 108 or not 0 <= pitch <= 127:
                        break
                    values = {}
                    if shift:
                        values["pitch"] = pitch
                    if flip:
                        values["hand"] = "right" if n.get("hand") == "left" else "left"
                    patch[n["id"]] = values
                else:
                    key = tuple((nid, tuple(sorted(values.items()))) for nid,values in sorted(patch.items()))
                    if key not in seen:
                        seen.add(key)
                        # Hand-only repair is cheapest; then local octave changes.
                        # Duration changes below are a last resort at equal gain.
                        yield patch, (0, len(bundle) * abs(shift) / 12, len(bundle))
    if issue["code"] == "line_reassignment":
        # Exchange an octave-doubled bass/inner tone between physical hands,
        # carrying a short repeated bass run in the same register. At the first
        # attack, matching boundaries retain the exact aggregate sounding keys;
        # subsequent bass roots retain their IDs, pitch classes and rhythm.
        for bass in affected:
            if bass["_role"] != "bass" or not bass.get("_inferred_role") or not _refinement_mutable(bass, octave=True):
                continue
            partners = [n for n in timeline if n["_start"] == bass["_start"] and n["_release"] == bass["_release"] and
                        n["_role"] in {"harmony", "texture"} and _refinement_mutable(n, octave=True) and
                        n.get("hand") != bass.get("hand") and abs(n["_pitch"] - bass["_pitch"]) == 12]
            for partner in partners[:3]:
                run = [bass]
                following = [n for n in timeline if n["_role"] == "bass" and n.get("part_id") == bass.get("part_id") and
                             bass["_start"] < n["_start"] <= bass["_start"] + 4 * 60 / bpm]
                for n in following:
                    if n["_pitch"] != bass["_pitch"] or n.get("hand") != bass.get("hand") or not n.get("_inferred_role") or not _refinement_mutable(n, octave=True):
                        break
                    run.append(n)
                    if len(run) == 4:
                        break
                shift = partner["_pitch"] - bass["_pitch"]
                if any(not 0 <= n["pitch"] + shift <= 127 for n in run):
                    continue
                patch = {n["id"]: {"pitch": n["pitch"] + shift, "hand": partner["hand"]} for n in run}
                patch[partner["id"]] = {"pitch": partner["pitch"] - shift, "hand": bass["hand"]}
                yield patch, (0, len(patch), len(patch))
    if issue["code"] == "rapid_jump":
        origin = score.first_downbeat_seconds - float(Fraction(score.pickup_beats)) * 60 / bpm
        for n in selected:
            if not _refinement_mutable(n, octave=True) or n["_start"] >= first:
                continue
            # At most one written grid unit of deliberate articulation. No
            # guessed pedal and no attack displacement: the audible note ends
            # earlier unless an explicit pedal/release pair already exists.
            release = n["_release"] - grid_seconds
            if release > first - grid_seconds + 1e-8:
                continue
            seconds = release + origin
            if release <= n["_start"] or seconds <= n["start"] or seconds >= n["_physical_seconds"]:
                continue
            field = "key_release_seconds" if n.get("pedal_sustained") and n.get("key_release_seconds") is not None else "end"
            yield {n["id"]: {field: seconds}}, (1, 0, 1)


def repair_piano_movement(notes: list[dict], piano_settings=None, score_settings=None) -> dict:
    """Refine allocated phrases without deleting notes or changing their attacks.

    Candidate audits cover surrounding held notes, preceding/following gestures
    and eight-beat line continuity. Every accepted edit is checked again against
    the entire score by the SAME public assessor. The pass never changes role,
    voice, tempo, pitch class, or a locked/manual-fingered event.
    """
    piano, score = _settings(piano_settings, score_settings)
    output = copy.deepcopy(notes)
    before = report = assess_playability(output, piano, score)
    timeline, bpm = _timeline(output, score)
    # Pin inferred tempo during local audits: a neighborhood must use the exact
    # same intended timing as the complete score, not a new local tempo guess.
    audit_score = score.model_copy(update={"tempo_bpm": bpm})
    original_by_id = {n["id"]: copy.deepcopy(n) for n in output}
    evaluations, accepted, changes = 0, 0, []
    exhausted = False
    dense_skipped = set()
    while accepted < _REFINEMENT_ACCEPTED and evaluations < _REFINEMENT_EVALUATIONS:
        improved = False
        targets = sorted((issue for issue in report["issues"] if issue["code"] in {"rapid_jump", "line_reassignment"}),
                         key=lambda item: (item["code"] != "line_reassignment", item["measure"], tuple(item["note_ids"])))
        if not targets:
            break
        by_id = {n["id"]: n for n in timeline}
        for issue in targets:
            affected = [by_id[nid] for nid in issue["note_ids"]]
            lo = min(n["_start"] for n in affected) - 10 * 60 / bpm
            hi = max(n["_start"] for n in affected) + 10 * 60 / bpm
            context_ids = {n["id"] for n in timeline if n["_release"] >= lo and n["_start"] <= hi}
            if len(context_ids) > _REFINEMENT_CONTEXT_NOTES:
                dense_skipped.add((issue["code"], tuple(issue["note_ids"])))
                continue  # dense passages retain their honest review warnings
            context = [n for n in output if n["id"] in context_ids]
            base_local = _audit_cost(assess_playability(context, piano, audit_score))
            evaluations += 1
            best = None
            for patch, edit_cost in _refinement_candidates(issue, timeline, score, bpm):
                if evaluations >= _REFINEMENT_EVALUATIONS - 1:
                    exhausted = True
                    break
                candidate = [{**n, **patch.get(n["id"], {})} for n in context]
                candidate_report = assess_playability(candidate, piano, audit_score)
                evaluations += 1
                cost = _audit_cost(candidate_report)
                if cost >= base_local:
                    continue
                rank = (cost, edit_cost, tuple((nid, tuple(sorted(values.items()))) for nid,values in sorted(patch.items())))
                if best is None or rank < best[0]:
                    best = (rank, patch)
            if best is None:
                if exhausted:
                    break
                continue
            patch = best[1]
            candidate = [{**n, **patch.get(n["id"], {})} for n in output]
            verified = assess_playability(candidate, piano, audit_score)
            evaluations += 1
            if _audit_cost(verified) >= _audit_cost(report):
                continue
            output, report = candidate, verified
            timeline, _ = _timeline(output, audit_score)
            accepted += 1
            improved = True
            break
        if not improved or exhausted:
            break
    for n in output:
        original = original_by_id[n["id"]]
        edits = []
        for field in ("hand", "pitch", "end", "key_release_seconds"):
            if n.get(field) == original.get(field):
                continue
            reason = {"hand": "Keep the phrase in a consistent, reachable hand using preceding and following attacks",
                      "pitch": "Local octave revoicing preserves note identity, attack and pitch class; inferred bass root sequence retained",
                      "end": "Shorten physical articulation by one grid unit to provide movement time; no pedal inferred",
                      "key_release_seconds": "Release the explicitly pedal-sustained key one grid unit earlier for movement; sounding sustain retained"}[field]
            edits.append({"field": field, "before": original.get(field), "after": n.get(field), "reason": reason})
        if edits:
            previous = copy.deepcopy(n.get("arrangement_provenance") or {})
            n["arrangement_provenance"] = {**previous, "kind": "derived_accompaniment", "engine": ENGINE_VERSION,
                                            "changes": list(previous.get("changes", [])) + edits}
            n.setdefault("source_note_ids", [n["id"]])
            n.setdefault("original_timing", {k: original[k] for k in ("start", "end", "pitch")})
            n["provenance"], n["locked"] = "edited", False
            changes.append({"note_id": n["id"], "action": "modify", "measure": _measure(by_id[n["id"]]["_start"], score, bpm), "edits": edits})
    return {"notes": output, "settings": piano.model_dump(), "report": report, "before_report": before,
            "changes": changes, "arrangement": True, "engine": ENGINE_VERSION, "preserve_timing": True,
            "method": "Bounded phrase refinement with unchanged full-score constraints; hands and local octaves before explicit articulation; no omitted notes or generated accompaniment",
            "locked_note_ids": [n["id"] for n in timeline if n["_locked"]],
            "refinement": {"candidate_evaluations": evaluations, "accepted_repairs": accepted,
                           "evaluation_limit": _REFINEMENT_EVALUATIONS, "repair_limit": _REFINEMENT_ACCEPTED,
                           "budget_exhausted": exhausted or accepted >= _REFINEMENT_ACCEPTED,
                           "dense_passages_skipped": len(dense_skipped)}}


_TEXTURE_AUDIT_LIMIT = 512
_TEXTURE_EDIT_LIMIT = 256


def _texture_candidates(notes, report, score, bpm, *, frame=None):
    timeline, _ = _timeline(notes, score)
    by_id = {n["id"]: n for n in timeline}
    attacks = defaultdict(list)
    for n in timeline:
        if n.get("role") != "vocal_cue":
            attacks[n["_start"]].append(n)
    dense_ids = {nid for frame in report["density"]["dense_passages"] for nid in frame["note_ids"]}
    frame = frame or next((frame for frame in report["density"]["dense_passages"]
                  if any(not by_id[nid]["_locked"] and by_id[nid].get("role") in {"harmony", "texture"}
                         and by_id[nid].get("fingering") is None for nid in frame["note_ids"])), None)
    if frame is None:
        return
    played = [by_id[nid] for nid in frame["note_ids"]]
    optional = [n for n in played if not n["_locked"] and n.get("role") in {"harmony", "texture"}
                and n.get("fingering") is None]
    onset_counts = Counter(n["_start"] for n in timeline if n.get("role") != "vocal_cue")
    def removable(n):
        return onset_counts[n["_start"]] > 1 and not n["_locked"] and n.get("role") in {"harmony", "texture"} and n.get("fingering") is None
    origin = score.first_downbeat_seconds - float(Fraction(score.pickup_beats)) * 60 / bpm
    moment = max(n["_start"] for n in played)
    keys = sorted({n["_pitch"] for n in played})
    for n in optional:
        duplicate_class = any(x["id"] != n["id"] and x["_pitch"] % 12 == n["_pitch"] % 12 for x in played)
        salience = (0 if duplicate_class else 4) + (2 if n["_pitch"] in (keys[0], keys[-1]) else 0)
        salience += (n.get("velocity") or 64) / 127 + min(1., n["end"] - n["start"])
        if n.get("voice_id") is not None:
            salience += 4
        if removable(n):
            yield {n["id"]: None}, salience, "Remove an optional inner tone while preserving the existing attack and protected musical lines"
            # Repeated chordal figures receive the same local voicing decision.
            # Exact attack pitch-class signatures prevent unrelated chords from
            # being collapsed into a generic pattern.
            signature = tuple(sorted({x["_pitch"] % 12 for x in attacks[n["_start"]]}))
            repeated = [x for x in timeline if x["id"] in dense_ids and removable(x) and
                        x.get("hand") == n.get("hand") and x["_pitch"] == n["_pitch"] and
                        x.get("part_id") == n.get("part_id") and abs(x["_start"] - n["_start"]) <= 8 * 60 / bpm and
                        tuple(sorted({a["_pitch"] % 12 for a in attacks[x["_start"]]})) == signature]
            if 1 < len(repeated) <= 8:
                yield {x["id"]: None for x in repeated}, salience * len(repeated), "Use the same lighter inner voicing across a recurring chordal figure; its attacks and source pitch classes remain traceable"
        # If a separately attacked inner line is still held into a dense chord,
        # prefer explicit articulation over erasing its unique rhythmic attack.
        step = float(note_grid(score, n)) * 60 / bpm
        release = math.floor((moment + 1e-8) / step) * step
        seconds = release + origin
        if release > n["_start"] + 1e-8 and release < n["_release"] - 1e-8 and n["start"] + .04 < seconds < n["_physical_seconds"]:
            field = "key_release_seconds" if n.get("pedal_sustained") and n.get("key_release_seconds") is not None else "end"
            yield {n["id"]: {field: seconds}}, salience + 1, "Articulate an optional held inner voice to lighten the following chord; preserve its attack and never infer pedal"


def refine_piano_texture(notes: list[dict], piano_settings=None, score_settings=None) -> dict:
    """Lighten an existing edition without regenerating any model notes.

    The three-key default is editorial, separate from the five-key physical
    limit. Only optional unlocked inner material can be omitted/articulated.
    Every candidate must improve density without worsening the unchanged hand
    audit; marked musical lines, source timing lineage and retained IDs survive.
    """
    piano, score = _settings(piano_settings, score_settings)
    output = copy.deepcopy(notes)
    before = report = assess_playability(output, piano, score)
    timeline, bpm = _timeline(output, score)
    if any(n.get("role") != "vocal_cue" and n.get("hand") not in HANDS for n in timeline):
        raise ValueError("Assign piano hands before refining an existing edition's texture.")
    audits, edit_count, changes = 0, 0, []
    rejected_frames = set()
    def hard_severity(audit):
        return sum(max(0., issue.get("span_semitones", 0) - issue.get("reach_limit", 0)) +
                   max(0., issue.get("key_count", 0) - piano.max_keys_per_hand)
                   for issue in audit["issues"] if issue["severity"] == "error")
    while (not report["density"]["target_met"] or report["summary"]["error_count"]) and audits < _TEXTURE_AUDIT_LIMIT and edit_count < _TEXTURE_EDIT_LIMIT:
        # Hard held-key conflicts take precedence even when only two or three
        # keys are involved. Their bounds use the exact canonical timeline.
        hard_frames = [{**issue, "hard_conflict": True} for issue in report["issues"] if issue["severity"] == "error" and
                       issue["code"] in {"held_note_conflict", "hand_reach", "finger_load", "keyboard_shape"}]
        frames = hard_frames + report["density"]["dense_passages"]
        # A protected/unsuitable first passage must not block later passages.
        work_report = {**report, "density": {**report["density"], "dense_passages": [frame for frame in frames
                       if (frame["measure"], frame["hand"], tuple(frame["note_ids"])) not in rejected_frames]}}
        by_id = {n["id"]: n for n in output}
        active_frame = next((frame for frame in work_report["density"]["dense_passages"] if any(
            not _locked(by_id[nid]) and by_id[nid].get("role") in {"harmony", "texture"} and by_id[nid].get("fingering") is None
            for nid in frame["note_ids"])), None)
        if active_frame is None:
            break
        best = None
        for patch, salience, reason in _texture_candidates(output, work_report, score, bpm, frame=active_frame):
            if audits >= _TEXTURE_AUDIT_LIMIT or edit_count + len(patch) > _TEXTURE_EDIT_LIMIT:
                break
            candidate = [{**n, **(patch.get(n["id"]) or {})} for n in output if n["id"] not in patch or patch[n["id"]] is not None]
            checked = assess_playability(candidate, piano, score)
            audits += 1
            improves_hard = (checked["summary"]["error_count"], hard_severity(checked)) < (report["summary"]["error_count"], hard_severity(report))
            improves_density = checked["density"]["excess_key_frames"] < report["density"]["excess_key_frames"]
            if _audit_cost(checked) > _audit_cost(report) or not (improves_hard or improves_density):
                continue
            rank = (checked["summary"]["error_count"], hard_severity(checked), checked["density"]["excess_key_frames"],
                    sum(value is None for value in patch.values()), salience, len(patch), tuple(sorted(patch)))
            if best is None or rank < best[0]:
                best = (rank, patch, reason, candidate, checked)
        if best is None:
            rejected_frames.add((active_frame["measure"], active_frame["hand"], tuple(active_frame["note_ids"])))
            continue
        _, patch, reason, candidate, checked = best
        originals = {n["id"]: n for n in output}
        for nid, fields in patch.items():
            if fields is None:
                changes.append({"note_id": nid, "source_note_ids": originals[nid].get("source_note_ids", [nid]),
                                "action": "omit_optional_inner_voice", "reason": reason})
            else:
                changed = next(n for n in candidate if n["id"] == nid)
                edits = [{"field": key, "before": originals[nid].get(key), "after": value, "reason": reason} for key,value in fields.items()]
                provenance = changed.get("arrangement_provenance") or {}
                changed["arrangement_provenance"] = {**provenance, "kind": "derived_accompaniment", "engine": ENGINE_VERSION,
                                                     "changes": list(provenance.get("changes", [])) + edits}
                changed["provenance"], changed["locked"] = "edited", False
                changed.setdefault("original_timing", {key: originals[nid][key] for key in ("start", "end", "pitch")})
                changed.setdefault("source_note_ids", [nid])
                changes.append({"note_id": nid, "action": "modify", "edits": edits})
        edit_count += len(patch)
        output, report = candidate, checked
    movement = repair_piano_movement(output, piano, score)
    # A motion repair may change a hand, but cannot quietly make the texture
    # denser than the explicit target improvement just achieved.
    if movement["report"]["density"]["excess_key_frames"] <= report["density"]["excess_key_frames"]:
        output, report = movement["notes"], movement["report"]
        changes.extend(movement["changes"])
    return {"notes": output, "settings": piano.model_dump(), "before_report": before, "report": report,
            "density_before": before["density"], "density_after": report["density"], "changes": changes,
            "engine": ENGINE_VERSION, "arrangement": True, "preserve_timing": True,
            "locked_note_ids": [n["id"] for n in timeline if n["_locked"]],
            "method": "Bounded phrase-consistent inner-voice simplification with a separate comfortable held-key target and unchanged physical/movement constraints",
            "refinement": {"density_audits": audits, "density_edits": edit_count, "density_audit_limit": _TEXTURE_AUDIT_LIMIT,
                           "density_edit_limit": _TEXTURE_EDIT_LIMIT, "density_budget_exhausted": audits >= _TEXTURE_AUDIT_LIMIT or edit_count >= _TEXTURE_EDIT_LIMIT,
                           "movement": movement["refinement"]}}

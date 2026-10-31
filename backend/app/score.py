"""Canonical, rational-beat notation independent of performance seconds.

The score compiler owns rhythm, voices, rests and ties. music21 engraves this
model; it is never asked to quantize the performance implicitly.
"""
from __future__ import annotations

import copy
import math
import statistics
import tempfile
import uuid
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GRIDS = {"quarter": Fraction(1), "eighth": Fraction(1, 2),
         "sixteenth": Fraction(1, 4), "eighth_triplet": Fraction(1, 3),
         "sixteenth_triplet": Fraction(1, 6)}
MANUAL_FIELDS = {"tempo_bpm", "time_signature", "key_signature", "first_downbeat_seconds",
                 "pickup_beats", "quantization", "melody_quantization", "clef", "vocal_clef", "transpose_semitones", "piano_split", "split_pitch"}


class ScoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tempo_bpm: float | None = Field(default=None, ge=30, le=300)
    time_signature: str = "4/4"
    key_signature: int = Field(default=0, ge=-7, le=7)
    first_downbeat_seconds: float = Field(default=0, ge=0, le=3600)
    pickup_beats: str = "0"
    quantization: Literal["quarter", "eighth", "sixteenth", "eighth_triplet", "sixteenth_triplet"] = "eighth"
    melody_quantization: Literal["same", "sixteenth"] = "same"
    clef: Literal["auto", "treble", "bass", "alto", "tenor"] = "auto"
    vocal_clef: Literal["auto", "treble", "bass"] = "auto"
    transpose_semitones: int = Field(default=0, ge=-24, le=24)
    piano_split: bool = False
    split_pitch: int = Field(default=60, ge=0, le=127)
    staff_overrides: dict[str, Literal["treble", "bass"]] = Field(default_factory=dict, max_length=10000)
    readability_mode: Literal["standard", "accompaniment"] = "standard"
    confirmed_fields: list[str] = Field(default_factory=list, max_length=12)
    estimated_fields: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("time_signature")
    @classmethod
    def valid_meter(cls, value: str) -> str:
        try:
            numerator, denominator = map(int, value.split("/"))
        except (ValueError, TypeError):
            raise ValueError("Time signature must be a numerator/denominator such as 4/4.")
        if numerator not in range(1, 17) or denominator not in (2, 4, 8, 16):
            raise ValueError("Time signature numerator must be 1–16 and denominator 2, 4, 8, or 16.")
        return f"{numerator}/{denominator}"

    @field_validator("pickup_beats")
    @classmethod
    def valid_pickup(cls, value: str) -> str:
        if len(value) > 16:
            raise ValueError("Pickup must be a small rational number of quarter-note beats.")
        try:
            pickup = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError("Pickup must be a rational number such as 1, 1/2, or 1/3.")
        if pickup < 0 or pickup.denominator > 24:
            raise ValueError("Pickup must be non-negative with denominator at most 24.")
        return str(pickup)

    @field_validator("confirmed_fields", "estimated_fields")
    @classmethod
    def valid_confirmed(cls, value: list[str]) -> list[str]:
        if any(item not in MANUAL_FIELDS for item in value):
            raise ValueError("Unknown manually confirmed score setting.")
        return sorted(set(value))

    @model_validator(mode="after")
    def valid_alignment(self):
        pickup = Fraction(self.pickup_beats)
        if pickup >= bar_length(self.time_signature):
            raise ValueError("The pickup must be shorter than a full measure.")
        if pickup / GRIDS[self.quantization] % 1:
            raise ValueError("The pickup must be an exact multiple of the selected quantization grid.")
        if bar_length(self.time_signature) / GRIDS[self.quantization] % 1:
            raise ValueError("Choose a quantization grid that divides the measure duration exactly.")
        if self.melody_quantization == "sixteenth" and "triplet" in self.quantization:
            raise ValueError("Use the same melody grid for triplet notation; mixed straight/triplet grids need explicit editing.")
        return self


def rational(value: Fraction | int) -> dict:
    value = Fraction(value)
    return {"numerator": value.numerator, "denominator": value.denominator}


def fraction(value: dict) -> Fraction:
    return Fraction(value["numerator"], value["denominator"])


def note_grid(settings: ScoreSettings, note: dict) -> Fraction:
    """A lead line may need shorter values than the accompaniment texture."""
    if settings.melody_quantization == "sixteenth" and note.get("role") in {"melody", "vocal_cue"}:
        return GRIDS["sixteenth"]
    return GRIDS[settings.quantization]


def bar_length(signature: str) -> Fraction:
    numerator, denominator = map(int, signature.split("/"))
    return Fraction(numerator * 4, denominator)


def default_score_settings(project: dict) -> dict:
    values: dict = {}
    confirmed = []
    if project.get("tempo_hint"):
        values["tempo_bpm"] = project["tempo_hint"]
        confirmed.append("tempo_bpm")
    if project.get("time_signature_hint"):
        values["time_signature"] = project["time_signature_hint"]
        confirmed.append("time_signature")
    values["confirmed_fields"] = confirmed
    return ScoreSettings(**values).model_dump()


def estimate_tempo(notes: list[dict]) -> dict:
    """A deliberately limited onset-interval estimate, not beat tracking.

    Chord onsets within 50 ms count once. Only relatively regular patterns are
    estimated; sparse/irregular material receives an explicitly assumed tempo.
    """
    onsets = []
    for start in sorted(float(note["start"]) for note in notes):
        if not onsets or start - onsets[-1] >= 0.05:
            onsets.append(start)
    intervals = [b - a for a, b in zip(onsets, onsets[1:]) if 0.08 <= b - a <= 2.0]
    if len(onsets) >= 4 and len(intervals) >= 3 and onsets[-1] - onsets[0] >= 1.2:
        median = statistics.median(intervals)
        dispersion = statistics.median(abs(item - median) for item in intervals) / median
        regular = sum(abs(item / median - round(item / median)) <= 0.12 for item in intervals)
        if dispersion <= 0.15 and regular / len(intervals) >= 0.75:
            bpm = 60 / median
            while bpm < 60:
                bpm *= 2
            while bpm > 180:
                bpm /= 2
            return {"bpm": round(bpm, 2), "source": "estimated",
                    "method": "Median of distinct note-onset intervals, octave-normalized to 60–180 BPM",
                    "detail": "Regular onsets support this estimate, but half/double tempo and beat placement remain ambiguous. Confirm by listening."}
    return {"bpm": 120.0, "source": "assumed", "method": "Fallback for sparse or irregular note onsets",
            "detail": "Not enough regular onset evidence for a useful estimate. 120 BPM is assumed; set the tempo manually."}


def _snap(value: Fraction, grid: Fraction) -> Fraction:
    # Nearest grid point, with deterministic upward ties (not banker's rounding).
    units = value / grid
    return math.floor(units + Fraction(1, 2)) * grid


def _event_id(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "score:" + ":".join(map(str, parts))))


def _ledger_estimate(pitch: int, staff: str) -> int:
    # Register estimate for choosing between existing piano staves only. No
    # clef/pitch/hand change is implied; manual staff placements take priority.
    bottom, top = (30, 38) if staff == "treble" else (18, 26)
    diatonic = (pitch // 12 - 1) * 7 + (0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 6)[pitch % 12]
    return max(0, (bottom - diatonic) // 2, (diatonic - top) // 2)


def _hand_directions(measure: dict, staff_id: str) -> list[dict]:
    """Consolidate hand instructions without changing voices or performed notes.

    Named pitches carry their instruction until another instruction for that
    pitch or the barline. In an entirely single-hand-at-a-time passage, the
    ordinary R.H./L.H. directions apply until the next change or barline.
    """
    normal = "left" if staff_id == "bass" else "right" if staff_id == "treble" else None
    if normal is None:
        return []
    onsets = defaultdict(lambda: defaultdict(set))
    for voice in measure["voices"]:
        for event in voice["events"]:
            if event["kind"] == "rest" or (event.get("tie") in {"continue", "stop"} and fraction(event["offset"]) != 0):
                continue
            for hand, pitches in event.get("hand_pitches", {}).items():
                for midi in pitches:
                    onsets[fraction(event["offset"])][midi].add(hand)
    mixed = any(len(set().union(*pitches.values())) > 1 for pitches in onsets.values())
    directions = []
    if not mixed:
        current = normal
        for offset, pitches in sorted(onsets.items()):
            hands = set().union(*pitches.values())
            if not hands:
                continue
            hand = next(iter(hands))
            if hand != current:
                directions.append({"offset": rational(offset), "hands": {hand: []}, "named_pitches": False})
                current = hand
        return directions
    previous = {}
    for offset, pitches in sorted(onsets.items()):
        changed = defaultdict(list)
        for midi, hands in sorted(pitches.items()):
            if hands != previous.get(midi, {normal}):
                for hand in sorted(hands):
                    changed[hand].append(midi)
            previous[midi] = hands
        if changed:
            directions.append({"offset": rational(offset), "hands": dict(changed), "named_pitches": True})
    return directions


def _rhythm_segments(start: Fraction, length: Fraction, grid: Fraction,
                     measure_length: Fraction, is_rest: bool = False,
                     beat_group: Fraction | None = None) -> list[tuple[Fraction, Fraction]]:
    """Use only printable durations, splitting offbeat values at beat boundaries."""
    if is_rest and start == 0 and length == measure_length:
        return [(start, length)]
    straight = [Fraction(4), Fraction(3), Fraction(2), Fraction(3, 2), Fraction(1),
                Fraction(3, 4), Fraction(1, 2), Fraction(1, 4)]
    triplet = [Fraction(4), Fraction(3), Fraction(2), Fraction(1), Fraction(2, 3),
               Fraction(1, 3), Fraction(1, 6)]
    choices = [value for value in (triplet if grid.denominator % 3 == 0 else straight)
               if value >= grid and value / grid % 1 == 0]
    result, cursor, remaining = [], start, length
    while remaining:
        allowed = remaining
        if cursor % 1:
            allowed = min(allowed, math.ceil(cursor) - cursor)
        # In readable accompaniment, a rest must reveal the primary beats. A
        # whole silent measure is still one rest, handled above. Compound meter
        # groups eighths in dotted-quarter beats rather than arbitrary quarters.
        if is_rest and beat_group is not None:
            allowed = min(allowed, (cursor // beat_group + 1) * beat_group - cursor)
        chosen = next((value for value in choices if value <= allowed), None)
        if chosen is None:
            # All boundaries are grid-aligned by construction; no tiny residual.
            chosen = grid
        if chosen > remaining:
            raise ValueError("The rhythmic grid cannot represent a measure boundary.")
        result.append((cursor, chosen))
        cursor += chosen
        remaining -= chosen
    return result


def _readable_rest_segments(start: Fraction, length: Fraction, grid: Fraction,
                            measure_length: Fraction, signature: str):
    if start == 0 and length == measure_length:
        return [(start, length)]
    numerator, denominator = map(int, signature.split("/"))
    compound = denominator == 8 and numerator > 3 and numerator % 3 == 0
    beat = Fraction(3, 2) if compound else Fraction(4, denominator)
    choices = [Fraction(2), Fraction(1), Fraction(1, 2), Fraction(1, 4)]
    if compound:
        choices.insert(0, Fraction(3, 2))
    if grid.denominator % 3 == 0:
        choices += [Fraction(2, 3), Fraction(1, 3), Fraction(1, 6)]
    choices = sorted({d for d in choices if d / grid % 1 == 0}, reverse=True)
    result, cursor, remaining = [], start, length
    while remaining:
        boundary = (cursor // beat + 1) * beat
        candidate = next((d for d in choices if d <= remaining and
                          ((compound and cursor + d <= boundary) or
                           (not compound and cursor / d % 1 == 0))), grid)
        result.append((cursor, candidate))
        cursor += candidate
        remaining -= candidate
    return result


def build_score(notes: list[dict], settings: ScoreSettings | dict, *, title: str,
                clip_duration_seconds: float | None = None, source_notes_revision: int = 0) -> dict:
    settings = settings if isinstance(settings, ScoreSettings) else ScoreSettings.model_validate(settings)
    accompaniment = settings.readability_mode == "accompaniment"
    if len(notes) > 10000:
        raise ValueError("The notation editor supports at most 10,000 performance notes.")
    source = copy.deepcopy(notes)
    tempo_info = ({"bpm": settings.tempo_bpm, "source": "manual", "method": "User-selected quarter-note tempo",
                   "detail": "Tempo was supplied manually."} if settings.tempo_bpm is not None else estimate_tempo(source))
    if settings.tempo_bpm is not None and "tempo_bpm" in settings.estimated_fields:
        tempo_info.update(source="estimated", method="Explicitly selected timing-grid estimate",
                          detail="An estimated tempo was selected for this edition; verify against the recording.")
    bpm = Fraction(str(tempo_info["bpm"]))
    grid, bar, pickup = GRIDS[settings.quantization], bar_length(settings.time_signature), Fraction(settings.pickup_beats)
    if settings.melody_quantization == "sixteenth":
        grid = min(grid, GRIDS["sixteenth"])
    origin_seconds = Fraction(str(settings.first_downbeat_seconds)) - pickup * 60 / bpm
    warnings = []
    assumptions = []
    if settings.melody_quantization == "sixteenth":
        assumptions.append("Identified melody and vocal cues use a sixteenth-note grid; accompaniment uses the selected main grid. Review short vocal notes for tracking artifacts.")
    if tempo_info["source"] != "manual":
        assumptions.append(f"Tempo {tempo_info['bpm']:g} BPM is {tempo_info['source']}; confirm it by listening.")
    labels = {"time_signature": f"Time signature {settings.time_signature}",
              "key_signature": f"Key signature {settings.key_signature:+d} fifths",
              "first_downbeat_seconds": f"First full-bar downbeat at {settings.first_downbeat_seconds:g} seconds",
              "pickup_beats": f"Pickup {settings.pickup_beats} quarter-note beats"}
    for name, label in labels.items():
        if name in settings.estimated_fields:
            assumptions.append(label + " is estimated; verify against the recording.")
        elif name not in settings.confirmed_fields:
            assumptions.append(label + " is assumed; it has not been manually confirmed.")
    if settings.clef == "auto":
        assumptions.append("Clef is chosen from the note register and can be changed manually.")
    if settings.piano_split and not accompaniment:
        warnings.append("Treble/bass assignment is a pitch-threshold heuristic, not identification of the performer's hands. Correct individual assignments as needed.")
    if accompaniment:
        warnings.append("Hand assignments are separate from staff and musical voice. Assigned hands guide default staves; staff overrides do not change the physical hand.")
    if settings.first_downbeat_seconds > (clip_duration_seconds or 3600):
        raise ValueError("The first downbeat must lie within the selected audio clip.")
    known_ids = {str(n.get("id", f"event-{i}")) for i, n in enumerate(source)}
    if set(settings.staff_overrides) - known_ids:
        raise ValueError("A staff override refers to a note that is no longer present; reload the performance notes.")
    quantized = []
    pedal_ranges = defaultdict(list)
    staff_placement_changes = []
    extended = 0
    for index, event in enumerate(source):
        identifier = str(event.get("id", f"event-{index}"))
        event["id"] = identifier
        if not all(math.isfinite(float(event[k])) for k in ("start", "end", "pitch")) or event["end"] <= event["start"]:
            raise ValueError("Performance notes must have finite pitches/times and positive duration.")
        pitch = int(event["pitch"]) + settings.transpose_semitones
        if not 0 <= pitch <= 127:
            raise ValueError("Transposition would move a note outside MIDI pitches 0–127.")
        event_grid = note_grid(settings, event)
        start = _snap((Fraction(str(event["start"])) - origin_seconds) * bpm / 60, event_grid)
        physical_end = event["end"]
        if accompaniment and event.get("pedal_sustained") and event.get("key_release_seconds") is not None:
            physical_end = float(event["key_release_seconds"])
            if not event["start"] < physical_end <= event["end"]:
                raise ValueError("Physical key release must follow the attack and not exceed the sounding end.")
        end = _snap((Fraction(str(physical_end)) - origin_seconds) * bpm / 60, event_grid)
        if start < 0:
            raise ValueError("This downbeat/pickup alignment excludes detected notes before the score starts. Move the downbeat earlier or increase the pickup; performance notes were not changed.")
        if end <= start:
            end = start + event_grid
            extended += 1
        is_cue = accompaniment and event.get("role") == "vocal_cue"
        part_id = ("vocal-cue" if is_cue else "piano") if accompaniment else event.get("part_id", "part-1")
        hand_staff = {"left": "bass", "right": "treble"}.get(event.get("hand")) if accompaniment else None
        staff = (settings.staff_overrides.get(identifier) or hand_staff or ("bass" if pitch < settings.split_pitch else "treble")) if settings.piano_split and not is_cue else "staff-1"
        if accompaniment and settings.piano_split and not is_cue and identifier not in settings.staff_overrides:
            opposite = "bass" if staff == "treble" else "treble"
            before_lines, after_lines = _ledger_estimate(pitch, staff), _ledger_estimate(pitch, opposite)
            if before_lines >= 4 and after_lines <= before_lines - 2:
                staff_placement_changes.append({"note_id": identifier, "hand": event.get("hand"),
                    "pitch": pitch, "from_staff": staff, "to_staff": opposite,
                    "estimated_ledger_lines_before": before_lines, "estimated_ledger_lines_after": after_lines,
                    "reason": "Extreme-register readability placement; physical hand, pitch and timing are unchanged."})
                staff = opposite
        if accompaniment and physical_end < event["end"]:
            sounding_end = _snap((Fraction(str(event["end"])) - origin_seconds) * bpm / 60, grid)
            if sounding_end > end:
                pedal_ranges[part_id].append((start, sounding_end))
        quantized.append({"id": identifier, "start": start, "end": end, "pitch": pitch,
                          "velocity": event.get("velocity"), "staff": staff,
                          "part_id": part_id, "voice_id": event.get("voice_id"),
                          "hand": event.get("hand"), "source_part_id": event.get("part_id", "part-1"),
                          "role": event.get("role"), "fingering": event.get("fingering")})
    if extended:
        warnings.append(f"{extended} short note(s) were extended to one grid unit for readable notation; original timing is preserved.")
    pedal_spans = []
    for part_id, ranges in pedal_ranges.items():
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        pedal_spans += [{"part_id": part_id, "start": rational(start), "end": rational(end)} for start, end in merged]
    last = max([item["end"] for item in quantized] + [fraction(item["end"]) for item in pedal_spans], default=pickup or bar)
    full_count = max(1, math.ceil(max(Fraction(0), last - pickup) / bar))
    if full_count > 10000:
        raise ValueError("This score would exceed 10,000 measures. Select a shorter clip or a slower tempo.")
    measure_specs = ([(0, Fraction(0), pickup, True)] if pickup else []) + [
        (i + 1, pickup + i * bar, bar, False) for i in range(full_count)]
    parts = []
    part_ids = sorted({item["part_id"] for item in quantized}, key=lambda value: (value != "vocal-cue", value)) or ["piano" if accompaniment else "part-1"]
    if len(part_ids) > 16:
        raise ValueError("The review score supports at most 16 parts.")
    for part_id in part_ids:
        staves = []
        cue_part = accompaniment and part_id == "vocal-cue"
        staff_ids = ["treble", "bass"] if settings.piano_split and not cue_part else ["staff-1"]
        for staff_id in staff_ids:
            items = [item for item in quantized if item["staff"] == staff_id and item["part_id"] == part_id]
            groups = defaultdict(list)
            for item in items:
                groups[(item["start"], item["end"], item["voice_id"])].append(item)
            groups = [dict(start=start, end=end, notes=group, assigned=assigned) for (start, end, assigned), group in groups.items()]
            coincident = sum(len(group["notes"]) - len({item["pitch"] for item in group["notes"]}) for group in groups)
            if coincident:
                warnings.append(f"{coincident} same-pitch detection(s) share quantized timing on {staff_id} and appear once in the notation; all source IDs and original events remain preserved.")
            groups.sort(key=lambda group: (group["start"], group["end"], min(n["pitch"] for n in group["notes"])))
            lanes = {}
            manual = {group["assigned"] for group in groups if group["assigned"] is not None}
            for voice_id in manual:
                if str(voice_id) not in {str(i) for i in range(1, 33)}:
                    raise ValueError("Voice assignments must be 1–32 or automatic.")
                lane = [group for group in groups if group["assigned"] == voice_id]
                if any(a["end"] > b["start"] for a, b in zip(lane, lane[1:])):
                    raise ValueError(f"Voice {voice_id} in {part_id}/{staff_id} contains overlapping notes with different boundaries. Use separate voices or matching chord timing.")
                lanes[str(voice_id)] = lane
            # Reuse the smallest set of automatic lanes after all notes from
            # the preceding overlap have ended. Register matching against the
            # entire recording otherwise keeps routing isolated later notes
            # through a third voice that was needed only in an earlier chord.
            # This is an interval boundary, not an inferred musical phrase or
            # a barline reset: a sustained line retains its lane across bars.
            overlap_lanes = set()
            overlap_end = Fraction(-1)
            for group in groups:
                if group["assigned"] is not None:
                    continue
                if group["start"] >= overlap_end:
                    overlap_lanes.clear()
                center = statistics.mean(n["pitch"] for n in group["notes"])
                free = [key for key in overlap_lanes if lanes[key][-1]["end"] <= group["start"]]
                if free:
                    chosen = min(free, key=lambda key: (abs(statistics.mean(n["pitch"] for n in lanes[key][-1]["notes"]) - center), int(key)))
                else:
                    chosen = next((str(i) for i in range(1, 33) if str(i) not in manual and str(i) not in overlap_lanes), None)
                    if chosen is None:
                        raise ValueError("More than 32 voices would be needed. Clean overlapping notes or use another part.")
                    lanes.setdefault(chosen, [])
                lanes[chosen].append(group)
                overlap_lanes.add(chosen)
                overlap_end = max(overlap_end, group["end"])
            if not lanes:
                lanes = {"1": []}
            voice_ids = sorted(lanes, key=int)
            # Automatic voices receive stable upper/lower register identities
            # over the whole phrase, rather than switching stem directions at
            # every chord. User-assigned IDs keep their exact identity.
            if accompaniment and not manual and len(voice_ids) > 1:
                ordered = sorted(voice_ids, key=lambda key: -statistics.median(
                    item["pitch"] for group in lanes[key] for item in group["notes"]))
                lanes = {str(index + 1): lanes[key] for index, key in enumerate(ordered)}
                voice_ids = sorted(lanes, key=int)
            voices = [lanes[key] for key in voice_ids]
            if len(voices) > 1:
                warnings.append(f"{len(voices)} voices on {part_id}/{staff_id}; explicit assignments are preserved and other voices use overlap/pitch heuristics.")
            chosen_clef = settings.vocal_clef if cue_part else staff_id if settings.piano_split else settings.clef
            if chosen_clef == "auto":
                chosen_clef = "bass" if items and statistics.median(item["pitch"] for item in items) < 60 else "treble"
            measures = []
            for number, mstart, mlength, is_pickup in measure_specs:
                measure_voices = []
                for voice_index, groups_in_voice in enumerate(voices):
                    events = []
                    cursor = Fraction(0)
                    def rests(start: Fraction, length: Fraction):
                        numerator, denominator = map(int, settings.time_signature.split("/"))
                        group = Fraction(3, 2) if denominator == 8 and numerator > 3 and numerator % 3 == 0 else Fraction(1)
                        for off, dur in _rhythm_segments(start, length, grid, mlength, True, group if accompaniment else None):
                            events.append({"id": _event_id(part_id, staff_id, voice_ids[voice_index], number, "rest", off), "kind": "rest",
                                           "offset": rational(off), "duration": rational(dur), "pitches": [],
                                           "source_note_ids": [], "tie": None})
                    for group in groups_in_voice:
                        if group["end"] <= mstart or group["start"] >= mstart + mlength:
                            continue
                        begin, end = max(group["start"], mstart), min(group["end"], mstart + mlength)
                        offset = begin - mstart
                        if offset > cursor:
                            rests(cursor, offset - cursor)
                        ids = sorted(item["id"] for item in group["notes"])
                        pitches = sorted(set(item["pitch"] for item in group["notes"]))
                        # A chord shares a notated attack/duration, not a dynamic.
                        # Only coincident unisons collapse to one physical key;
                        # their loudest attack wins without boosting other tones.
                        pitch_velocities = {str(pitch): max(
                            item["velocity"] or 64 for item in group["notes"] if item["pitch"] == pitch
                        ) for pitch in pitches}
                        for off, dur in _rhythm_segments(offset, end - begin, grid, mlength):
                            absolute_start = mstart + off
                            before, after = absolute_start > group["start"], absolute_start + dur < group["end"]
                            tie_type = "continue" if before and after else "stop" if before else "start" if after else None
                            events.append({"id": _event_id(*ids, number, off), "kind": "chord" if len(pitches) > 1 else "note",
                                           "offset": rational(off), "duration": rational(dur), "pitches": pitches,
                                           "source_note_ids": ids, "tie": tie_type,
                                           "velocity": max((item["velocity"] or 64 for item in group["notes"]), default=64),
                                           "pitch_velocities": pitch_velocities.copy(),
                                           "hands": {item["id"]: item["hand"] for item in group["notes"] if item.get("hand")},
                                           "hand_pitches": {hand: sorted({item["pitch"] for item in group["notes"] if item.get("hand") == hand}) for hand in ("left", "right")},
                                           "source_parts": {item["id"]: item["source_part_id"] for item in group["notes"]},
                                           "fingerings": {str(item["pitch"]): item["fingering"] for item in group["notes"] if item.get("fingering") is not None}})
                        cursor = end - mstart
                    if cursor < mlength:
                        rests(cursor, mlength - cursor)
                    if sum((fraction(event["duration"]) for event in events), Fraction(0)) != mlength:
                        raise ValueError("Internal notation error: voice does not fill its measure.")
                    measure_voices.append({"id": voice_ids[voice_index], "events": events})
                if accompaniment:
                    # Auto lanes are a layout device, not an instruction to
                    # show a stack of rests while this staff is sounding.
                    # Retain the full timeline in every voice. Only the lead
                    # automatic voice prints staff-wide silent gaps; explicit
                    # user voices preserve all of their intentional rests.
                    sounding = [(fraction(e["offset"]), fraction(e["offset"]) + fraction(e["duration"]))
                                for v in measure_voices for e in v["events"] if e["kind"] != "rest"]
                    automatic_ids = [v["id"] for v in measure_voices if v["id"] not in manual]
                    lead_auto = automatic_ids[0] if automatic_ids else None
                    boundaries = {point for interval in sounding for point in interval}
                    for voice in measure_voices:
                        refined = []
                        for event in voice["events"]:
                            if event["kind"] != "rest":
                                if len(voice_ids) > 1:
                                    event["stem_direction"] = "up" if voice["id"] == voice_ids[0] else "down"
                                refined.append(event)
                                continue
                            if voice["id"] in manual:
                                refined.append(event)
                                continue
                            start = fraction(event["offset"])
                            end = start + fraction(event["duration"])
                            splits = sorted({start, end} | {point for point in boundaries if start < point < end})
                            for left, right in zip(splits, splits[1:]):
                                hidden = bool(manual) or voice["id"] != lead_auto or any(a <= left and b >= right for a, b in sounding)
                                # A split can need a printable decomposition on
                                # a triplet grid; preserve exact total duration.
                                for offset, duration in _rhythm_segments(left, right - left, grid, mlength, True):
                                    refined.append({**event, "id": _event_id(event["id"], offset),
                                                    "offset": rational(offset), "duration": rational(duration), "hidden": hidden})
                        voice["events"] = refined
                        if voice["id"] not in manual:
                            # Merge adjacent automatic rests with the same
                            # visibility before spelling the actual silent gap.
                            # This avoids three quarter rests where a half plus
                            # quarter expresses the same metrical silence.
                            regrouped = []
                            index = 0
                            while index < len(refined):
                                event = refined[index]
                                if event["kind"] != "rest":
                                    regrouped.append(event)
                                    index += 1
                                    continue
                                start, length = fraction(event["offset"]), fraction(event["duration"])
                                next_index = index + 1
                                while next_index < len(refined):
                                    following = refined[next_index]
                                    if following["kind"] != "rest" or following.get("hidden") != event.get("hidden") or fraction(following["offset"]) != start + length:
                                        break
                                    length += fraction(following["duration"])
                                    next_index += 1
                                segments = (_rhythm_segments(start, length, grid, mlength, True) if event.get("hidden")
                                            else _readable_rest_segments(start, length, grid, mlength, settings.time_signature))
                                for offset, duration in segments:
                                    regrouped.append({**event, "id": _event_id(event["id"], "grouped", offset),
                                                      "offset": rational(offset), "duration": rational(duration)})
                                index = next_index
                            voice["events"] = refined = regrouped
                        if sum((fraction(e["duration"]) for e in refined), Fraction(0)) != mlength:
                            raise ValueError("Readability rest grouping changed a voice's duration.")
                canonical_measure = {"number": number, "start": rational(mstart), "duration": rational(mlength),
                                     "is_pickup": is_pickup, "voices": measure_voices}
                if accompaniment:
                    canonical_measure["hand_directions"] = _hand_directions(canonical_measure, staff_id)
                measures.append(canonical_measure)
            staves.append({"id": staff_id, "clef": chosen_clef, "measures": measures})
        part_name = ("Vocal cue" if cue_part else "Piano") if accompaniment else (("Piano" if settings.piano_split else "Transcription") if len(part_ids) == 1 else part_id)
        parts.append({"id": part_id, "name": part_name, "staves": staves, "is_cue": cue_part})
    if settings.transpose_semitones:
        warnings.append("Transposition changes written pitches; the selected key signature describes the written score and is not automatically transposed.")
    if staff_placement_changes:
        warnings.append(f"{len(staff_placement_changes)} extreme-register note(s) use the clearer existing staff, with explicit R.H./L.H. markings. Physical hand assignments and pitches are unchanged; manual staff overrides take priority.")
    result = {"schema_version": 1, "title": title, "beat_unit": "quarter", "settings": settings.model_dump(),
            "tempo": tempo_info, "assumptions": assumptions, "warnings": warnings,
            "source_notes_revision": source_notes_revision, "source_notes": source,
            "origin_seconds": float(origin_seconds), "grid": rational(grid), "bar_duration": rational(bar),
            "parts": parts, "pedal_spans": pedal_spans, "staff_placement_changes": staff_placement_changes}
    if accompaniment:
        from .readability import assess_notation
        result["readability"] = assess_notation(result)
        for issue in result["readability"]["issues"][:20]:
            warnings.append(f"Measure {issue['measure']}, {issue['staff']} staff: {issue['detail']}")
        if len(result["readability"]["issues"]) > 20:
            warnings.append(f"{len(result['readability']['issues']) - 20} more notation concerns are listed in the saved score's readability report.")
    return result


def _separate_crossing_beams(measure):
    """Separate simultaneous equal-direction beams in independent voices.

    Three independent lines can legitimately need two downward stems. A pair
    can produce crossed or overlapping beams when the engraver chooses slopes
    around chords. Pitch averages cannot predict that page geometry. Flag one
    of the simultaneous groups conservatively, retaining the exact rhythm and
    voice identity without moving notes or flipping sustained-line stems.
    """
    groups = []
    for voice in measure.voices:
        current = []
        for element in voice.notes:
            primary = next((beam for beam in element.beams if beam.number == 1), None)
            if primary is None:
                current = []
                continue
            if primary.type == "start":
                current = [element]
            elif current and primary.type in {"continue", "stop"}:
                current.append(element)
                if primary.type == "stop":
                    directions = {n.stemDirection for n in current}
                    if len(directions) == 1 and directions <= {"up", "down"}:
                        groups.append((str(voice.id), current, next(iter(directions))))
                    current = []
            else:
                current = []

    unbeam = set()
    for index, (voice, group, direction) in enumerate(groups):
        for earlier, (other_voice, other, other_direction) in enumerate(groups[:index]):
            if voice == other_voice or direction != other_direction or earlier in unbeam:
                continue
            if max(group[0].offset, other[0].offset) < min(group[-1].offset, other[-1].offset):
                unbeam.add(index)
                break
    for index in unbeam:
        for element in groups[index][1]:
            element.beams = type(element.beams)()


def score_to_stream(score: dict):
    """Translate canonical structure exactly; no inference or timing quantization."""
    from music21 import articulations, chord, clef, duration, expressions, instrument, key, layout, metadata, meter, note, pitch, stream, tempo, tie
    result = stream.Score(id="canonical-score")
    # A single-part transcription has one title, not a separately named movement.
    # An explicit empty movement prevents music21 duplicating the work title.
    readable = score["settings"].get("readability_mode") == "accompaniment"
    result.metadata = metadata.Metadata(title=score["title"], movementName="",
                                        composer=score.get("edition_label") or ("Derived piano accompaniment · review required" if readable else "Audio transcription · review required"))
    if readable:
        result.insert(0, layout.ScoreLayout(scalingMillimeters=7.5, scalingTenths=40))
    clefs = {"treble": clef.TrebleClef, "bass": clef.BassClef, "alto": clef.AltoClef, "tenor": clef.TenorClef}
    signature = key.KeySignature(score["settings"]["key_signature"])
    key_spellings = {p.pitchClass: p.name for p in signature.asKey("major").pitches}
    for part in score["parts"]:
        staff_streams = []
        for staff_index, staff in enumerate(part["staves"]):
            target = stream.PartStaff(id=f"{part['id']}-{staff['id']}") if len(part["staves"]) > 1 else stream.Part(id=part["id"])
            target.partName = part["name"]
            inst = instrument.Piano() if len(part["staves"]) > 1 else instrument.Instrument()
            inst.partId = part["id"]
            inst.partName = part["name"]
            target.insert(0, inst)
            for measure_index, canonical in enumerate(staff["measures"]):
                measure = stream.Measure(number=canonical["number"])
                if measure_index == 0:
                    measure.timeSignature = meter.TimeSignature(score["settings"]["time_signature"])
                    measure.clef = clefs[staff["clef"]]()
                    measure.keySignature = key.KeySignature(score["settings"]["key_signature"])
                    if not readable or (staff_index == 0 and not part.get("is_cue")):
                        measure.insert(0, tempo.MetronomeMark(number=score["tempo"]["bpm"]))
                    if readable:
                        measure.insert(0, layout.StaffLayout(distance=80, staffSize=100))
                if readable and (measure_index == 0 or (canonical["number"] > 1 and (canonical["number"] - 1) % 4 == 0)):
                    # Encoded four-bar phrase guides; the engraver may break
                    # earlier for dense material rather than shrinking staves.
                    measure.insert(0, layout.SystemLayout(isNew=True, distance=130))
                if canonical["is_pickup"]:
                    measure.paddingLeft = fraction(score["bar_duration"]) - fraction(canonical["duration"])
                written_names = {}
                for canonical_voice in canonical["voices"]:
                    voice = stream.Voice(id=canonical_voice["id"])
                    for event in canonical_voice["events"]:
                        if event["kind"] == "rest":
                            element = note.Rest()
                            # A full-bar rest encodes the time signature's length
                            # in MusicXML. A silent pickup must retain its shorter
                            # explicit duration, including on an empty piano staff.
                            element.fullMeasure = (not canonical["is_pickup"] and
                                fraction(event["duration"]) == fraction(canonical["duration"]))
                            if event.get("hidden"):
                                element.style.hideObjectOnPrint = True
                        elif event["kind"] == "chord":
                            element = chord.Chord(event["pitches"])
                        else:
                            element = note.Note(event["pitches"][0])
                        # XML IDs cannot begin with a digit. Canonical UUIDs
                        # often do, so give every stable exported ID a prefix.
                        element.id = f"score-{event['id']}"
                        element.duration = duration.Duration(fraction(event["duration"]))
                        if event["kind"] != "rest":
                            element.volume.velocity = event.get("velocity", 64)
                            if event.get("tie"):
                                element.tie = tie.Tie(event["tie"])
                            if event.get("stem_direction"):
                                element.stemDirection = event["stem_direction"]
                            for member in element.notes if isinstance(element, chord.Chord) else [element]:
                                member.volume.velocity = _pitch_velocity(event, int(member.pitch.midi))
                                if isinstance(element, chord.Chord):
                                    member.id = f"score-{event['id']}-p{int(member.pitch.midi)}"
                                fingering = event.get("fingerings", {}).get(str(int(member.pitch.midi)))
                                if fingering is not None and event.get("tie") not in {"continue", "stop"}:
                                    if not isinstance(fingering, int) or not 1 <= fingering <= 5:
                                        raise ValueError("Manual piano fingering must be a finger number from 1 to 5.")
                                    # Chord fingerings are applied by exact
                                    # member ID below: music21 otherwise maps
                                    # positional chord articulations and cannot
                                    # preserve a partially specified chord.
                                    if not isinstance(element, chord.Chord):
                                        member.articulations.append(articulations.Fingering(fingering))
                            if part.get("is_cue"):
                                element.style.noteSize = "cue"
                            # Prefer diatonic spellings in the selected signature,
                            # including C-flat/B-sharp octave boundaries. Outside
                            # it, use the signature's flat/sharp orientation.
                            for written_pitch in element.pitches:
                                midi = written_pitch.midi
                                if written_pitch.pitchClass in key_spellings:
                                    replacement = pitch.Pitch(key_spellings[written_pitch.pitchClass])
                                    replacement.octave = written_pitch.octave
                                    while replacement.midi < midi:
                                        replacement.octave += 1
                                    while replacement.midi > midi:
                                        replacement.octave -= 1
                                    written_pitch.name = replacement.name
                                    written_pitch.octave = replacement.octave
                                elif score["settings"]["key_signature"] < 0 and written_pitch.accidental and written_pitch.accidental.alter > 0:
                                    written_pitch.getEnharmonic(inPlace=True)
                                written_names[int(written_pitch.midi)] = written_pitch.nameWithOctave.replace("-", "b")
                        voice.insert(fraction(event["offset"]), element)
                    measure.insert(0, voice)
                if readable:
                    directions = canonical.get("hand_directions", _hand_directions(canonical, staff["id"]))
                    for direction in directions:
                        labels = []
                        for hand in ("right", "left"):
                            if hand not in direction["hands"]:
                                continue
                            label = "R.H." if hand == "right" else "L.H."
                            if direction["named_pitches"]:
                                label += ": " + ", ".join(written_names[p] for p in direction["hands"][hand])
                            labels.append(label)
                        indication = expressions.TextExpression("; ".join(labels))
                        indication.placement = "above" if staff["id"] == "bass" else "below"
                        indication.style.fontStyle = "italic"
                        indication.style.fontSize = 8
                        measure.insert(fraction(direction["offset"]), indication)
                    if measure_index == 0 and staff["id"] == "bass" and any(
                        _hand_directions(m, s["id"]) for s in part["staves"] for m in s["measures"]
                    ):
                        legend = expressions.TextExpression("Hand labels apply until changed or the barline; named pitches only when shown.")
                        legend.placement = "below"
                        legend.style.fontSize = 7
                        measure.insert(0, legend)
                # Attach first so later measures inherit the initial meter when
                # music21 derives beams from the already fixed event durations.
                target.insert(fraction(canonical["start"]), measure)
                measure.makeBeams(inPlace=True, setStemDirections=not readable, failOnNoTimeSignature=False)
                if readable:
                    _separate_crossing_beams(measure)
                stream.makeNotation.makeTupletBrackets(measure, inPlace=True)
            staff_streams.append(target)
            result.insert(0, target)
        if len(staff_streams) > 1:
            result.insert(0, layout.StaffGroup(staff_streams, name=part["name"], symbol="brace", barTogether=True))
    return result


def score_to_musicxml(score: dict) -> str:
    with tempfile.TemporaryDirectory(prefix="score-musicxml-") as directory:
        path = Path(directory) / "score.musicxml"
        score_to_stream(score).write("musicxml", fp=str(path))
        xml = path.read_text()
    xml = xml.replace("  <movement-title />\n", "")
    if Fraction(score["settings"]["pickup_beats"]):
        xml = xml.replace('<measure implicit="no" number="0">', '<measure implicit="yes" number="0">')
    manual_fingers = {}
    pitch_dynamics = {}
    for part_index, part in enumerate(score["parts"]):
        for staff_index, staff in enumerate(part["staves"]):
            for measure_index, measure in enumerate(staff["measures"]):
                for voice in measure["voices"]:
                    for event in voice["events"]:
                        if event["kind"] != "rest" and event.get("pitch_velocities"):
                            for pitch in event["pitches"]:
                                identifier = f"score-{event['id']}-p{pitch}" if event["kind"] == "chord" else f"score-{event['id']}"
                                pitch_dynamics[identifier] = _pitch_velocity(event, pitch)
                        if event["kind"] == "chord" and event.get("tie") not in {"continue", "stop"}:
                            manual_fingers.update({(part_index, staff_index + 1, measure_index, fraction(event["offset"]), int(pitch)): finger
                                                   for pitch, finger in event.get("fingerings", {}).items()})
    if score.get("pedal_spans") or manual_fingers or pitch_dynamics:
        # Pedal endpoints can occur in rests or at a measure end, not only on
        # note heads. Explicit MusicXML offsets preserve those exact positions.
        from lxml import etree
        document = etree.fromstring(xml.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
        # music21's chord exporter uses the chord-parent volume for every
        # member. MusicXML supports per-note dynamics (90 MIDI velocity = 100),
        # so restore the canonical member value by its stable exported ID.
        for node in document.findall(".//note"):
            if node.get("id") in pitch_dynamics:
                node.set("dynamics", f"{pitch_dynamics[node.get('id')] * 100 / 90:.6f}")
        for part_index, xml_part in enumerate(document.findall("part")):
            divisions = 10080
            for measure_index, xml_measure in enumerate(xml_part.findall("measure")):
                cursor, previous_attack = Fraction(0), Fraction(0)
                for node in xml_measure:
                    if node.tag == "attributes" and node.findtext("divisions"):
                        divisions = int(node.findtext("divisions"))
                    elif node.tag in {"backup", "forward"}:
                        cursor += (-1 if node.tag == "backup" else 1) * Fraction(int(node.findtext("duration")), divisions)
                    elif node.tag == "note":
                        onset = previous_attack if node.find("chord") is not None else cursor
                        if node.find("chord") is None:
                            previous_attack = onset
                            cursor += Fraction(int(node.findtext("duration", "0")), divisions)
                        pitch_node = node.find("pitch")
                        if pitch_node is None:
                            continue
                        midi = (int(pitch_node.findtext("octave")) + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[pitch_node.findtext("step")] + int(pitch_node.findtext("alter", "0"))
                        finger = manual_fingers.get((part_index, int(node.findtext("staff", "1")), measure_index, onset, midi))
                        if finger is None:
                            continue
                        notations = node.find("notations")
                        if notations is None:
                            notations = etree.SubElement(node, "notations")
                        technical = notations.find("technical")
                        if technical is None:
                            technical = etree.SubElement(notations, "technical")
                        etree.SubElement(technical, "fingering").text = str(finger)
        for canonical_part, xml_part in zip(score["parts"], document.findall("part")):
            measures = canonical_part["staves"][-1]["measures"]
            xml_measures = xml_part.findall("measure")
            divisions = 10080
            for span in score.get("pedal_spans", []):
                if span["part_id"] != canonical_part["id"]:
                    continue
                for kind, absolute in (("start", fraction(span["start"])), ("stop", fraction(span["end"]))):
                    for index, measure in enumerate(measures):
                        begin = fraction(measure["start"])
                        end = begin + fraction(measure["duration"])
                        if begin <= absolute < end or (kind == "stop" and absolute == end):
                            node = xml_measures[index]
                            for earlier in xml_measures[:index + 1]:
                                value = earlier.findtext("attributes/divisions")
                                if value:
                                    divisions = int(value)
                            direction = etree.Element("direction", placement="below")
                            types = etree.SubElement(direction, "direction-type")
                            etree.SubElement(types, "pedal", type=kind, line="yes", sign="no", number="1")
                            etree.SubElement(direction, "offset").text = str(int((absolute - begin) * divisions))
                            etree.SubElement(direction, "staff").text = str(len(canonical_part["staves"]))
                            etree.SubElement(direction, "sound", **{"damper-pedal": "yes" if kind == "start" else "no"})
                            first_note = node.find("note")
                            node.insert(node.index(first_note) if first_note is not None else len(node), direction)
                            break
        xml = etree.tostring(document, encoding="utf-8", xml_declaration=True).decode()
    return xml


def _pitch_velocity(event: dict, pitch: int) -> int:
    """Read current per-pitch dynamics, with compatibility for saved old scores."""
    value = event.get("pitch_velocities", {}).get(str(pitch))
    return int(value if value is not None else event.get("velocity") or 64)


def write_score_midi(score: dict, path: Path) -> None:
    """Notation MIDI is compiled from canonical beats, including tied durations."""
    import pretty_midi
    from .performance import write_midi_object
    seconds_per_beat = 60 / score["tempo"]["bpm"]
    sounding = {}
    for part in score["parts"]:
        for staff in part["staves"]:
            for measure in staff["measures"]:
                for voice in measure["voices"]:
                    for event in voice["events"]:
                        if event["kind"] == "rest":
                            continue
                        start = fraction(measure["start"]) + fraction(event["offset"])
                        end = start + fraction(event["duration"])
                        for pitch in event["pitches"]:
                            key = (part["id"], staff["id"], voice["id"], tuple(event["source_note_ids"]), pitch)
                            if event.get("tie") in ("continue", "stop") and key in sounding:
                                sounding[key]["end"] = float(end) * seconds_per_beat
                            else:
                                sounding[key] = {"pitch": pitch, "start": float(start) * seconds_per_beat,
                                                 "end": float(end) * seconds_per_beat,
                                                 "velocity": _pitch_velocity(event, pitch), "part_id": part["id"]}
    midi = pretty_midi.PrettyMIDI(initial_tempo=score["tempo"]["bpm"], resolution=10080)
    numerator, denominator = map(int, score["settings"]["time_signature"].split("/"))
    midi.time_signature_changes.append(pretty_midi.TimeSignature(numerator, denominator, 0))
    # MIDI key numbers are pitch classes 0–11 for major signatures. The UI
    # chooses a signature, not a tonic/mode, so do not invent a minor-mode claim.
    midi.key_signature_changes.append(pretty_midi.KeySignature((score["settings"]["key_signature"] * 7) % 12, 0))
    for part in score["parts"]:
        if part.get("is_cue"):
            continue
        # MIDI note-off messages identify a channel and pitch, not a score
        # voice. Overlapping unisons on one channel would terminate the longer
        # note early. Color only conflicting pitch intervals into separate
        # channels; unrelated pitches can still share a piano track.
        lanes = []
        for event in sorted((e for e in sounding.values() if e["part_id"] == part["id"]),
                            key=lambda e: (e["start"], e["end"], e["pitch"])):
            lane = next((lane for lane in lanes if lane["ends"].get(event["pitch"], -1) <= event["start"] + 1e-9), None)
            if lane is None:
                lane = {"ends": {}, "notes": []}
                lanes.append(lane)
            lane["ends"][event["pitch"]] = event["end"]
            lane["notes"].append(event)
        if len(midi.instruments) + len(lanes) > 15:
            raise ValueError("This score needs more than 15 independent MIDI channels to preserve overlapping same-pitch voices. Export MusicXML or reduce overlapping unisons; no notes were truncated.")
        for index, lane in enumerate(lanes):
            name = part["name"] + (f" - overlap layer {index + 1}" if len(lanes) > 1 else "")
            instrument = pretty_midi.Instrument(program=0, name=name)
            instrument.notes = [pretty_midi.Note(velocity=int(event["velocity"] or 64),
                                pitch=event["pitch"], start=event["start"], end=event["end"]) for event in lane["notes"]]
            instrument.control_changes = [pretty_midi.ControlChange(number=64, value=value,
                                          time=float(fraction(span[position])) * seconds_per_beat)
                                          for span in score.get("pedal_spans", []) if span["part_id"] == part["id"]
                                          for position, value in (("start", 127), ("end", 0))]
            midi.instruments.append(instrument)
    write_midi_object(midi, path)

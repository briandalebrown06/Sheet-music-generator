"""Unquantized editable note data and conservative, reproducible cleanup.

Model scores are activation values, not calibrated accuracy probabilities.
Original note-event tuples remain in a separate immutable artifact. Cleanup
never changes the original timing snapshot of a surviving detected note.
"""
from __future__ import annotations

import math
import uuid
from copy import deepcopy
from typing import Any, TypedDict


class NoteEvent(TypedDict, total=False):
    id: str
    pitch: int
    start: float
    end: float
    velocity: int | None
    part_id: str
    voice_id: str | None
    model_score: float | None
    provenance: str
    source_note_ids: list[str]
    original_timing: dict[str, Any] | None
    score_timing: dict[str, Any] | None


DEFAULT_CLEANUP = {"minimum_note_seconds": 0.08, "merge_gap_seconds": 0.03}


def cleanup_settings(value: dict | None = None) -> dict[str, float]:
    result = dict(DEFAULT_CLEANUP)
    if value:
        unknown = set(value) - set(result)
        if unknown:
            raise ValueError(f"Unknown cleanup option: {sorted(unknown)[0]}")
        result.update(value)
    for key, maximum in (("minimum_note_seconds", 1.0), ("merge_gap_seconds", 0.2)):
        number = float(result[key])
        if not math.isfinite(number) or not 0 <= number <= maximum:
            raise ValueError(f"{key} must be between 0 and {maximum} seconds.")
        result[key] = number
    return result


def raw_note_id(namespace: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"scorecraft:{namespace}:raw-note:{index}"))


def detected_notes(events: list, namespace: str, clip_duration: float | None = None) -> list[NoteEvent]:
    """Give each real raw event a deterministic ID without quantizing its time."""
    notes: list[NoteEvent] = []
    for index, event in enumerate(events):
        start, end, pitch, amplitude = event[:4]
        if not all(math.isfinite(float(v)) for v in (start, end, pitch, amplitude)):
            continue
        start, end, pitch, amplitude = float(start), float(end), int(pitch), float(amplitude)
        if start < 0 or end <= start or not 0 <= pitch <= 127:
            continue
        # This is an amplitude-derived MIDI velocity, not measured playing force.
        velocity = max(1, min(127, round(amplitude * 127)))
        identifier = raw_note_id(namespace, index)
        original = {"start": start, "end": end, "pitch": pitch, "velocity": velocity}
        if clip_duration is not None:
            end = min(end, clip_duration)
            if end <= start:
                continue
        notes.append({
            "id": identifier, "pitch": pitch, "start": start, "end": end,
            "velocity": velocity, "part_id": "part-1", "model_score": amplitude,
            "provenance": "detected", "source_note_ids": [identifier],
            "original_timing": original, "score_timing": None,
        })
    return sorted(notes, key=lambda item: (item["start"], item["pitch"], item["end"], item["id"]))


def _continuous_without_onset(previous: NoteEvent, current: NoteEvent,
                              model_output: dict | None, frame_times: Any) -> bool:
    """Require model evidence before joining two distinct same-pitch events.

    A positive gap alone is never sufficient. Both overlapping and adjacent
    fragments must have sustained pitch activation and no strong local onset.
    Without activations or frame alignment we keep the events separate.
    """
    if model_output is None or frame_times is None or len(frame_times) == 0:
        return False
    import numpy as np
    pitch_index = current["pitch"] - 21  # Basic Pitch's A0-based 88-bin output.
    frames, onsets = model_output.get("note"), model_output.get("onset")
    if frames is None or onsets is None or not 0 <= pitch_index < frames.shape[1]:
        return False
    index = int(np.searchsorted(frame_times, current["start"], side="left"))
    index = min(len(frame_times) - 1, index)
    onset_left, onset_right = max(0, index - 1), min(len(frame_times), index + 2)
    # Lower than the extraction threshold (0.5): conservative repeat protection.
    if float(np.max(onsets[onset_left:onset_right, pitch_index])) >= 0.2:
        return False
    start = min(previous["end"], current["start"])
    bridge_left = max(0, int(np.searchsorted(frame_times, start)) - 1)
    bridge_right = min(len(frame_times), index + 2)
    bridge = frames[bridge_left:bridge_right, pitch_index]
    if len(bridge) == 0 or float(np.min(bridge)) < 0.3:
        return False
    # A frame-activation rise can indicate rearticulation even with weak onsets.
    local = frames[max(0, index - 2):min(len(frame_times), index + 2), pitch_index]
    if len(local) > 1 and float(np.max(np.diff(local))) >= 0.15:
        return False
    return True


def cleanup_notes(events: list, namespace: str, cleanup: dict | None = None,
                  model_output: dict | None = None, frame_times: Any = None,
                  clip_duration: float | None = None) -> tuple[list[NoteEvent], dict]:
    """Return editable notes and an audit of filtering/merges, never mutate raw data."""
    options = cleanup_settings(cleanup)
    detected = detected_notes(events, namespace, clip_duration)
    retained, removed = [], []
    for item in detected:
        if item["end"] - item["start"] + 1e-12 >= options["minimum_note_seconds"]:
            retained.append(item)
        else:
            removed.append(item["id"])
    grouped: dict[tuple[str, int], list[NoteEvent]] = {}
    for item in retained:
        grouped.setdefault((item["part_id"], item["pitch"]), []).append(deepcopy(item))
    output: list[NoteEvent] = []
    for notes in grouped.values():
        merged: list[NoteEvent] = []
        for item in notes:
            previous = merged[-1] if merged else None
            if previous and options["merge_gap_seconds"] > 0 and (
                item["start"] - previous["end"] <= options["merge_gap_seconds"] + 1e-12
                and _continuous_without_onset(previous, item, model_output, frame_times)
            ):
                previous["end"] = max(previous["end"], item["end"])
                previous["velocity"] = max(previous["velocity"] or 1, item["velocity"] or 1)
                previous["model_score"] = max(previous["model_score"] or 0, item["model_score"] or 0)
                previous["source_note_ids"].extend(item["source_note_ids"])
                # Keep the earliest ID stable; all raw IDs remain inspectable.
            else:
                merged.append(item)
        output.extend(merged)
    merged_groups = [list(item["source_note_ids"]) for item in output if len(item["source_note_ids"]) > 1]
    output.sort(key=lambda item: (item["start"], item["pitch"], item["end"], item["id"]))
    return output, {
        "settings": options, "raw_valid_note_count": len(detected),
        "removed_short_note_ids": removed, "merged_source_note_groups": merged_groups,
        "normalized_note_count": len(output),
        "merge_rule": "same pitch/part, bounded gap or overlap, continuous frame activation >=0.3, boundary onset <0.2, local frame rise <0.15",
    }

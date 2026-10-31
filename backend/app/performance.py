"""MIDI exports with performance seconds preserved and no score quantization."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from .notes import NoteEvent


def write_performance_midi(notes: list[NoteEvent], path: Path) -> None:
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(initial_tempo=120, resolution=10000)
    parts: dict[str, list] = {}
    for event in notes:
        part_id = event.get("part_id", "part-1")
        if part_id not in parts:
            parts[part_id] = []
        velocity = event.get("velocity")
        parts[part_id].append(pretty_midi.Note(
            velocity=64 if velocity is None else max(1, min(127, int(velocity))),
            pitch=int(event["pitch"]), start=float(event["start"]), end=float(event["end"]),
        ))
    for part_id, part_notes in parts.items():
        # On one MIDI channel a note-off closes the pitch, not its originating
        # voice. Independent overlapping unisons therefore need separate
        # channels even in a piano-only performance export. Keep exact seconds.
        lanes = []
        for note in sorted(part_notes, key=lambda note: (note.start, note.end, note.pitch)):
            lane = next((lane for lane in lanes if lane["ends"].get(note.pitch, -1) <= note.start + 1e-9), None)
            if lane is None:
                lane = {"ends": {}, "notes": []}
                lanes.append(lane)
            lane["ends"][note.pitch] = note.end
            lane["notes"].append(note)
        if len(midi.instruments) + len(lanes) > 15:
            raise ValueError("This performance needs more than 15 independent MIDI channels to preserve overlapping same-pitch voices. Reduce overlapping unisons or export MusicXML; no notes were truncated.")
        for index, lane in enumerate(lanes):
            name = part_id + (f" - overlap layer {index + 1}" if len(lanes) > 1 else "")
            instrument = pretty_midi.Instrument(program=0, name=name)
            instrument.notes = lane["notes"]
            midi.instruments.append(instrument)
    write_midi_object(midi, path)


def write_midi_object(midi, path: Path) -> None:
    """Atomically publish a MIDI file, including an empty performance."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            midi.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

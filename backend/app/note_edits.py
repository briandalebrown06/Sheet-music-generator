"""Validated performance edits with server-owned provenance and stable identities."""
from copy import deepcopy
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class NoteEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: uuid.UUID | None = None
    pitch: int = Field(ge=0, le=127)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    velocity: int | None = Field(default=None, ge=1, le=127)
    part_id: str = Field(default="part-1", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    voice_id: str | None = Field(default=None, pattern=r"^(?:[1-9]|[12][0-9]|3[0-2])$")
    new_note: bool = False
    derived_from_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10000)
    hand: Literal["left", "right"] | None = None
    locked: bool | None = None
    key_release_seconds: float | None = Field(default=None, ge=0)
    pedal_sustained: bool = False
    role: Literal["bass", "harmony", "riff", "countermelody", "melody", "texture", "vocal_cue"] | None = None
    fingering: int | None = Field(default=None, ge=1, le=5)


def normalize_edits(project: dict, proposed_notes: list[NoteEdit], registry: dict | None = None) -> list[dict]:
    duration = project["clip_duration_seconds"] or project["duration_seconds"]
    if duration is None:
        raise HTTPException(409, "The decoded clip duration is unavailable. Transcribe audio before editing.")
    existing = {**(registry or {}), **{note["id"]: note for note in project["notes"] or []}}
    notes, seen = [], set()
    for proposed in proposed_notes:
        if proposed.end <= proposed.start or proposed.end > duration + 0.001:
            raise HTTPException(422, "Each note must end after it starts and remain within the selected audio clip.")
        identifier = str(proposed.id) if proposed.id else str(uuid.uuid4())
        if identifier in seen or (proposed.id and identifier not in existing and not proposed.new_note):
            raise HTTPException(422, "Note IDs must be unique existing IDs, or marked as a new manually added note.")
        seen.add(identifier)
        parents = [existing.get(str(value)) for value in proposed.derived_from_ids]
        if any(parent is None for parent in parents):
            raise HTTPException(422, "An edit refers to a source note that is no longer available. Reload the project.")
        previous = existing.get(identifier)
        values = {"pitch": proposed.pitch, "start": proposed.start, "end": min(proposed.end, duration),
                  "velocity": proposed.velocity, "part_id": proposed.part_id, "voice_id": proposed.voice_id}
        for field in ("hand", "locked", "key_release_seconds", "pedal_sustained", "role", "fingering"):
            if field in proposed.model_fields_set:
                values[field] = getattr(proposed, field)
        if previous:
            note = deepcopy(previous)
            if any(note.get(key) != value for key, value in values.items()) and note["provenance"] != "manually_added":
                note["provenance"] = "edited"
            note.update(values)
        else:
            note = {"id": identifier, **values, "model_score": None, "provenance": "manually_added",
                    "source_note_ids": [], "original_timing": None, "score_timing": None}
        if parents:
            note["source_note_ids"] = sorted({value for parent in parents + ([previous] if previous else [])
                                             for value in parent.get("source_note_ids", [])})
            if not previous:
                note["original_timing"] = deepcopy(parents[0].get("original_timing"))
                note["model_score"] = None
            note["provenance"] = "edited" if note["source_note_ids"] else "manually_added"
        release = note.get("key_release_seconds")
        if release is not None and not note["start"] < release <= note["end"]:
            raise HTTPException(422, "Physical key release must fall after the attack and no later than the sound's end.")
        if note.get("pedal_sustained") and release is None:
            raise HTTPException(422, "Specify the physical key release before marking a note as sustained by pedal.")
        notes.append(note)
    return sorted(notes, key=lambda item: (item["start"], item["pitch"], item["id"]))

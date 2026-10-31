from __future__ import annotations

import logging
from pathlib import Path

from .engine import NoteEvent, atomic_json, package_available
from .performance import write_performance_midi
from .score import build_score, default_score_settings, score_to_musicxml


def build_exports(notes: list[NoteEvent], directory: Path, title: str, *,
                  tempo_hint: int | None = None, time_signature_hint: str | None = None,
                  instrument_hint: str | None = None, include_midi: bool = True) -> list[str]:
    """Compatibility entrypoint: canonical MusicXML plus performance MIDI.

    The separate score API provides notation MIDI from the canonical model.
    This existing worker export deliberately keeps original performance seconds.
    """
    if not notes or not package_available("music21"):
        return []
    available = set()
    if include_midi:
        try:
            write_performance_midi(notes, directory / "score.mid")
            available.add("midi")
        except Exception:
            logging.getLogger(__name__).exception("MIDI export failed; retaining raw note events")
    try:
        settings = default_score_settings({"tempo_hint": tempo_hint, "time_signature_hint": time_signature_hint})
        score = build_score(notes, settings, title=title, source_notes_revision=1)
        if instrument_hint:
            score["parts"][0]["name"] = instrument_hint.capitalize()
        xml = score_to_musicxml(score)
        atomic_json(directory / "canonical-score.json", score)
        (directory / "score.musicxml").write_text(xml)
        available.add("musicxml")
    except Exception:
        logging.getLogger(__name__).exception("MusicXML export failed; retaining other successful exports")
    return [format for format in ("musicxml", "midi") if format in available]

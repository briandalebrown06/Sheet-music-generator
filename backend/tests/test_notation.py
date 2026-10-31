"""Optional music21 integration checks. Events below are labeled test fixtures."""
import pytest

music21 = pytest.importorskip("music21")

from backend.app.notation import build_exports


@pytest.mark.parametrize("events", [
    [{"start": 0.25, "end": 0.75, "pitch": 60, "velocity": 90},
     {"start": 1.0, "end": 1.5, "pitch": 64, "velocity": 80}],
    [{"start": 0.0, "end": 1.5, "pitch": 60, "velocity": 90},
     {"start": 0.25, "end": 0.75, "pitch": 64, "velocity": 80},
     {"start": 1.0, "end": 2.5, "pitch": 67, "velocity": 75}],
])
def test_real_musicxml_and_midi_roundtrip(tmp_path, events):
    assert build_exports(events, tmp_path, "TEST FIXTURE — export roundtrip") == ["musicxml", "midi"]
    xml = music21.converter.parse(tmp_path / "score.musicxml")
    midi = music21.converter.parse(tmp_path / "score.mid", quantizePost=False)
    expected = {event["pitch"] for event in events}
    assert {p.midi for n in xml.recurse().notes for p in n.pitches} == expected
    assert {p.midi for n in midi.recurse().notes for p in n.pitches} == expected
    assert (tmp_path / "score.mid").read_bytes().startswith(b"MThd")
    assert "TEST FIXTURE" in (tmp_path / "score.musicxml").read_text()


def test_musicxml_failure_preserves_midi(tmp_path, monkeypatch):
    events = [{"start": 0.0, "end": 0.5, "pitch": 60, "velocity": 90}]
    original = music21.stream.Score.write
    def fail_xml(self, fmt, *args, **kwargs):
        if fmt == "musicxml":
            raise RuntimeError("TEST FIXTURE — engraving failure")
        return original(self, fmt, *args, **kwargs)
    monkeypatch.setattr(music21.stream.Score, "write", fail_xml)
    available = build_exports(events, tmp_path, "TEST FIXTURE — partial export")
    assert available == ["midi"]
    assert (tmp_path / "score.mid").read_bytes().startswith(b"MThd")


def test_user_hints_affect_notation_without_changing_midi_seconds(tmp_path):
    events = [{"start": 0.5, "end": 1.0, "pitch": 60, "velocity": 90}]
    assert build_exports(events, tmp_path, "TEST FIXTURE — hinted score", tempo_hint=90,
                         time_signature_hint="3/4", instrument_hint="voice") == ["musicxml", "midi"]
    xml = music21.converter.parse(tmp_path / "score.musicxml")
    assert xml.parts[0].partName == "Voice"
    assert xml.recurse().getElementsByClass(music21.meter.TimeSignature).first().ratioString == "3/4"
    assert xml.recurse().getElementsByClass(music21.tempo.MetronomeMark).first().number == 90
    midi = music21.converter.parse(tmp_path / "score.mid", quantizePost=False)
    sounding = [item for item in midi.flatten().secondsMap if isinstance(item["element"], music21.note.Note)]
    assert len(sounding) == 1
    assert sounding[0]["offsetSeconds"] == pytest.approx(0.5, abs=0.002)
    assert sounding[0]["durationSeconds"] == pytest.approx(0.5, abs=0.002)

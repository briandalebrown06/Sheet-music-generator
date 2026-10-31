"""Long-file plumbing regressions with explicitly injected UNIT TEST tensors.

These tensors test coverage, checkpoint reuse and boundary assembly, never
model accuracy. Real model tests and the 720-second evaluation run separately.
"""
from __future__ import annotations

import math
import os
import struct
import time
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from backend.app.analysis_checkpoints import CheckpointStore, cleanup_analysis_checkpoints
from backend.app.audio import AnalysisError
from backend.app.engine import BasicPitchEngine
from backend.app.transcription_profiles import PyinPartEngine
from backend.tests.test_separation_adapter import audio_fixture, settings, tensor_fixture


def test_checkpoint_identity_and_corruption_are_recoverable(tmp_path):
    source = tmp_path / "EXPLICIT-TEST-audio"
    source.write_bytes(b"audio content identity fixture")
    first = CheckpointStore(tmp_path / "checkpoints", source, {"engine": "explicit-unit-test", "version": 1})
    first.save(0, {"frames": np.arange(10, dtype="float32")})
    again = CheckpointStore(tmp_path / "checkpoints", source, {"engine": "explicit-unit-test", "version": 1})
    np.testing.assert_array_equal(again.load(0, {"frames": (10,)})["frames"], np.arange(10))
    assert again.hits == 1
    assert again.load(0, {"frames": (9,)}) is None
    assert CheckpointStore(tmp_path / "checkpoints", source, {"engine": "explicit-unit-test", "version": 2}).load(0, {"frames": (10,)}) is None
    source.write_bytes(b"different audio content identity fixture")
    assert CheckpointStore(tmp_path / "checkpoints", source, {"engine": "explicit-unit-test", "version": 1}).load(0, {"frames": (10,)}) is None
    (first.directory / "window-0000000.npz").write_bytes(b"PK\x03\x04interrupted zip fixture")
    assert first.load(0, {"frames": (10,)}) is None
    first.save(0, {"frames": np.ones(10)})
    assert first.load(0, {"frames": (10,)}) is not None
    assert not list(first.directory.glob("*.tmp"))


def test_checkpoint_damaged_deflate_payload_is_recomputed(tmp_path):
    """A valid ZIP header can still contain an invalid compressed array body."""
    audio = tmp_path / "EXPLICIT-TEST-audio"
    audio.write_bytes(b"explicit checkpoint identity fixture")
    store = CheckpointStore(tmp_path / "checkpoints", audio, {"engine": "explicit-unit-test"})
    expected = {"frames": np.arange(10, dtype="float32")}
    store.save(0, expected)
    path = store.directory / "window-0000000.npz"
    payload = bytearray(path.read_bytes())
    filename_length, extra_length = struct.unpack_from("<HH", payload, 26)
    compressed_offset = 30 + filename_length + extra_length
    # DEFLATE block type 3 is reserved: numpy/zipfile raises zlib.error, not
    # BadZipFile. Treat this cache as absent so retries can regenerate it.
    payload[compressed_offset] = 0x07
    path.write_bytes(payload)
    assert store.load(0, {"frames": (10,)}) is None
    store.save(0, expected)
    np.testing.assert_array_equal(store.load(0, {"frames": (10,)})["frames"], expected["frames"])


def test_basic_pitch_streamed_global_timeline_resumes_and_preserves_tail(tmp_path, monkeypatch):
    pytest.importorskip("basic_pitch")
    import librosa
    from basic_pitch.constants import AUDIO_N_SAMPLES, FFT_HOP

    rate, frames = 22050, 7 * 22050 + 137
    source = tmp_path / "EXPLICIT-UNIT-TEST.wav"
    sf.write(source, np.zeros(frames), rate, subtype="PCM_16")
    hop, core = AUDIO_N_SAMPLES - 30 * FFT_HOP, 142
    windows = math.ceil(frames / hop)
    count = (windows - 1) * core + min(core, math.ceil((frames - (windows - 1) * hop) / FFT_HOP))
    timeline = np.zeros((count + 172, 88), dtype="float32")
    onsets = np.zeros_like(timeline)
    # Sustain crosses the first model seam; same-pitch reattacks cross the next.
    for start, end, pitch in ((100, 250, 60), (270, 290, 64), (300, 330, 64), (count - 40, count - 4, 72)):
        timeline[start:end, pitch - 21] = .9
        onsets[start, pitch - 21] = .9

    class ExplicitModelTensorFixture:
        def __init__(self, first_index):
            self.index, self.calls = first_index, 0

        def predict(self, audio):
            assert audio.shape == (1, AUDIO_N_SAMPLES, 1)
            result = {"note": np.zeros((1, 172, 88), dtype="float32"),
                      "onset": np.zeros((1, 172, 88), dtype="float32"),
                      "contour": np.zeros((1, 172, 264), dtype="float32")}
            left = self.index * core
            result["note"][0, 15:-15] = timeline[left:left + core]
            result["onset"][0, 15:-15] = onsets[left:left + core]
            self.calls += 1
            self.index += 1
            return result

    monkeypatch.setattr(librosa, "load", lambda *args, **kwargs: pytest.fail("Full-file audio loading is not permitted"))
    checkpoint_root = tmp_path / "checkpoint-root"
    first = BasicPitchEngine(checkpoint_dir=checkpoint_root)
    first_model = ExplicitModelTensorFixture(0)
    monkeypatch.setattr(first, "_load_model", lambda: first_model)

    def interrupted(label):
        if "window 3 of" in label:
            raise RuntimeError("Explicit simulated interrupted attempt")

    with pytest.raises(AnalysisError, match="checkpoints are retained"):
        first.analyze(source, tmp_path / "interrupted", "first-test-attempt", stage_callback=interrupted)
    assert first_model.calls == 2
    assert len(list(checkpoint_root.rglob("window-*.npz"))) == 2
    assert not (tmp_path / "interrupted" / "raw-performance.mid").exists()
    assert not (tmp_path / "interrupted" / ".activation-work").exists()

    resumed = BasicPitchEngine(checkpoint_dir=checkpoint_root)
    resumed_model = ExplicitModelTensorFixture(2)
    monkeypatch.setattr(resumed, "_load_model", lambda: resumed_model)
    result = resumed.analyze(source, tmp_path / "resumed", "resumed-test-attempt")
    manifest = result["manifest"]["windowing"]
    assert resumed_model.calls == windows - 2
    assert manifest["checkpoint_windows_reused"] == 2
    assert manifest["covered_input_frames"] == frames
    assert manifest["activation_frames"] == count
    assert frames / rate - .02 < manifest["last_activation_seconds"] < frames / rate
    assert len([n for n in result["notes"] if n["pitch"] == 60]) == 1
    assert len([n for n in result["notes"] if n["pitch"] == 64]) == 2
    tail = [n for n in result["notes"] if n["pitch"] == 72]
    assert len(tail) == 1 and tail[0]["end"] > frames / rate - .15
    sustain = next(n for n in result["notes"] if n["pitch"] == 60)
    expected_start = 100 * FFT_HOP / rate
    expected_end = (hop + (250 - core) * FFT_HOP) / rate
    assert sustain["start"] == pytest.approx(expected_start, abs=.02)
    assert sustain["end"] == pytest.approx(expected_end, abs=.02)
    with np.load(tmp_path / "resumed" / "raw-model-output.npz") as raw:
        np.testing.assert_array_equal(raw["note"], timeline[:count])
    assert not (tmp_path / "resumed" / ".activation-work").exists()


def test_pyin_checkpoint_resume_reads_bounded_context_and_segments_once(tmp_path, monkeypatch):
    import librosa
    sr, duration, hop = 22050, 61, 256
    audio = tmp_path / "EXPLICIT-PYIN-UNIT-TEST.wav"
    sf.write(audio, np.zeros(sr * duration), sr, subtype="PCM_16")
    calls = []

    def explicit_frame_fixture(part, **kwargs):
        calls.append(len(part))
        count = 1 + len(part) // hop
        return np.full(count, 440.), np.ones(count, dtype=bool), np.full(count, .9)

    monkeypatch.setattr(librosa, "pyin", explicit_frame_fixture)
    monkeypatch.setattr(librosa, "load", lambda *args, **kwargs: pytest.fail("Full-file loading is not permitted"))
    monkeypatch.setattr(librosa.feature, "rms", lambda y, **kwargs: np.full((1, 1 + len(y) // hop), .5))
    checkpoints = tmp_path / "checkpoints"
    stages = []

    def cancel_second_window(label):
        stages.append(label)
        if len([s for s in stages if s.startswith("Tracking vocals pitch:")]) == 2:
            raise RuntimeError("Explicit test interruption")

    with pytest.raises(RuntimeError, match="test interruption"):
        PyinPartEngine("vocals", checkpoints).analyze(audio, tmp_path / "failed", "test", stage_callback=cancel_second_window)
    result = PyinPartEngine("vocals", checkpoints).analyze(audio, tmp_path / "resumed", "test")
    assert len(calls) == 3 and max(calls) < 32 * sr
    assert result["manifest"]["chunking"]["checkpoint_windows_reused"] == 1
    assert result["manifest"]["chunking"]["covered_input_frames"] == sr * duration
    assert len(result["notes"]) == 1 and result["notes"][0]["pitch"] == 69
    assert result["notes"][0]["end"] == pytest.approx(duration, abs=.02)


def test_separator_checkpoint_retry_keeps_full_sample_alignment(tmp_path, tensor_fixture):
    from backend.app.separation import SeparationEngine
    source = tmp_path / "EXPLICIT-SEPARATION-UNIT-TEST.wav"
    expected = audio_fixture(source)
    root = tmp_path / "checkpoints"

    def cancel(label):
        if label == "Separating audio segment 2 of 3":
            raise RuntimeError("Explicit test interruption")

    with pytest.raises(RuntimeError, match="test interruption"):
        SeparationEngine(settings(), checkpoint_dir=root).separate(source, tmp_path / "failed", cancel)
    assert not list((tmp_path / "failed").iterdir())
    result = SeparationEngine(settings(), checkpoint_dir=root).separate(source, tmp_path / "resumed")
    assert tensor_fixture == [88200, 66287, 4096]
    assert result["manifest"]["checkpoints"]["windows_reused"] == 1
    for stem, scale in zip(result["stems"], (.1, .2, .3, .4)):
        actual, rate = sf.read(tmp_path / "resumed" / stem["filename"], always_2d=True)
        assert rate == 44100 and actual.shape == expected.shape
        np.testing.assert_allclose(actual, expected * scale, rtol=2e-6, atol=1e-8)


def test_checkpoint_janitor_retains_active_and_retryable_work(tmp_path):
    records, roots, scratch = [], {}, {}
    for label, status in (("active", "processing"), ("queued", "queued"), ("done", "completed"),
                          ("cancel", "canceled"), ("retry", "failed"), ("expired", "failed")):
        identifier = str(uuid.uuid4())
        records.append({"id": identifier, "status": status})
        project = tmp_path / "projects" / identifier
        root = project / ".analysis-checkpoints"
        content = root / "content-addressed-unit-test"
        content.mkdir(parents=True)
        (content / "window-0000000.npz").write_bytes(b"explicit cache fixture")
        work = project / "explicit-attempt" / ".activation-work"
        work.mkdir(parents=True)
        (work / "frames.npy").write_bytes(b"explicit scratch fixture")
        if label == "expired":
            os.utime(content, (time.time() - 86401, time.time() - 86401))
        roots[label], scratch[label] = root, work
    cleanup_analysis_checkpoints(SimpleNamespace(data_dir=tmp_path), SimpleNamespace(list_projects=lambda: records))
    assert {label for label, path in roots.items() if path.exists()} == {"active", "queued", "retry"}
    assert {label for label, path in scratch.items() if path.exists()} == {"active", "queued"}


def test_hour_waveform_preserves_final_samples_with_bounded_reads(tmp_path, monkeypatch):
    import struct
    import wave
    from backend.app.review_api import waveform_peaks
    path = tmp_path / "EXPLICIT-ONE-HOUR-WAVEFORM-TEST.wav"
    sr, frames = 22050, 3600 * 22050
    # Sparse on disk: real WAV frame count and a final negative full-scale sample.
    with path.open("w+b") as stream:
        stream.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + frames * 2, b"WAVE",
                                 b"fmt ", 16, 1, 1, sr, sr * 2, 2, 16, b"data", frames * 2))
        stream.seek(44 + (frames - 1) * 2)
        stream.write(b"\0\x80")
    original = wave.Wave_read.readframes
    reads = []

    def bounded_read(self, count):
        reads.append(count)
        return original(self, count)

    monkeypatch.setattr(wave.Wave_read, "readframes", bounded_read)
    result = waveform_peaks(path)
    assert result["duration"] == 3600
    assert len(result["peaks"]) == 2000 and result["peaks"][-1] == 1
    assert max(reads) <= math.ceil(frames / 2000)

"""Separator safety/alignment checks; injected tensors are explicitly test fixtures.

RUN_SEPARATION_TESTS=1 enables a separate test using installed, verified weights.
No test fixture is a production inference fallback or a quality measurement.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from backend.app import separation
from backend.app.audio import AnalysisError


def settings(**values):
    return SimpleNamespace(separation_segment_seconds=2, separation_overlap=.25,
                           separation_threads=2, **values)


def audio_fixture(path, frames=3 * 44100 + 137, channels=2):
    """Generated, original unit-test audio with nonzero samples across seams."""
    time = np.arange(frames, dtype=np.float64) / 44100
    audio = np.stack([.2 * np.sin(2 * np.pi * 220 * time),
                      .1 * np.sin(2 * np.pi * 331 * time)], axis=1).astype(np.float32)
    sf.write(path, audio[:, :channels], 44100, subtype="FLOAT")
    return audio[:, :channels]


def test_capability_reports_missing_weights_without_loading_runtime(tmp_path, monkeypatch):
    inspected = []
    monkeypatch.setattr(separation.importlib.util, "find_spec", lambda name: inspected.append(name) or object())
    result = separation.separation_capability(settings(separation_model_dir=tmp_path))
    assert not result["available"]
    assert set(result["missing"]) == {entry["filename"] for entry in separation.WEIGHTS.values()}
    assert inspected == ["openunmix", "torch", "torchaudio", "soundfile"]
    assert result["sources"] == ["vocals", "drums", "bass", "other"]


def test_missing_and_corrupt_weights_fail_before_inference(tmp_path):
    config = settings(separation_model_dir=tmp_path)
    with pytest.raises(AnalysisError, match="weights are missing"):
        separation.verified_weights(config)
    (tmp_path / separation.WEIGHTS["vocals"]["filename"]).write_bytes(b"EXPLICIT CORRUPT TEST CHECKPOINT")
    with pytest.raises(AnalysisError, match="vocals.*checksum"):
        separation.verified_weights(config)


@pytest.fixture
def tensor_fixture(monkeypatch):
    """Known scaled tensors test overlap plumbing ONLY, never separation accuracy."""
    torch = pytest.importorskip("torch")
    openunmix = pytest.importorskip("openunmix")
    calls = []

    class ExplicitTensorFixture:
        target_models = {}

        def freeze(self):
            pass

        def __call__(self, audio):
            calls.append(audio.shape[-1])
            return torch.stack([audio * scale for scale in (.1, .2, .3, .4)], dim=1)

    def fixture_constructor(**kwargs):
        assert kwargs["pretrained"] is False
        assert kwargs["device"] == "cpu"
        return ExplicitTensorFixture()

    monkeypatch.setattr(separation, "separation_capability", lambda config: {"available": True})
    monkeypatch.setattr(separation, "verified_weights", lambda config: [])
    monkeypatch.setattr(openunmix, "umxhq", fixture_constructor)
    return calls


def test_overlap_add_preserves_samples_and_gain_at_every_boundary(tmp_path, tensor_fixture):
    path = tmp_path / "DEVELOPMENT-tensor-fixture.wav"
    expected = audio_fixture(path)
    stages = []
    result = separation.SeparationEngine(settings()).separate(path, tmp_path / "attempt", stages.append)
    assert tensor_fixture == [88200, 66287, 4096]
    assert [stage for stage in stages if stage.startswith("Separating audio")] == [
        "Separating audio segment 1 of 3", "Separating audio segment 2 of 3", "Separating audio segment 3 of 3"]
    for stem, scale in zip(result["stems"], (.1, .2, .3, .4)):
        samples, rate = sf.read(tmp_path / "attempt" / stem["filename"], dtype="float32", always_2d=True)
        assert rate == 44100 and samples.shape == expected.shape
        np.testing.assert_allclose(samples, expected * scale, rtol=2e-6, atol=1e-8)
        assert stem["sample_offset"] == 0 and stem["frames"] == len(expected)
    assert result["manifest"]["settings"]["overlap"] == .25
    assert not list((tmp_path / "attempt").glob(".*.partial.wav"))


def test_cancel_callback_cleans_already_written_partial_stems(tmp_path, tensor_fixture):
    path = tmp_path / "DEVELOPMENT-cancellation-fixture.wav"
    audio_fixture(path)
    output = tmp_path / "attempt"

    def cancel_after_first_segment(label):
        if label == "Separating audio segment 2 of 3":
            assert list(output.glob(".*.partial.wav"))
            raise RuntimeError("Explicit test cancellation")

    with pytest.raises(RuntimeError, match="test cancellation"):
        separation.SeparationEngine(settings()).separate(path, output, cancel_after_first_segment)
    assert tensor_fixture == [88200]
    assert list(output.iterdir()) == []


def test_resource_and_channel_guards_prevent_model_inference(tmp_path, tensor_fixture):
    path = tmp_path / "DEVELOPMENT-limit-fixture.wav"
    audio_fixture(path)
    with pytest.raises(AnalysisError, match="storage limit"):
        separation.SeparationEngine(settings(max_separation_storage_bytes=1)).separate(path, tmp_path / "attempt")
    mono = tmp_path / "DEVELOPMENT-invalid-mono.wav"
    audio_fixture(mono, channels=1)
    with pytest.raises(AnalysisError, match="stereo"):
        separation.SeparationEngine(settings()).separate(mono, tmp_path / "attempt")
    assert tensor_fixture == []


@pytest.mark.skipif(os.getenv("RUN_SEPARATION_TESTS") != "1", reason="Opt-in actual UMX-HQ inference")
def test_real_verified_model_exact_alignment_and_repeatability(tmp_path):
    from backend.app.config import Settings
    from dataclasses import replace

    config = replace(Settings.from_env(), separation_segment_seconds=2, separation_overlap=.25)
    capability = separation.separation_capability(config)
    assert capability["available"], capability
    source = tmp_path / "DEVELOPMENT-real-model-fixture.wav"
    audio_fixture(source, frames=2 * 44100 + 137)
    engine = separation.SeparationEngine(config)
    first = engine.separate(source, tmp_path / "first")
    second = engine.separate(source, tmp_path / "second")
    assert len(first["stems"]) == 4
    assert len({stem["sha256"] for stem in first["stems"]}) > 1
    for stem in first["stems"]:
        samples, rate = sf.read(tmp_path / "first" / stem["filename"], always_2d=True)
        repeated, _ = sf.read(tmp_path / "second" / stem["filename"], always_2d=True)
        assert samples.shape == (88337, 2) and rate == 44100
        assert np.isfinite(samples).all()
        # libsndfile's FLOAT WAV PEAK chunk contains a creation timestamp, so
        # decoded sample reproducibility must not compare container digests.
        np.testing.assert_array_equal(samples, repeated)
    manifest = first["manifest"]
    assert manifest["engine"] == "open-unmix" and manifest["model_license"] == "MIT"
    assert {entry["sha256"] for entry in manifest["weights"]} == {
        entry["sha256"] for entry in separation.WEIGHTS.values()}

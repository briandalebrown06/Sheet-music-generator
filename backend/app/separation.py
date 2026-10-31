"""Replaceable, local-only source separation with verified UMX-HQ weights.

The upstream spectrogram model and Wiener reconstruction perform all separation.
This adapter bounds inference context and overlap-adds real outputs at their
original sample offsets. It never substitutes reference sources or generated audio.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.metadata
import importlib.util
import math
from pathlib import Path
import shutil
import time
from typing import Callable, Protocol

from .audio import AnalysisError
from .analysis_checkpoints import CheckpointStore

MODEL_NAME = "umxhq"
MODEL_VERSION = "1.0.1"
MODEL_RECORD = "https://zenodo.org/records/3370489"
SOURCES = ("vocals", "drums", "bass", "other")
SAMPLE_RATE = 44100
# Author-deposited checksums, also cross-checked against complete local SHA-256.
WEIGHTS = {
    "vocals": {"filename": "vocals-b62c91ce.pth", "md5": "d918985fad0fedf6d9ce89e279aa7218",
               "sha256": "b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083"},
    "drums": {"filename": "drums-9619578f.pth", "md5": "cebf76e196e73e85d247f462c31e36fc",
              "sha256": "9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a"},
    "bass": {"filename": "bass-8d85a5bd.pth", "md5": "8cc37d31903fe48306468ee968f4b1b6",
             "sha256": "8d85a5bd3f996a8867fca0e8442e077e5a3f5ec747a6112742452a8f347b39c8"},
    "other": {"filename": "other-b52fbbf7.pth", "md5": "8637606623ed7c74986789c3b1f94bc6",
              "sha256": "b52fbbf76479e752bd72e02304c602ac7802aa5bfbfb9cd12054b2695d5093ab"},
}


class SeparatorAdapter(Protocol):
    def separate(self, input_path: Path, output_dir: Path,
                 stage_callback: Callable[[str], None] | None = None) -> dict: ...


def model_directory(settings) -> Path:
    return Path(getattr(settings, "separation_model_dir", Path(__file__).resolve().parents[2] / ".data/models/umxhq"))


def separation_capability(settings) -> dict:
    """Cheap installation check; loading and full checksum validation happen in the worker."""
    missing = [name for name in ("openunmix", "torch", "torchaudio", "soundfile")
               if importlib.util.find_spec(name) is None]
    directory = model_directory(settings)
    missing += [value["filename"] for value in WEIGHTS.values() if not (directory / value["filename"]).is_file()]
    return {"available": not missing, "engine": "open-unmix", "model": MODEL_NAME,
            "model_version": MODEL_VERSION, "sources": list(SOURCES), "missing": missing,
            "detail": "Install backend/requirements-separation.txt and run backend/install_separation.py in the worker environment."
            if missing else "Local CPU separation; installed weights are checksum-verified before inference."}


def file_digests(path: Path) -> dict:
    sha256, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as source:
        while data := source.read(1024 * 1024):
            sha256.update(data)
            md5.update(data)
    return {"sha256": sha256.hexdigest(), "md5": md5.hexdigest(), "bytes": path.stat().st_size}


def verified_weights(settings) -> list[dict]:
    directory = model_directory(settings)
    records = []
    for category, expected in WEIGHTS.items():
        path = directory / expected["filename"]
        if not path.is_file():
            raise AnalysisError("Separation model weights are missing. Run backend/install_separation.py, then retry.")
        digests = file_digests(path)
        if digests["md5"] != expected["md5"] or (expected.get("sha256") and digests["sha256"] != expected["sha256"]):
            raise AnalysisError(f"The {category} separation weights failed checksum verification. Reinstall the official model files.")
        records.append({"category": category, "filename": path.name, **digests,
                        "url": f"{MODEL_RECORD}/files/{path.name}", "license": "MIT",
                        "license_source": "https://zenodo.org/api/records/3370489"})
    return records


class SeparationEngine:
    """Default adapter; only tensor inference runs here, never upstream audio downloads/I/O."""
    def __init__(self, settings, checkpoint_dir: Path | None = None):
        self.settings = settings
        self.checkpoint_dir = checkpoint_dir

    def separate(self, input_path: Path, output_dir: Path,
                 stage_callback: Callable[[str], None] | None = None) -> dict:
        stage = stage_callback or (lambda label: None)
        capability = separation_capability(self.settings)
        if not capability["available"]:
            raise AnalysisError(capability["detail"])
        stage("Verifying source-separation model weights")
        weights = verified_weights(self.settings)
        try:
            import numpy as np
            import soundfile as sf
            import torch
            import openunmix
        except Exception as exc:
            raise AnalysisError("The source-separation runtime could not load. Check the installed PyTorch/Open-Unmix versions.") from exc

        segment_seconds = float(getattr(self.settings, "separation_segment_seconds", 10.0))
        overlap = float(getattr(self.settings, "separation_overlap", .25))
        threads = int(getattr(self.settings, "separation_threads", 2))
        if not math.isfinite(segment_seconds) or not 2 <= segment_seconds <= 30 or not 0 <= overlap <= .5 or not 1 <= threads <= 8:
            raise AnalysisError("Separation settings require 2–30 second segments, 0–0.5 overlap, and 1–8 CPU threads.")
        info = sf.info(str(input_path))
        if info.samplerate != SAMPLE_RATE or info.channels != 2 or info.frames <= 0:
            raise AnalysisError("Source separation requires nonempty, decoded stereo audio at 44,100 Hz.")
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        planned_bytes = info.frames * 2 * 4 * len(SOURCES) + 4096
        existing_bytes = sum(path.stat().st_size for path in output_dir.iterdir() if path.is_file())
        storage_limit = int(getattr(self.settings, "max_separation_storage_bytes", 512 * 1024 ** 2))
        if planned_bytes + existing_bytes > storage_limit:
            raise AnalysisError("Separated stems exceed the configured storage limit. Select a shorter clip or increase MAX_SEPARATION_STORAGE_MB.")
        if shutil.disk_usage(output_dir).free < planned_bytes + 64 * 1024 ** 2:
            raise AnalysisError("There is not enough free disk space for the separated stems. Free space or select a shorter clip.")

        stage("Loading UMX-HQ source-separation model")
        torch.set_num_threads(threads)
        torch.manual_seed(0)
        # pretrained=False is essential: the application installer owns downloads.
        separator = openunmix.umxhq(targets=list(SOURCES), pretrained=False, niter=1,
                                   device="cpu", wiener_win_len=300, filterbank="torch")
        for record in weights:
            state = torch.load(model_directory(self.settings) / record["filename"], map_location="cpu", weights_only=True)
            # The 2019 deposits also contain obsolete, nonlearned audio-transform
            # buffers. Validate those explicitly; every learned key must match.
            rate = state.pop("sample_rate", None)
            if rate is not None and float(rate) != SAMPLE_RATE:
                raise AnalysisError("The separation checkpoint has an unexpected sample rate.")
            for name in ("stft.window", "transform.0.window"):
                window = state.pop(name, None)
                if window is not None and (window.shape != (4096,) or not torch.allclose(window, torch.hann_window(4096), atol=2e-7, rtol=1e-5)):
                    raise AnalysisError("The separation checkpoint has an unexpected transform window.")
            separator.target_models[record["category"]].load_state_dict(state, strict=True)
        separator.freeze()

        segment = int(round(segment_seconds * SAMPLE_RATE))
        step = max(1, int(round(segment * (1 - overlap))))
        crossfade = segment - step
        pending = np.zeros((len(SOURCES), segment, 2), dtype=np.float32)
        divisor = np.zeros(segment, dtype=np.float32)
        pending_files = [output_dir / f".{category}.partial.wav" for category in SOURCES]
        final_files = [output_dir / f"{category}.wav" for category in SOURCES]
        energy = np.zeros(len(SOURCES), dtype=np.float64)
        peaks = np.zeros(len(SOURCES), dtype=np.float64)
        total_segments = math.ceil(info.frames / step)
        store = CheckpointStore(self.checkpoint_dir or output_dir / ".analysis-checkpoints", input_path,
            {"engine": "open-unmix", "model": MODEL_NAME, "model_version": MODEL_VERSION,
             "torch_version": importlib.metadata.version("torch"), "openunmix_version": importlib.metadata.version("openunmix"),
             "weights": [record["sha256"] for record in weights], "segment": segment, "step": step,
             "threads": threads, "niter": 1, "seed": 0, "sample_rate": SAMPLE_RATE})
        started = time.monotonic()
        try:
            with ExitStack() as stack, torch.inference_mode():
                source = stack.enter_context(sf.SoundFile(str(input_path), "r"))
                targets = [stack.enter_context(sf.SoundFile(str(path), "w", samplerate=SAMPLE_RATE,
                           channels=2, subtype="FLOAT", format="WAV")) for path in pending_files]
                for index, start in enumerate(range(0, info.frames, step)):
                    stage(f"Separating audio segment {index + 1} of {total_segments}")
                    source.seek(start)
                    chunk = source.read(min(segment, info.frames - start), dtype="float32", always_2d=True)
                    length = len(chunk)
                    if not np.isfinite(chunk).all():
                        raise AnalysisError("The decoded mixture contains invalid audio samples.")
                    cached = store.load(index, {"stems": (len(SOURCES), length, 2)})
                    prediction = cached["stems"] if cached is not None else None
                    if prediction is None or not np.isfinite(prediction).all():
                        # Torch's centered STFT needs more than half its FFT length.
                        padded = np.pad(chunk, ((0, max(0, 4096 - length)), (0, 0)))
                        audio = torch.from_numpy(padded.T.copy()).unsqueeze(0)
                        prediction = separator(audio)[0, :, :, :length].permute(0, 2, 1).cpu().numpy()
                        if prediction.shape != (len(SOURCES), length, 2) or not np.isfinite(prediction).all():
                            raise AnalysisError("The separation engine produced invalid or misaligned stem samples.")
                        store.save(index, {"stems": prediction})
                    window = np.ones(length, dtype=np.float32)
                    ramp_length = min(crossfade, length)
                    if ramp_length:
                        ramp = np.linspace(0, 1, ramp_length + 2, dtype=np.float32)[1:-1]
                        if start:
                            window[:ramp_length] *= ramp
                        if start + length < info.frames:
                            window[-ramp_length:] *= ramp[::-1]
                    pending[:, :length] += prediction * window[None, :, None]
                    divisor[:length] += window
                    flush = min(step, info.frames - start)
                    samples = pending[:, :flush] / divisor[None, :flush, None]
                    for target_index, target in enumerate(targets):
                        target.write(samples[target_index])
                        energy[target_index] += np.square(samples[target_index], dtype=np.float64).sum()
                        peaks[target_index] = max(peaks[target_index], float(np.abs(samples[target_index]).max()))
                    remaining = segment - flush
                    if remaining:
                        pending[:, :remaining] = pending[:, flush:]
                        divisor[:remaining] = divisor[flush:]
                    pending[:, remaining:] = 0
                    divisor[remaining:] = 0
            stage("Validating aligned separated stems")
            stems = []
            for index, (category, temporary, final) in enumerate(zip(SOURCES, pending_files, final_files)):
                actual = sf.info(str(temporary))
                if actual.frames != info.frames or actual.samplerate != SAMPLE_RATE or actual.channels != 2:
                    raise AnalysisError("A separated stem has an unexpected duration or format.")
                temporary.replace(final)
                rms = math.sqrt(float(energy[index]) / (info.frames * 2))
                stems.append({"category": category, "filename": final.name,
                              "duration_seconds": info.frames / SAMPLE_RATE, "frames": info.frames,
                              "sample_rate": SAMPLE_RATE, "channels": 2, "sample_offset": 0,
                              "rms_dbfs": 20 * math.log10(max(rms, 1e-12)), "peak": float(peaks[index]),
                              "sha256": file_digests(final)["sha256"], "bytes": final.stat().st_size})
        except BaseException:
            for path in pending_files + final_files:
                path.unlink(missing_ok=True)
            if self.checkpoint_dir is None:
                shutil.rmtree(output_dir / ".analysis-checkpoints", ignore_errors=True)
            raise
        if self.checkpoint_dir is None:
            shutil.rmtree(output_dir / ".analysis-checkpoints", ignore_errors=True)
        return {"stems": stems, "manifest": {
            "schema_version": 1, "engine": "open-unmix", "model": MODEL_NAME, "model_version": MODEL_VERSION,
            "versions": {name: importlib.metadata.version(name) for name in ("openunmix", "torch", "torchaudio", "numpy", "soundfile")},
            "weights": weights, "code_license": "MIT", "model_license": "MIT",
            "model_license_source": "https://zenodo.org/api/records/3370489",
            "settings": {"device": "cpu", "threads": threads, "segment_seconds": segment_seconds,
                         "segment_frames": segment, "step_frames": step, "overlap": overlap,
                         "overlap_add": "linear crossfade normalized by accumulated weights",
                         "niter": 1, "wiener_win_len": 300, "n_fft": 4096, "n_hop": 1024,
                         "sample_rate": SAMPLE_RATE, "channels": 2, "output_subtype": "FLOAT",
                         "seed": 0},
            "time_basis": "Each stem frame matches the same frame in the decoded selected mixture; no trimming or time stretching.",
            "input_frames": info.frames, "input_sha256": store.audio_sha256,
            "checkpoints": {"windows": total_segments, "windows_reused": store.hits,
                            "coverage": "All input frames; normalized overlap-add reassembled from complete windows"},
            "duration_seconds": info.frames / SAMPLE_RATE, "elapsed_seconds": time.monotonic() - started,
            "limitations": ["Stem categories are estimates and may contain leakage.",
                            "Other is a residual instrument category, not isolated guitar or piano.",
                            "Chunked bidirectional inference can differ near boundaries from whole-track inference."]}}

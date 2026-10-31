# Source-separation engine

Verified on 2026-10-27. The application uses the real **Open-Unmix UMX-HQ 1.0.1** four-source model on CPU. The replaceable `SeparatorAdapter` interface returns the model's actual categories: **vocals, drums, bass, other**. “Other” is a residual instrument mixture, not an identified guitar or piano. Stems can leak into one another. No accompaniment is generated.

## Selection and independent license review

| Candidate | Current official evidence | Decision |
| --- | --- | --- |
| Meta Demucs | The [original repository](https://github.com/facebookresearch/demucs) was archived on January 1, 2025. The [author's fork](https://github.com/adefossez/demucs) is unarchived; its last push was October 26, 2026. Its current [README license section](https://github.com/adefossez/demucs#license) explicitly says the weight license is unclear, separately from MIT code. | Do not interpret the code license as permission for the official weights. Not selected. |
| Demucs-compatible `demucs-infer` | [Version 4.4.0](https://pypi.org/project/demucs-infer/4.4.0/) was released October 5, 2026; [upstream](https://github.com/openmirlab/demucs-infer) is unarchived and currently directs future releases to Git installation. This fork removes the TorchAudio dependency and retains CPU and GPU inference, segmentation and overlap. Its code is MIT, but using the same official Demucs weights does not resolve their separate license ambiguity. | Dependency resolution was checked in the actual environment; no model was installed or benchmarked. Keep this adapter option open if acceptable weights become available. |
| Open-Unmix UMX-HQ | [Open-Unmix 1.3.0](https://pypi.org/project/openunmix/1.3.0/) was released April 16, 2024. The [repository](https://github.com/sigsep/open-unmix-pytorch) is unarchived, with its last push June 17, 2024: this is an older reference implementation, not evidence of active maintenance. Its [code license](https://github.com/sigsep/open-unmix-pytorch/blob/master/LICENSE) is MIT. Separately, the authors' [UMX-HQ weight deposit](https://zenodo.org/records/3370489), version 1.0.1, has explicit `mit-license` metadata in the [official record API](https://zenodo.org/api/records/3370489). | Selected after installation and actual inference passed. Known dependency pins and a replaceable adapter limit reliance on the older implementation. |
| Open-Unmix UMX-L | The [separate weight record](https://zenodo.org/records/5069601) declares CC-BY-NC-SA 4.0 in its [record metadata](https://zenodo.org/api/records/5069601). | Not selected; UMX-HQ's license does not apply to UMX-L. |
| Spleeter | [Version 2.4.2 metadata](https://pypi.org/pypi/spleeter/json) requires Python below 3.12, TensorFlow 2.12.1, and older HTTPX/Typer dependencies. The [repository](https://github.com/deezer/spleeter) is unarchived and was last pushed June 18, 2026; code is MIT. | The older TensorFlow/API dependency stack is a poor fit for this existing ONNX environment. Not installed or benchmarked; its separate model-weight license review was not completed. |

Repository push dates describe observed activity, not a maintenance guarantee. No comparative audio-quality ranking was measured across these engines.

## Install in the existing backend environment

```sh
.venv/bin/python -m pip install -r backend/requirements-separation.txt
.venv/bin/python backend/install_separation.py
.venv/bin/python -m pip check
```

Current tested combination on macOS 27.0.1 arm64 / Python 3.11.18: Open-Unmix 1.3.0, Torch 2.14.1, TorchAudio 2.11.0, NumPy 1.26.4, SoundFile 0.14.0 and librosa 0.11.0. The deployment audit upgraded Torch from 2.11.0 to address GHSA-rrmf-rvhw-rf47; actual UMX/Basic Pitch regressions passed and every measured multitrack quality metric was unchanged. TorchAudio 2.11 uses its documented stable ABI with newer Torch. See [DEPENDENCY_ADVISORIES.md](DEPENDENCY_ADVISORIES.md) for remaining upstream constraints. Existing Basic Pitch ONNX dependencies remain installed. `pip check` passed. Other operating systems and GPUs were not tested. TorchAudio is an upstream dependency; the adapter uses SoundFile for audio I/O and Torch STFT/ISTFT, avoiding TorchAudio's audio-decoding APIs.

The installer downloads four author-deposited files totaling 142,551,184 bytes to `SEPARATION_MODEL_DIR` (default `.data/models/umxhq`). Both the published MD5 and pinned complete SHA-256 must match before installation. The runtime verifies every weight checksum again before loading with `torch.load(weights_only=True)`. Downloads occur only during explicit installation; a job cannot silently fetch a model. The installer retains model provenance and a separate weight-license notice.

The 2019 checkpoints include obsolete, nonlearned sample-rate and transform-window buffers. The adapter validates these against 44,100 Hz and the expected 4,096-sample Hann window, then removes only those buffers. Every learned model parameter is loaded with strict key and shape checking. A failed installation or incompatible checkpoint produces an error, never substituted separation.

## Timing, memory and reproducibility

The backend first decodes the selected clip to stereo 44,100 Hz. Inference uses ten-second segments, 25% overlap, one Wiener iteration, 300-frame Wiener windows and two CPU threads by default. Each real model output is placed at its original sample position and combined using a normalized linear crossfade. Fixed-size audio and overlap buffers avoid allocating a whole-song output tensor. Bidirectional context resets at each segment, so boundaries can differ from upstream whole-track inference; overlap mitigates but cannot eliminate this difference.

All four stems have exactly the selected mixture's frame count. No trimming, time stretching, independent gain normalization or per-stem peak scaling is applied. FLOAT32 WAV preserves the output's gain and avoids integer clipping. The parent project's selected-clip offset preserves the position in the original recording. The manifest records input/output digests, model digests, runtime versions, settings, frame counts and the timing convention.

`SEPARATION_SEGMENT_SECONDS` (2–30), `SEPARATION_OVERLAP` (0–0.5), and `SEPARATION_THREADS` (1–8) are bounded. The worker applies configured process time, memory, retry and project-family storage limits; the adapter checks planned output bytes and free disk space before inference. A 300-second stereo FLOAT32 stem requires approximately 106 MB, so four stems require approximately 423 MB before the original, decoded mixture or selected-part copies. CPU-only operation requires sufficient RAM and disk; no GPU is required by this implementation. A killed or canceled attempt is unpublished and cleaned by the job supervisor.

The first measured run of the repository's 12-second synthetic multitrack fixture used two inference windows, took **1.452 seconds** including checkpoint verification/loading, and reached **972,570,624 bytes** process peak RSS on this machine. Each of its four outputs contained exactly 529,200 stereo frames. These measurements are not a completion estimate or a guarantee for other hardware, recordings or segment lengths. The reproducible quality evaluation and its errors are in [MIXED_EVALUATION.md](MIXED_EVALUATION.md); synthetic-source results do not establish accuracy on commercial songs.

## Verification

```sh
.venv/bin/python -m pytest backend/tests/test_separation_adapter.py -q
RUN_SEPARATION_TESTS=1 .venv/bin/python -m pytest backend/tests/test_separation_adapter.py -q
.venv/bin/python scripts/evaluate_mixed.py
```

The default adapter checks include explicitly injected unit-test tensors for overlap reconstruction and cancellation cleanup; these fixtures never enter production inference. The opt-in test loads the actual verified weights, checks deterministic output and exact sample alignment, and requires the installed separation runtime. The evaluation uses independently generated source audio only as measurement references; production inference receives the mixture alone.

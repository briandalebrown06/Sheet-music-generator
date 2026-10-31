# Linux dependency resolution

Resolved on 2026-10-27 with uv **0.13.6**, CPython **3.11**, and Debian Bookworm's **glibc 2.36** target. This is cross-platform dependency and wheel-availability verification, not a Linux installation, container-build or inference result.

- `requirements-linux-amd64.lock.txt`: 112 resolved packages for Linux x86_64.
- `requirements-linux-arm64.lock.txt`: 113 resolved packages for Linux aarch64.
- Every package is pinned and hashed. Torch 2.14.1 and TorchAudio 2.11.0 use explicit, hash-pinned CPU wheel URLs returned by the [official CPU index](https://download.pytorch.org/whl/cpu/). Other packages resolve from PyPI. [TorchAudio 2.11's stable ABI](https://docs.pytorch.org/audio/stable/installation.html) supports newer Torch releases.
- Both completed resolution with `--only-binary :all:`. Compatible wheels exist for every selected package; the configured target does not need a source-package compiler toolchain.

Basic Pitch 0.4.0's Linux metadata requires TensorFlow even when its ONNX extra is selected and the app runs ONNX inference. The lock honors that metadata instead of hiding it with `--no-deps`. The amd64 result selects TensorFlow 2.15.0.post1; arm64 selects TensorFlow 2.15.0 and its `tensorflow-cpu-aws` 2.15.0 implementation. Both select protobuf 4.25.9 and ml-dtypes 0.2.0. The legacy TensorFlow pin increases image size and is a dependency-maintenance constraint. A clean resolution is not a vulnerability audit or a claim of current upstream support for every transitive dependency. The [dependency advisory review](../docs/DEPENDENCY_ADVISORIES.md) records unresolved Keras/protobuf/setuptools findings and the Torch fix; these remain a public-release gate.

The common input references the application's three maintained requirements files. Architecture inputs add exact CPU-wheel links. Reproduce from the repository root with an isolated resolver environment:

```sh
python3.11 -m venv .venv-lock
.venv-lock/bin/python -m pip install uv==0.13.6
.venv-lock/bin/uv pip compile deploy/requirements-linux-amd64.in \
  --python .venv-lock/bin/python --python-version 3.11 \
  --python-platform x86_64-manylinux_2_36 --generate-hashes --only-binary :all: \
  --emit-index-url --emit-index-annotation \
  -o deploy/requirements-linux-amd64.lock.txt
.venv-lock/bin/uv pip compile deploy/requirements-linux-arm64.in \
  --python .venv-lock/bin/python --python-version 3.11 \
  --python-platform aarch64-manylinux_2_36 --generate-hashes --only-binary :all: \
  --emit-index-url --emit-index-annotation \
  -o deploy/requirements-linux-arm64.lock.txt

# Confirm all locked distributions have wheels for the target.
.venv-lock/bin/uv pip compile deploy/requirements-linux-amd64.lock.txt \
  --python .venv-lock/bin/python --python-version 3.11 \
  --python-platform x86_64-manylinux_2_36 --only-binary :all: \
  -o /tmp/scorecraft-amd64-wheel-check.txt
.venv-lock/bin/uv pip compile deploy/requirements-linux-arm64.lock.txt \
  --python .venv-lock/bin/python --python-version 3.11 \
  --python-platform aarch64-manylinux_2_36 --only-binary :all: \
  -o /tmp/scorecraft-arm64-wheel-check.txt
```

uv explicitly evaluates the [target Python/platform markers](https://docs.astral.sh/uv/concepts/resolution/) and supports [dedicated PyTorch CPU indexes](https://docs.astral.sh/uv/guides/integration/pytorch/). Ordinary host-platform `pip --platform` inspection alone was not treated as proof of Linux marker resolution. The final lock uses direct CPU-wheel URLs so ordinary pip installation does not depend on uv-specific index routing.

On the matching Linux image, install with `python -m pip install --require-hashes --only-binary=:all: -r <matching-lockfile>` and run `python -m pip check`. These install commands and actual ONNX/pYIN/UMX-HQ/PDF execution still require the Docker qualification described in the deployment guide. Rebuild, scan dependencies and rerun inference/export tests when updating pins; do not assume a newly resolvable version preserves model behavior.

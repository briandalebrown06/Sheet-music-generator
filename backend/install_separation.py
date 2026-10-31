"""Download verified official UMX-HQ weights once; inference never downloads them.

First install backend/requirements-separation.txt in the Python 3.11 environment.
Then run: .venv/bin/python backend/install_separation.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.separation import MODEL_NAME, MODEL_RECORD, MODEL_VERSION, WEIGHTS, file_digests, model_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, help="Override SEPARATION_MODEL_DIR for this download")
    args = parser.parse_args()
    directory = (args.model_dir or model_directory(Settings.from_env())).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    for category, expected in WEIGHTS.items():
        target = directory / expected["filename"]
        valid = target.is_file() and file_digests(target)["sha256"] == expected["sha256"]
        if not valid:
            temporary = directory / f".{uuid.uuid4()}.partial"
            started = time.monotonic()
            count = 0
            try:
                print(f"Downloading official UMX-HQ {category} weights…", flush=True)
                request = urllib.request.Request(f"{MODEL_RECORD}/files/{target.name}?download=1",
                                                 headers={"User-Agent": "Scorecraft-local-model-installer/1"})
                with urllib.request.urlopen(request, timeout=45) as response, temporary.open("xb") as output:
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > 40 * 1024 ** 2 or time.monotonic() - started > 600:
                            raise RuntimeError("The model download exceeded its size/time bound.")
                        output.write(chunk)
                digests = file_digests(temporary)
                if any(digests[key] != expected[key] for key in ("sha256", "md5")):
                    raise RuntimeError(f"Official {category} weight checksum did not match. No model was installed.")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        records.append({"category": category, "filename": target.name, **file_digests(target),
                        "url": f"{MODEL_RECORD}/files/{target.name}"})
        print(f"Verified {category}: {expected['sha256']}", flush=True)
    manifest = {"engine": "open-unmix", "model": MODEL_NAME, "model_version": MODEL_VERSION,
                "model_record": MODEL_RECORD, "model_license": "MIT",
                "license_source": "https://zenodo.org/api/records/3370489",
                "authors": ["Fabian-Robert Stöter", "Antoine Liutkus"], "weights": records}
    (directory / "model-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (directory / "MODEL_LICENSE_NOTICE.txt").write_text(
        "UMX-HQ model version 1.0.1 by Fabian-Robert Stöter and Antoine Liutkus.\n"
        "The authors' Zenodo record 3370489 declares these model weights MIT-licensed.\n"
        "Model: https://zenodo.org/records/3370489\n"
        "License metadata: https://zenodo.org/api/records/3370489\n"
        "MIT terms: https://opensource.org/license/mit\n"
        "\nPermission is hereby granted, free of charge, to any person obtaining a copy\n"
        "of this software and associated documentation files (the Software), to deal\n"
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in\n"
        "all copies or substantial portions of the Software.\n\n"
        "THE SOFTWARE IS PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n"
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN\n"
        "THE SOFTWARE.\n\n"
        "Open-Unmix implementation copyright 2019 Inria (Fabian-Robert Stöter, Antoine Liutkus).\n"
        "Preserve the separately installed package's MIT license notice when distributing its code.\n")
    print(f"UMX-HQ installed at {directory}. No model download occurs during analysis.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"Separation model installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

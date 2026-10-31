"""Fetch or verify the explicitly licensed, pinned public piano playback assets.

No project audio or user data is read. Only the 30 allowlisted sample filenames
from the checked manifest can be written to frontend/public/samples/salamander.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "efd8296360f9526e379bfbe5c1698ff54d6a1d34"
BASE = f"https://raw.githubusercontent.com/Tonejs/audio/{COMMIT}/salamander/"
DESTINATION = ROOT / "frontend/public/samples/salamander"
ALLOWED = {"A0.mp3", "C8.mp3"} | {f"{note}{octave}.mp3" for octave in range(1, 8) for note in ("C", "Ds", "Fs", "A")}


def verified(path: Path, item: dict) -> bool:
    return (path.is_file() and path.stat().st_size == item["bytes"]
            and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Check the committed bytes without network access.")
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "scripts/piano_samples.manifest.json").read_text())
    samples = manifest["samples"]
    if manifest["commit"] != COMMIT or len(samples) != 30 or {row["file"] for row in samples} != ALLOWED:
        raise ValueError("The piano asset manifest does not match the pinned allowlist.")
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for item in samples:
        path = DESTINATION / item["file"]
        if verified(path, item):
            continue
        if args.verify_only:
            raise ValueError(f"Missing or changed public piano sample: {item['file']}")
        if not 0 < item["bytes"] <= 512 * 1024:
            raise ValueError("Unexpected piano sample size.")
        with urllib.request.urlopen(BASE + item["file"], timeout=30) as response:
            content = response.read(item["bytes"] + 1)
        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError(f"Public sample checksum failed: {item['file']}")
        temporary = path.with_suffix(".mp3.part")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Verified {len(samples)} public piano samples ({sum(row['bytes'] for row in samples):,} bytes), commit {COMMIT}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

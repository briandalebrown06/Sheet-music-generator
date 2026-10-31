"""Create and verify requested local piano refinements, retaining their originals.

Usage: .venv/bin/python scripts/verify_piano_refinement.py \
  --source accompaniment=PROJECT_ID --source solo_piano=PROJECT_ID \
  --output output/pdf/piano-refinement --target 3

This calls the real local API and writes private comparison evidence. It measures
saved-note/export consistency; it is not a listening or transcription benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.piano import _locked
from backend.app.score import fraction
from scripts.evaluate_piano import inspect_exports, score_stats
from scripts.audit_piano_density import event_timeline, hand_density


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, help="label=project-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--base", default="http://127.0.0.1:8000/api")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(args.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=240) as response:
            return response.read(), {key.lower(): value for key, value in response.headers.items()}

    def get(path):
        return json.loads(request(path)[0])

    result = {"listening_review": False, "pianist_review": False, "editions": {}}
    for item in args.source:
        label, identifier = item.split("=", 1)
        if label not in {"accompaniment", "solo_piano"}:
            raise ValueError("Label must be accompaniment or solo_piano")
        base = f"/projects/{identifier}"
        before, before_score = get(base), get(base + "/score")
        settings = {**before["arrangement"]["settings"], "target_keys_per_hand": args.target}
        payload = {"notes_revision": before["notes_revision"], "score_revision": before_score["revision"],
                   "piano_settings": settings}
        started = time.monotonic()
        after = json.loads(request(base + "/accompaniment", payload)[0])
        elapsed = time.monotonic() - started
        new_base = f'/projects/{after["id"]}'
        state = get(new_base + "/score")
        score = state["score"]
        independent = hand_density(event_timeline(after["notes"], score), after.get("clip_duration_seconds"))
        for hand in ("left", "right"):
            measured = independent["hands"][hand]
            assert measured["maximum_keys"] == score["playability"]["summary"][f"max_{hand}_keys"], "Canonical key count and physical checker disagree"
            assert measured["maximum_span_semitones"] == score["playability"]["summary"][f"max_{hand}_span"], "Canonical hand span and physical checker disagree"
        assert after["id"] != identifier
        assert after["arrangement"]["edition_mode"] == label
        old_by_id = {note["id"]: note for note in before["notes"]}
        new_by_id = {note["id"]: note for note in after["notes"]}
        assert new_by_id.keys() <= old_by_id.keys(), "Refinement invented note IDs"
        for note in after["notes"]:
            assert note["original_timing"] == old_by_id[note["id"]]["original_timing"]
        protected = [note for note in before["notes"] if note.get("role") in
                     {"melody", "vocal_cue", "bass", "riff", "countermelody"} or _locked(note)]
        for note in protected:
            assert note["id"] in new_by_id, "A protected source event was removed"
            if note.get("role") in {"melody", "vocal_cue"} or _locked(note):
                for key in ("pitch", "start", "end", "original_timing"):
                    assert new_by_id[note["id"]].get(key) == note.get(key), f"Protected {key} changed"
        for part in score["parts"]:
            for staff in part["staves"]:
                for measure in staff["measures"]:
                    for voice in measure["voices"]:
                        assert sum(fraction(event["duration"]) for event in voice["events"]) == fraction(measure["duration"])
        exports = {}
        for format in ("musicxml", "midi", "pdf"):
            content, headers = request(new_base + f'/score/exports/{format}?revision={state["revision"]}')
            header = Message()
            header["Content-Disposition"] = headers["content-disposition"]
            name = header.get_filename()
            assert name and Path(name).name == name
            assert int(headers["x-score-revision"]) == state["revision"]
            target = args.output / name
            target.write_bytes(content)
            exports[format] = {"path": str(target.resolve()), "bytes": len(content),
                               "sha256": hashlib.sha256(content).hexdigest()}
        agreement = inspect_exports(score, Path(exports["musicxml"]["path"]).read_text(), Path(exports["midi"]["path"]))
        assert get(new_base)["notes"] == after["notes"]
        assert get(base)["notes"] == before["notes"]
        assert get(base + "/score") == before_score
        duplicate = json.loads(request(base + "/accompaniment", payload)[0])
        assert duplicate["id"] == after["id"], "Duplicate refinement created a second project"
        old_raw = request(base + "/exports/raw-midi")[0]
        new_raw = request(new_base + "/exports/raw-midi")[0]
        assert old_raw == new_raw
        entry = {"source_id": identifier, "id": after["id"], "title": after["title"],
                 "revision": state["revision"], "elapsed_api_seconds": round(elapsed, 3),
                 "notes_before": len(before["notes"]), "notes_after": len(after["notes"]),
                 "protected_notes_retained": len(protected), "before": before["arrangement"]["report"],
                 "after": score["playability"], "score_statistics": score_stats(score),
                 "independent_canonical_density": independent,
                 "exports": exports, "verification": agreement, "reopened": True,
                 "source_unchanged": True, "idempotent": True,
                 "raw_midi_sha256": hashlib.sha256(new_raw).hexdigest()}
        result["editions"][label] = entry
        for suffix, data in (("before-project", before), ("before-score", before_score),
                             ("project", after), ("score", state)):
            (args.output / f"{label}-{suffix}.private.json").write_text(json.dumps(data, indent=2))
        (args.output / "verification.private.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({"edition": label, "id": after["id"], "before": len(before["notes"]),
                          "after": len(after["notes"]), "hand_checks": score["playability"]["summary"],
                          "exports_verified": True}), flush=True)


if __name__ == "__main__":
    main()

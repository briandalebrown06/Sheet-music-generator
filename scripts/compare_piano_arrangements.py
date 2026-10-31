"""Audit arrangement changes without claiming human musical recognizability.

Inputs may be project API responses, project snapshots, arrangement results,
note lists, or canonical scores. This read-only tool never reads the live DB.
Note comparisons use exact stored values; canonical comparisons use rational
beat positions and count attacks once, excluding continuation tie fragments.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


PROTECTED_ROLES = ("bass", "riff", "countermelody")
MUSICAL_FIELDS = ("pitch", "start", "end", "key_release_seconds", "pedal_sustained")


def _score(document):
    if not isinstance(document, dict):
        return None
    if "parts" in document and "settings" in document:
        return document
    nested = document.get("score")
    return _score(nested) if isinstance(nested, dict) else None


def extract_notes(document):
    """Use stable source notes, never reconstruct identities from score glyphs."""
    if isinstance(document, list):
        notes = document
    elif isinstance(document, dict):
        notes = document.get("notes")
        if notes is None:
            notes = document.get("source_notes")
        if notes is None and document.get("notes_json") is not None:
            notes = json.loads(document["notes_json"])
        if notes is None and isinstance(document.get("project"), dict):
            return extract_notes(document["project"])
        nested_score = _score(document)
        if notes is None and nested_score is not None and nested_score is not document:
            return extract_notes(nested_score)
    else:
        notes = None
    if not isinstance(notes, list):
        raise ValueError("Input needs a notes/source_notes list or project.notes_json.")
    identifiers = set()
    for note in notes:
        if not isinstance(note, dict) or not isinstance(note.get("id"), str) or not note["id"]:
            raise ValueError("Every compared note must have a nonempty stable string ID.")
        if note["id"] in identifiers:
            raise ValueError(f"Duplicate stable note ID: {note['id']}")
        identifiers.add(note["id"])
        for field in ("pitch", "start", "end"):
            value = note.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"Note {note['id']} needs a finite numeric {field}.")
        if note["pitch"] != int(note["pitch"]) or not 0 <= note["pitch"] <= 127:
            raise ValueError(f"Note {note['id']} needs an integer MIDI pitch 0–127.")
        if note["start"] < 0 or note["start"] >= note["end"]:
            raise ValueError(f"Note {note['id']} needs a positive duration and nonnegative onset.")
        if note.get("hand") not in (None, "left", "right"):
            raise ValueError(f"Note {note['id']} has an unsupported hand assignment.")
        release = note.get("key_release_seconds")
        if release is not None and (isinstance(release, bool) or not isinstance(release, (float, int)) or not math.isfinite(release)):
            raise ValueError(f"Note {note['id']} needs a finite physical release time.")
    return notes


def _locked(note):
    return bool(note["locked"]) if note.get("locked") is not None else note.get("provenance") in {"edited", "manually_added"}


def _fields(before, after):
    return {field: {"before_present": field in before, "after_present": field in after,
                    "before": before.get(field), "after": after.get(field)}
            for field in sorted(before.keys() | after.keys())
            if (field in before) != (field in after) or before.get(field) != after.get(field)}


def _hand(note):
    return note.get("hand") or "unassigned"


def _delta(before, after):
    if before is None or after is None:
        return None
    # Report the stored decimal difference without rounding away small changes.
    return str(Decimal(str(after)) - Decimal(str(before)))


def _protection(before, after, identifiers):
    missing = sorted(set(identifiers) - after.keys())
    changes = []
    for identifier in sorted(set(identifiers) & after.keys()):
        fields = _fields(before[identifier], after[identifier])
        if fields:
            changes.append({"note_id": identifier, "fields": fields,
                            "musical_fields": sorted(set(fields) & set(MUSICAL_FIELDS))})
    return {"note_ids": sorted(identifiers), "count": len(identifiers), "missing_ids": missing,
            "all_fields_exact": not missing and not changes,
            "pitch_timing_pedal_exact": not missing and not any(c["musical_fields"] for c in changes),
            "changed_notes": changes}


def _counter_report(before, after, names):
    def rows(counter):
        return [{**dict(zip(names, key if isinstance(key, tuple) else (key,))), "count": count}
                for key, count in sorted(counter.items())]
    return {"exact": before == after, "before_count": sum(before.values()), "after_count": sum(after.values()),
            "removed": rows(before - after), "added": rows(after - before)}


def _fraction(value):
    if isinstance(value, dict):
        return Fraction(value["numerator"], value["denominator"])
    return Fraction(str(value))


def _score_measures(score):
    measures = defaultdict(lambda: {"attacks": Counter(), "pitch_classes": Counter(), "onsets": Counter()})
    for part in score["parts"]:
        if part.get("is_cue"):
            continue
        for staff in part["staves"]:
            for measure in staff["measures"]:
                result = measures[measure["number"]]
                for voice in measure["voices"]:
                    for event in voice["events"]:
                        if event["kind"] == "rest" or event.get("tie") in {"continue", "stop"}:
                            continue
                        offset = str(_fraction(event["offset"]))
                        for pitch in event["pitches"]:
                            result["attacks"][(offset, pitch % 12)] += 1
                            result["pitch_classes"][pitch % 12] += 1
                            result["onsets"][offset] += 1
    return measures


def compare_scores(before, after):
    if before is None or after is None:
        return {"available": False, "reason": "Supply both canonical scores for per-measure rational-onset checks."}
    first, second = _score_measures(before), _score_measures(after)
    settings = _fields(before["settings"], after["settings"])
    mapping = _fields({k: before.get(k) for k in ("tempo", "origin_seconds", "grid", "bar_duration")},
                      {k: after.get(k) for k in ("tempo", "origin_seconds", "grid", "bar_duration")})
    measures = []
    for number in sorted(first.keys() | second.keys()):
        a, b = first[number], second[number]
        measures.append({"measure": number,
                         "pitch_class_onset_multiset": _counter_report(a["attacks"], b["attacks"], ("offset_beats", "pitch_class")),
                         "pitch_class_multiset": _counter_report(a["pitch_classes"], b["pitch_classes"], ("pitch_class",)),
                         "onset_multiset": _counter_report(a["onsets"], b["onsets"], ("offset_beats",))})
    return {"available": True, "cue_parts_excluded": True, "measure_count": len(measures),
            "settings_changes": settings, "time_mapping_changes": mapping,
            "all_pitch_class_onset_multisets_exact": all(m["pitch_class_onset_multiset"]["exact"] for m in measures),
            "all_pitch_class_multisets_exact": all(m["pitch_class_multiset"]["exact"] for m in measures),
            "all_onset_multisets_exact": all(m["onset_multiset"]["exact"] for m in measures),
            "measures": measures}


def compare_arrangements(before_notes, after_notes, before_score=None, after_score=None):
    before_notes, after_notes = extract_notes(before_notes), extract_notes(after_notes)
    before = {n["id"]: n for n in before_notes}
    after = {n["id"]: n for n in after_notes}
    retained = sorted(before.keys() & after.keys())
    omitted, added = sorted(before.keys() - after.keys()), sorted(after.keys() - before.keys())
    changes, pitches, onsets, releases, hands = [], [], [], [], []
    by_hand = {hand: {"before_count": 0, "after_count": 0, "omitted_ids": [], "added_ids": [],
                     "changed_retained_ids": [], "pitch_changes": 0, "onset_changes": 0, "release_changes": 0}
               for hand in ("left", "right", "unassigned")}
    for note in before_notes:
        by_hand[_hand(note)]["before_count"] += 1
    for note in after_notes:
        by_hand[_hand(note)]["after_count"] += 1
    for identifier in omitted:
        by_hand[_hand(before[identifier])]["omitted_ids"].append(identifier)
    for identifier in added:
        by_hand[_hand(after[identifier])]["added_ids"].append(identifier)
    for identifier in retained:
        a, b = before[identifier], after[identifier]
        fields = _fields(a, b)
        if not fields:
            continue
        change = {"note_id": identifier, "before_hand": _hand(a), "after_hand": _hand(b), "fields": fields}
        changes.append(change)
        hand = by_hand[_hand(a)]
        hand["changed_retained_ids"].append(identifier)
        if a["pitch"] != b["pitch"]:
            semitones = b["pitch"] - a["pitch"]
            pitches.append({"note_id": identifier, "before": a["pitch"], "after": b["pitch"], "semitones": semitones,
                            "kind": "octave" if semitones % 12 == 0 else "chromatic", "before_hand": _hand(a), "after_hand": _hand(b)})
            hand["pitch_changes"] += 1
        if a["start"] != b["start"]:
            onsets.append({"note_id": identifier, "before": a["start"], "after": b["start"],
                           "delta_seconds": _delta(a["start"], b["start"]), "before_hand": _hand(a), "after_hand": _hand(b)})
            hand["onset_changes"] += 1
        changed_release = {f: {**fields[f], "delta_seconds": _delta(a.get(f), b.get(f))}
                           for f in ("end", "key_release_seconds") if f in fields}
        if changed_release:
            releases.append({"note_id": identifier, "fields": changed_release, "before_hand": _hand(a), "after_hand": _hand(b)})
            hand["release_changes"] += 1
        if _hand(a) != _hand(b):
            hands.append({"note_id": identifier, "before": _hand(a), "after": _hand(b)})
    protected = {}
    for role in PROTECTED_ROLES:
        ids = [n["id"] for n in before_notes if n.get("role") == role]
        inferred = [identifier for identifier in ids if (before[identifier].get("arrangement_provenance") or {}).get("inferred_role") == role]
        protected[role] = {"all": _protection(before, after, ids),
                           "explicit": _protection(before, after, set(ids) - set(inferred)),
                           "inferred": _protection(before, after, inferred)}
    locked = _protection(before, after, [n["id"] for n in before_notes if _locked(n)])
    raw_onsets = [Counter(str(_fraction(n["start"])) for n in notes) for notes in (before_notes, after_notes)]
    raw_attacks = [Counter((str(_fraction(n["start"])), n["pitch"] % 12) for n in notes) for notes in (before_notes, after_notes)]
    original_changes = [c["note_id"] for c in changes if "original_timing" in c["fields"]]
    score_comparison = compare_scores(before_score, after_score)
    return {"schema_version": 1, "human_recognizability_validated": False, "human_playability_validated": False,
            "summary": {"before_notes": len(before), "after_notes": len(after), "retained": len(retained),
                        "omitted": len(omitted), "added": len(added), "changed_retained": len(changes),
                        "pitch_changes": len(pitches), "octave_changes": sum(p["kind"] == "octave" for p in pitches),
                        "chromatic_changes": sum(p["kind"] == "chromatic" for p in pitches),
                        "onset_changes": len(onsets), "release_changes": len(releases), "hand_changes": len(hands),
                        "locked_notes_exact": locked["all_fields_exact"],
                        "original_timing_exact_for_retained_notes": not original_changes},
            "ids": {"retained": retained, "omitted": omitted, "added": added},
            "pitch_changes": pitches, "onset_changes": onsets, "release_changes": releases, "hand_changes": hands,
            "all_changed_fields": changes, "changes_by_original_hand": by_hand,
            "omitted_notes": [before[i] for i in omitted], "added_notes": [after[i] for i in added],
            "locked_notes": locked, "protected_roles": protected,
            "original_timing_changed_ids": original_changes,
            "raw_onset_multiset": _counter_report(*raw_onsets, ("onset_seconds_fraction",)),
            "raw_pitch_class_onset_multiset": _counter_report(*raw_attacks, ("onset_seconds_fraction", "pitch_class")),
            "canonical_score": score_comparison,
            "limitations": ["These exact-value checks are proxies, not human listening, recognizability, pianist review, or a guarantee of musical quality.",
                            "Pitch-class equality permits octave changes; exact pitch edits are listed separately.",
                            "Canonical attack multisets count repeated pitches and omit tied continuations and vocal cue parts.",
                            "Protected-role reports distinguish explicit user markings from editorial roles inferred by the arrangement engine.",
                            "Unmarked characteristic riffs, harmonic function, introductions and transitions cannot be established from these comparisons alone."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--before-score", type=Path)
    parser.add_argument("--after-score", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Full audit JSON; use a private, ignored output directory for private project data.")
    parser.add_argument("--require-exact-protected", action="store_true", help="Exit 2 if a locked note or explicitly protected role has any changed/missing field.")
    args = parser.parse_args(argv)
    try:
        before = json.loads(args.before.read_text())
        after = json.loads(args.after.read_text())
        before_score = _score(json.loads(args.before_score.read_text())) if args.before_score else _score(before)
        after_score = _score(json.loads(args.after_score.read_text())) if args.after_score else _score(after)
        if (args.before_score and before_score is None) or (args.after_score and after_score is None):
            raise ValueError("A --before-score/--after-score file must contain a canonical score.")
        report = compare_arrangements(extract_notes(before), extract_notes(after), before_score, after_score)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({**report["summary"], "canonical_score_compared": report["canonical_score"]["available"],
                      "report": str(args.output), "human_recognizability_validated": False}))
    preserved = report["locked_notes"]["all_fields_exact"] and all(role["explicit"]["all_fields_exact"] for role in report["protected_roles"].values())
    return 2 if args.require_exact_protected and not preserved else 0


if __name__ == "__main__":
    raise SystemExit(main())

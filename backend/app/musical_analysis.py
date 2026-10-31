"""Bounded musical-setting suggestions from note evidence, never score authority.

This module uses only the standard library. It neither loads audio/models nor
changes source notes. A periodic onset lattice is evidence for a pulse family,
not evidence for a meter, a bar downbeat, or the performer's intended notation.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Iterable

METHOD = "note-lattice-key-templates-v1"
MAX_SAMPLED_ONSETS = 768
MIN_ONSETS = 8
# Krumhansl-Schmuckler major/minor pitch-class profiles. Correlations below are
# descriptive similarities, not calibrated probabilities of a correct key.
_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
_FIFTHS_MAJOR = (0, -5, 2, -3, 4, -1, 6, 1, -4, 3, -2, 5)
_PITCH_CLASSES = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_MINOR_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B")
_FIFTHS_MINOR = (-3, 4, -1, -6, 1, -4, 3, -2, 5, 0, -5, 2)


def _valid_notes(notes: Iterable[dict]) -> list[dict]:
    result = []
    for item in notes:
        try:
            start, end = float(item["start"]), float(item["end"])
            pitch = int(item["pitch"])
            if math.isfinite(start) and math.isfinite(end) and 0 <= start < end and 0 <= pitch <= 127:
                result.append({"start": start, "end": end, "pitch": pitch})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def _onsets(notes: list[dict]) -> list[float]:
    # Collapse near-simultaneous chord attacks so a seven-note chord cannot
    # outweigh seven melodic beats. Use bounded groups, not transitive chains.
    groups: list[list[float]] = []
    for start in sorted(item["start"] for item in notes):
        if not groups or start - groups[-1][0] > .035:
            groups.append([start])
        else:
            groups[-1].append(start)
    return [statistics.median(group) for group in groups]


def _sample(values: list[float], count: int = MAX_SAMPLED_ONSETS) -> list[float]:
    if len(values) <= count:
        return values
    return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]


def _circle(times: list[float], bpm: float) -> tuple[float, float]:
    """Eighth-note lattice concentration and phase, with uniform outlier weight."""
    period = 30.0 / bpm
    scale = math.tau / period
    cosine = sum(math.cos(t * scale) for t in times)
    sine = sum(math.sin(t * scale) for t in times)
    return math.hypot(cosine, sine) / len(times), (math.atan2(sine, cosine) % math.tau) / scale


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = math.floor(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def grid_fit(notes: list[dict], bpm: float, phase_seconds: float | None = None) -> dict:
    """Compare distinct note attacks with an eighth-note lattice in milliseconds.

    Supplying phase_seconds=0 evaluates an existing zero-origin score grid;
    omitting it chooses the best circular phase for this tempo. This is not an
    accuracy score: wrong notes or regular transcription artifacts can fit too.
    """
    times = _onsets(_valid_notes(notes))
    if not times or not math.isfinite(bpm) or bpm <= 0:
        return {"onset_count": len(times), "median_grid_error_ms": None, "p90_grid_error_ms": None}
    concentration, fitted_phase = _circle(times, bpm)
    phase = fitted_phase if phase_seconds is None else phase_seconds
    period = 30.0 / bpm
    residuals = [abs((t - phase + period / 2) % period - period / 2) * 1000 for t in times]
    return {"onset_count": len(times), "grid_phase_seconds": round(phase, 6),
            "median_grid_error_ms": round(statistics.median(residuals), 3),
            "p90_grid_error_ms": round(_quantile(residuals, .9), 3),
            "onset_concentration": round(concentration, 5),
            "median_error_fraction_of_grid": round(statistics.median(residuals) / (period * 1000), 5),
            "p90_error_fraction_of_grid": round(_quantile(residuals, .9) / (period * 1000), 5)}


def _tempo_candidates(times: list[float], notes: list[dict]) -> list[dict]:
    # Coarse search uses short windows to avoid long-clip phase cancellation.
    # The subsequent fine searches use all sampled attacks across the clip.
    span = times[-1] - times[0]
    centers = (times[0] + min(span / 2, 12), (times[0] + times[-1]) / 2,
               times[-1] - min(span / 2, 12))
    windows = [_sample([t for t in times if abs(t - center) <= 12], 128) for center in centers]
    windows = [window for window in windows if len(window) >= 4]
    if not windows:
        return []
    sample = _sample(times)
    coarse = []
    for half_bpm in range(80, 481):
        bpm = half_bpm / 2
        strength = statistics.mean(_circle(window, bpm)[0] for window in windows)
        coarse.append((strength, bpm))
    seeds = []
    for _, bpm in sorted(coarse, reverse=True):
        if all(abs(bpm - old) > 4 for old in seeds):
            seeds.append(bpm)
        if len(seeds) == 8:
            break
    peaks: list[tuple[float, float]] = []
    # Use a denser search for long clips; cap evaluations to bound request work.
    step = max(.002, min(.025, 7.5 / max(span, 1)))
    for seed in seeds:
        low, high = max(40.0, seed - 1), min(240.0, seed + 1)
        steps = min(1000, math.ceil((high - low) / step))
        candidates = [low + (high - low) * i / steps for i in range(steps + 1)]
        strength, bpm = max((_circle(sample, value)[0], value) for value in candidates)
        # Local golden-section refinement resolves tempo drift beyond the
        # coarse sample precision without fitting thousands of extra grids.
        width = (high - low) / steps
        left, right = max(40., bpm - width), min(240., bpm + width)
        ratio = (math.sqrt(5) - 1) / 2
        a, b = right - ratio * (right - left), left + ratio * (right - left)
        fa, fb = _circle(sample, a)[0], _circle(sample, b)[0]
        for _ in range(22):
            if fa < fb:
                left, a, fa = a, b, fb
                b = left + ratio * (right - left)
                fb = _circle(sample, b)[0]
            else:
                right, b, fb = b, a, fa
                a = right - ratio * (right - left)
                fa = _circle(sample, a)[0]
        bpm = (left + right) / 2
        strength = _circle(sample, bpm)[0]
        if all(abs(bpm - old_bpm) > .2 for _, old_bpm in peaks):
            peaks.append((strength, bpm))
    peaks.sort(reverse=True)
    # Half/double pulse families are intrinsically ambiguous. Prefer a common
    # quarter-note range only among nearly tied fits, and disclose this prior.
    if peaks:
        near = [pair for pair in peaks if pair[0] >= peaks[0][0] - .045 and 70 <= pair[1] <= 160]
        if near:
            chosen = max(near)
            peaks.remove(chosen)
            peaks.insert(0, chosen)
    result = []
    for _, bpm in peaks[:5]:
        rounded = round(bpm, 4)
        result.append({"bpm": rounded, **grid_fit(notes, rounded),
                       "aliases_bpm": [round(value, 4) for value in (bpm / 2, bpm * 2) if 30 <= value <= 300]})
    return result


def _correlation(values: list[float], template: tuple[float, ...], tonic: int) -> float:
    mean_a, mean_b = statistics.mean(values), statistics.mean(template)
    a = [value - mean_a for value in values]
    b = [template[(index - tonic) % 12] - mean_b for index in range(12)]
    denom = math.sqrt(sum(value * value for value in a) * sum(value * value for value in b))
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0.0


def _key_candidates(notes: list[dict]) -> tuple[list[dict], list[float]]:
    # Union same-pitch-class intervals. Octave doublings and duplicated events
    # should not manufacture stronger tonal evidence than a single held note.
    intervals: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for item in notes:
        intervals[item["pitch"] % 12].append((item["start"], item["end"]))
    weights = [0.] * 12
    for pitch_class, rows in intervals.items():
        end = -1.
        for start, stop in sorted(rows):
            weights[pitch_class] += max(0., stop - max(start, end))
            end = max(end, stop)
    total = sum(weights)
    normalized = [value / total if total else 0 for value in weights]
    candidates = []
    for mode, template in (("major", _MAJOR), ("minor", _MINOR)):
        for tonic in range(12):
            tonic_name = (_PITCH_CLASSES if mode == "major" else _MINOR_NAMES)[tonic]
            fifths = (_FIFTHS_MAJOR if mode == "major" else _FIFTHS_MINOR)[tonic]
            candidates.append({"tonic": tonic_name, "mode": mode,
                               "key_signature": fifths,
                               "template_correlation": round(_correlation(normalized, template, tonic), 5)})
    candidates.sort(key=lambda item: (-item["template_correlation"], item["tonic"], item["mode"]))
    selected = candidates[:5]
    if selected:
        first = selected[0]
        if first["mode"] == "major":
            relative_pc = (_PITCH_CLASSES.index(first["tonic"]) + 9) % 12
            relative_mode, relative_name = "minor", _MINOR_NAMES[relative_pc]
        else:
            relative_pc = (_MINOR_NAMES.index(first["tonic"]) + 3) % 12
            relative_mode, relative_name = "major", _PITCH_CLASSES[relative_pc]
        relative = next(row for row in candidates if row["tonic"] == relative_name and row["mode"] == relative_mode)
        if relative["key_signature"] != first["key_signature"]:
            # F# major / D# minor and Gb major / Eb minor are enharmonic;
            # preserve the selected written signature in its relative key.
            relative = {**relative, "key_signature": first["key_signature"],
                        "tonic": "Gb" if relative_mode == "major" else "D#"}
        if relative not in selected:
            selected.append(relative)
    return selected, normalized


def recommend_musical_settings(notes: list[dict]) -> dict:
    """Return reviewable, explicitly estimated settings; never mutate the notes."""
    valid = _valid_notes(notes)
    times = _onsets(valid)
    span = times[-1] - times[0] if times else 0.
    warnings = [
        "Pulse estimates can be half or double the intended quarter-note tempo; compare the alternatives by listening.",
        "Onset periodicity does not identify meter or a bar downbeat. The suggested origin is a provisional grid alignment.",
        "Key-template correlations are uncalibrated similarities, not accuracy percentages; relative major/minor and changes of key remain ambiguous.",
        "Wrong detected notes, rubato, swing, and changing tempo can invalidate a constant straight-eighth grid.",
    ]
    enough_rhythm = (len(times) >= MIN_ONSETS and span >= 4
                     and statistics.median(b - a for a, b in zip(times, times[1:])) <= 1.5)
    tempo_candidates = _tempo_candidates(times, valid) if enough_rhythm else []
    key_candidates, weights = _key_candidates(valid) if valid else ([], [0.] * 12)
    concentration = tempo_candidates[0]["onset_concentration"] if tempo_candidates else 0.
    useful_tempo = bool(tempo_candidates and concentration >= .15
                        and (len(times) * concentration ** 2 >= 14
                             or (len(times) >= 8 and concentration >= .75)))
    useful_key = bool(key_candidates and len([value for value in weights if value > .01]) >= 4
                      and key_candidates[0]["template_correlation"] >= .45)
    settings: dict = {"quantization": "eighth", "confirmed_fields": [], "estimated_fields": []}
    rounding = None
    if useful_tempo:
        best = tempo_candidates[0]
        bpm, phase = best["bpm"], best["grid_phase_seconds"]
        nearest_integer = round(bpm)
        duration = max(item["end"] for item in valid) - min(item["start"] for item in valid)
        drift_ms = duration * abs(bpm / nearest_integer - 1) * 1000
        # A readable integer is useful only if it cannot accumulate a material
        # timing shift. The duration guard matters for hour-long recordings.
        if abs(bpm - nearest_integer) <= .05 and drift_ms <= 25:
            rounding = {"fine_fit_bpm": bpm, "suggested_bpm": nearest_integer,
                        "full_note_span_seconds": round(duration, 6),
                        "end_of_span_drift_ms": round(drift_ms, 3),
                        "reason": "Near-integer display tempo, within 0.05 BPM and at most 25 ms drift across the note span."}
            bpm = float(nearest_integer)
            phase = grid_fit(valid, bpm)["grid_phase_seconds"]
        period = 30 / bpm
        # Align to an equivalent lattice point at/before the first attack. When
        # that point is negative, a half-beat pickup preserves every early note.
        origin = phase + math.floor((times[0] - phase) / period) * period
        if origin >= -1e-7:
            settings.update(tempo_bpm=bpm, first_downbeat_seconds=round(max(0., origin), 6), pickup_beats="0")
        else:
            settings.update(tempo_bpm=bpm, first_downbeat_seconds=round(origin + period, 6), pickup_beats="1/2")
        settings["estimated_fields"].extend(["tempo_bpm", "first_downbeat_seconds", "pickup_beats"])
    else:
        warnings.append("There is insufficient regular onset evidence to recommend a reliable tempo or grid origin; keep these settings unconfirmed.")
    if useful_key:
        settings["key_signature"] = key_candidates[0]["key_signature"]
        settings["estimated_fields"].append("key_signature")
    else:
        warnings.append("There is insufficient pitch-class evidence to recommend a key signature; choose one manually.")
    return {"method": METHOD, "status": "suggestions_available" if useful_tempo or useful_key else "insufficient_evidence",
            "suggested_settings": settings, "tempo_candidates": tempo_candidates, "key_candidates": key_candidates,
            "evidence": {"note_count": len(valid), "onset_count": len(times), "onset_span_seconds": round(span, 6),
                         "sampled_onset_count": min(len(times), MAX_SAMPLED_ONSETS),
                         "pitch_class_weights": {name: round(value, 6) for name, value in zip(_PITCH_CLASSES, weights)},
                         "baseline_120_zero_origin": grid_fit(valid, 120, 0),
                         "tempo_rounding": rounding,
                         "tempo_preference": "70–160 BPM only when eighth-grid fits are nearly tied; half/double alternatives remain valid."},
            "assumptions": ["Straight eighth-note lattice is a notation proposal, not measured meter.",
                            "No time signature, first full-bar downbeat, actual pickup, or performer hand was inferred."],
            "warnings": warnings}

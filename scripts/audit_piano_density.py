"""Read-only piano density/fidelity audit, independent of the arranging search.

This measures saved events and their explicitly selected notation grid. It is
not an audio-accuracy test, identification of actual hands, or pianist review.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import json
import math
from pathlib import Path

from scripts.compare_piano_arrangements import extract_notes

GRIDS = {'quarter': Fraction(1), 'eighth': Fraction(1, 2), 'sixteenth': Fraction(1, 4),
         'eighth_triplet': Fraction(1, 3), 'sixteenth_triplet': Fraction(1, 6)}
ROLES = ('melody', 'vocal_cue', 'bass', 'riff', 'countermelody')


def canonical(document):
    return document.get('score', document) if isinstance(document, dict) else None


def event_timeline(notes, score=None):
    """Physical key intervals: end-exclusive, cue-free, no invented pedal lifts."""
    settings = score['settings'] if score else None
    bpm = Fraction(str(settings.get('tempo_bpm') or score['tempo']['bpm'])) if score else None
    origin = (Fraction(str(settings['first_downbeat_seconds'])) - Fraction(settings['pickup_beats']) * 60 / bpm) if score else 0
    events = []
    for note in extract_notes(notes):
        if note.get('role') == 'vocal_cue':
            continue
        start = Fraction(str(note['start']))
        release = note.get('key_release_seconds') if note.get('pedal_sustained') else None
        end = Fraction(str(release if release is not None else note['end']))
        if not start < end <= Fraction(str(note['end'])):
            raise ValueError('Invalid physical release in density input.')
        pitch = note['pitch']
        if settings:
            grid = GRIDS['sixteenth'] if settings.get('melody_quantization') == 'sixteenth' and note.get('role') == 'melody' else GRIDS[settings['quantization']]
            snap = lambda t: math.floor((t - origin) * bpm / (60 * grid) + Fraction(1, 2)) * grid * 60 / bpm + origin
            start, end = snap(start), max(snap(end), snap(start) + grid * 60 / bpm)
            pitch += settings.get('transpose_semitones', 0)
        events.append({'id': note['id'], 'pitch': pitch, 'start': start, 'end': end,
                       'hand': note.get('hand') or 'unassigned', 'role': note.get('role')})
    return events


def hand_density(events, duration=None):
    if not events:
        return {'window_seconds': float(duration or 0), 'hands': {}}
    beginning = min(Fraction(0), min(n['start'] for n in events))
    ending = max(max(n['end'] for n in events), Fraction(str(duration or 0)))
    changes = defaultdict(lambda: {'start': [], 'end': []})
    for event in events:
        changes[event['start']]['start'].append(event)
        changes[event['end']]['end'].append(event)
    states = {hand: Counter() for hand in ('left', 'right', 'unassigned')}
    stats = {hand: {'seconds_at_key_count': Counter(), 'attacks_at_held_key_count': Counter(),
                    'attacks_by_new_keys': Counter(), 'maximum_span_semitones': 0,
                    'maximum_keys': 0, 'dense_intervals': []} for hand in states}
    previous = beginning
    for moment in sorted(set(changes) | {ending}):
        for hand, active in states.items():
            keys = sorted(k for k, count in active.items() if count)
            span = keys[-1] - keys[0] if keys else 0
            item = stats[hand]
            item['seconds_at_key_count'][len(keys)] += moment - previous
            item['maximum_span_semitones'] = max(item['maximum_span_semitones'], span)
            item['maximum_keys'] = max(item['maximum_keys'], len(keys))
            if len(keys) >= 4 and moment > previous:
                item['dense_intervals'].append({'start': float(previous), 'end': float(moment), 'keys': len(keys), 'span': span})
        for event in changes[moment]['end']:
            states[event['hand']][event['pitch']] -= 1
        for event in changes[moment]['start']:
            states[event['hand']][event['pitch']] += 1
        for hand, item in stats.items():
            attacked = {n['pitch'] for n in changes[moment]['start'] if n['hand'] == hand}
            if attacked:
                held = sum(count > 0 for count in states[hand].values())
                item['attacks_by_new_keys'][len(attacked)] += 1
                item['attacks_at_held_key_count'][held] += 1
        previous = moment
    for item in stats.values():
        active_seconds = sum(value for key, value in item['seconds_at_key_count'].items() if key)
        item['active_seconds'] = round(float(active_seconds), 6)
        item['seconds_with_four_or_more_keys'] = round(float(sum(value for key, value in item['seconds_at_key_count'].items() if key >= 4)), 6)
        item['seconds_with_five_or_more_keys'] = round(float(sum(value for key, value in item['seconds_at_key_count'].items() if key >= 5)), 6)
        item['attack_moments'] = sum(item['attacks_by_new_keys'].values())
        item['four_or_more_key_attack_moments'] = sum(value for key, value in item['attacks_at_held_key_count'].items() if key >= 4)
        item['five_or_more_key_attack_moments'] = sum(value for key, value in item['attacks_at_held_key_count'].items() if key >= 5)
        for key in ('seconds_at_key_count', 'attacks_at_held_key_count', 'attacks_by_new_keys'):
            item[key] = {str(n): round(float(value), 6) if key == 'seconds_at_key_count' else value for n, value in sorted(item[key].items()) if value}
    return {'window_start_seconds': float(beginning), 'window_end_seconds': float(ending),
            'window_seconds': float(ending - beginning), 'hands': stats}


def notation_density(score):
    if not score:
        return None
    distribution = Counter()
    dense = []
    for part in score['parts']:
        if part.get('is_cue'):
            continue
        for staff in part['staves']:
            for measure in staff['measures']:
                voices = sum(any(e['kind'] != 'rest' for e in voice['events']) for voice in measure['voices'])
                distribution[voices] += 1
                if voices > 2:
                    dense.append({'part': part['id'], 'staff': staff['id'], 'measure': measure['number'], 'voices': voices})
    return {'maximum_sounding_voices': max(distribution, default=0),
            'staff_measures_by_sounding_voices': dict(sorted(distribution.items())), 'dense_staff_measures': dense}


def canonical_key_occupancy(score):
    """Read actual notation segments/hand pitches; ties are joined by union below."""
    result = []
    bpm = Fraction(str(score['tempo']['bpm']))
    origin = Fraction(str(score['origin_seconds']))
    value = lambda rational: Fraction(rational['numerator'], rational['denominator'])
    for part in score['parts']:
        if part.get('is_cue'):
            continue
        for staff in part['staves']:
            for measure in staff['measures']:
                for voice in measure['voices']:
                    for event in voice['events']:
                        if event['kind'] == 'rest':
                            continue
                        start = origin + (value(measure['start']) + value(event['offset'])) * 60 / bpm
                        end = start + value(event['duration']) * 60 / bpm
                        hands = event.get('hand_pitches') or {}
                        assigned = {pitch for pitches in hands.values() for pitch in pitches}
                        for hand, pitches in [*hands.items(), ('unassigned', set(event['pitches']) - assigned)]:
                            result.extend({'id': event['id'], 'hand': hand, 'pitch': pitch, 'start': start, 'end': end} for pitch in pitches)
    return result


def occupancy_union(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[(event['hand'], event['pitch'])].append((event['start'], event['end']))
    result = {}
    for key, spans in grouped.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result[key] = merged
    return result


def canonical_occupancy_check(notes, score):
    if not score:
        return None
    expected = occupancy_union(event_timeline(notes, score))
    actual = occupancy_union(canonical_key_occupancy(score))
    differences = []
    for hand, pitch in sorted(expected.keys() | actual.keys()):
        if expected.get((hand, pitch)) != actual.get((hand, pitch)):
            spans = lambda items: [[str(a), str(b)] for a, b in items]
            differences.append({'hand': hand, 'pitch': pitch,
                                'projected_intervals': spans(expected.get((hand, pitch), [])),
                                'canonical_intervals': spans(actual.get((hand, pitch), []))})
    return {'exact_hand_pitch_occupancy': not differences, 'differences': differences,
            'method': 'Exact union of actual canonical hand/pitch segments compared with independently projected source events; ties and duplicate keys do not add physical keys.'}


def retention(before, after):
    a, b = {n['id']: n for n in extract_notes(before)}, {n['id']: n for n in extract_notes(after)}
    common = set(a) & set(b)
    def group(ids):
        present = set(ids) & set(b)
        return {'before': len(ids), 'retained': len(present), 'omitted': sorted(set(ids) - set(b)),
                'pitch_changed': sum(a[i]['pitch'] != b[i]['pitch'] for i in present),
                'pitch_class_changed': sum(a[i]['pitch'] % 12 != b[i]['pitch'] % 12 for i in present),
                'start_changed': sum(a[i]['start'] != b[i]['start'] for i in present),
                'end_changed': sum(a[i]['end'] != b[i]['end'] for i in present),
                'physical_release_changed': sum(a[i].get('key_release_seconds') != b[i].get('key_release_seconds') for i in present),
                'velocity_changed': sum(a[i].get('velocity') != b[i].get('velocity') for i in present),
                'role_changed': sum(a[i].get('role') != b[i].get('role') for i in present),
                'original_timing_changed': sum(a[i].get('original_timing') != b[i].get('original_timing') for i in present)}
    roles = {}
    for role in ROLES:
        ids = [i for i, note in a.items() if note.get('role') == role]
        inferred = [i for i in ids if (a[i].get('arrangement_provenance') or {}).get('inferred_role') == role]
        roles[role] = {'all': group(ids), 'explicit': group(set(ids) - set(inferred)), 'inferred': group(inferred)}
    locked = [i for i, n in a.items() if (bool(n['locked']) if n.get('locked') is not None else n.get('provenance') in {'edited', 'manually_added'})]
    attack_a = Counter((n['start'], n['pitch'] % 12) for n in a.values() if n.get('role') != 'vocal_cue')
    attack_b = Counter((n['start'], n['pitch'] % 12) for n in b.values() if n.get('role') != 'vocal_cue')
    onset_a = {n['start'] for n in a.values() if n.get('role') != 'vocal_cue'}
    onset_b = {n['start'] for n in b.values() if n.get('role') != 'vocal_cue'}
    return {'before_events': len(a), 'after_events': len(b), 'retained_events': len(common),
            'omitted_ids': sorted(set(a) - set(b)), 'added_ids': sorted(set(b) - set(a)),
            'retained': group(common), 'roles': roles, 'locked_or_manual': group(locked),
            'pitch_change_intervals': dict(sorted(Counter(b[i]['pitch'] - a[i]['pitch'] for i in common if a[i]['pitch'] != b[i]['pitch']).items())),
            'hand_changes': sum(a[i].get('hand') != b[i].get('hand') for i in common),
            'all_piano_attack_times_retained': onset_a <= onset_b,
            'before_distinct_piano_attack_times': len(onset_a), 'after_distinct_piano_attack_times': len(onset_b),
            'removed_pitch_class_attack_events': sum((attack_a - attack_b).values()),
            'added_pitch_class_attack_events': sum((attack_b - attack_a).values()),
            'unmarked_motifs_verified': False}


def compare(before, after, before_score, after_score=None, duration=None):
    before_notes, after_notes = extract_notes(before), extract_notes(after)
    projected_score = after_score or before_score
    before_grid, after_grid = event_timeline(before_notes, before_score), event_timeline(after_notes, projected_score)
    before_onsets, after_onsets = {n['start'] for n in before_grid}, {n['start'] for n in after_grid}
    grid_attacks_a = Counter((n['start'], n['pitch'] % 12) for n in before_grid)
    grid_attacks_b = Counter((n['start'], n['pitch'] % 12) for n in after_grid)
    grid_retention = {'before_distinct_piano_attack_times': len(before_onsets), 'after_distinct_piano_attack_times': len(after_onsets),
                      'all_piano_attack_times_retained': before_onsets <= after_onsets,
                      'removed_pitch_class_attack_events': sum((grid_attacks_a - grid_attacks_b).values()),
                      'added_pitch_class_attack_events': sum((grid_attacks_b - grid_attacks_a).values())}
    return {'schema_version': 1, 'method': 'Independent end-exclusive key-occupancy sweep and exact saved-event comparisons',
            'retention': retention(before_notes, after_notes), 'score_grid_attack_retention': grid_retention,
            'before': {'saved_seconds': hand_density(event_timeline(before_notes), duration),
                       'score_grid': hand_density(event_timeline(before_notes, before_score), duration),
                       'notation': notation_density(before_score)},
            'after': {'saved_seconds': hand_density(event_timeline(after_notes), duration),
                      'score_grid': hand_density(event_timeline(after_notes, projected_score), duration),
                      'notation': notation_density(after_score)},
            'canonical_consistency': {'before': canonical_occupancy_check(before_notes, before_score),
                                      'after': canonical_occupancy_check(after_notes, after_score)},
            'limitations': ['Counts measure unique physical keys per explicitly assigned hand, with note-off processed before a simultaneous attack; vocal cues are excluded.',
                            'Score-grid projection independently applies saved quantization and explicit physical pedal release; raw saved-time results are also retained.',
                            'Explicitly marked riffs/countermelodies and lead/bass roles are audited; unmarked characteristic motifs, actual performed notes and musical recognizability are not established.',
                            'No audio listening, pianist validation, AI-origin detection, random variation, or transcription-accuracy claim is made.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('before', 'after', 'before-score', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--after-score', type=Path)
    parser.add_argument('--duration', type=float)
    args = parser.parse_args(argv)
    load = lambda path: json.loads(path.read_text())
    result = compare(load(args.before), load(args.after), canonical(load(args.before_score)),
                     canonical(load(args.after_score)) if args.after_score else None, args.duration)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    compact = {'retention': {k: result['retention'][k] for k in ('before_events', 'after_events', 'retained_events', 'hand_changes', 'all_piano_attack_times_retained')},
               'density': {stage: {hand: {k: values[k] for k in ('maximum_keys', 'maximum_span_semitones', 'seconds_with_four_or_more_keys', 'seconds_with_five_or_more_keys', 'four_or_more_key_attack_moments', 'five_or_more_key_attack_moments')} for hand, values in result[stage]['score_grid']['hands'].items() if hand != 'unassigned'} for stage in ('before', 'after')}}
    print(json.dumps(compact))


if __name__ == '__main__':
    main()

"""Explicit, bounded arrangement timing for audio-reviewed repeated figures.

Independent nearest-grid rounding can collapse successive performed attacks.
This pass retains original source-chain indices and assigns consecutive cells;
missing/declined source IDs remain gaps. It never invents an attack or changes a
pitch, velocity, hand, or the original_timing evidence.
"""
from __future__ import annotations

import copy
import math
from fractions import Fraction
from statistics import median

from backend.app.piano import _locked
from backend.app.score import ScoreSettings, _snap


def quantize_reviewed_phrases(notes, original_source_notes, reviewed_evidence,
                             score_settings, *, max_displacement_seconds=.1,
                             max_chains=128, max_notes=4096):
    settings = ScoreSettings.model_validate(score_settings)
    if settings.tempo_bpm is None:
        raise ValueError('Phrase timing needs an explicitly selected tempo.')
    if settings.quantization != 'sixteenth':
        raise ValueError('Reviewed rapid figures require a sixteenth main grid.')
    if not 0 < max_displacement_seconds <= .1:
        raise ValueError('Onset displacement must be positive and at most 100 ms.')
    if not isinstance(max_chains, int) or not 0 <= max_chains <= 256:
        raise ValueError('Chain budget must be an integer from 0 to 256.')
    if not isinstance(max_notes, int) or not 0 <= max_notes <= 10000:
        raise ValueError('Note budget must be an integer from 0 to 10000.')
    if len(notes) > 10000 or len(original_source_notes) > 10000:
        raise ValueError('Phrase review supports at most 10,000 input notes.')
    result = copy.deepcopy(notes)
    current = {n['id']: n for n in result}
    source = {n['id']: n for n in original_source_notes}
    if len(current) != len(notes) or len(source) != len(original_source_notes):
        raise ValueError('Stable source and candidate note IDs must be unique.')
    if isinstance(reviewed_evidence, dict):
        reviewed_evidence = reviewed_evidence.get('candidates', [])
    bpm = Fraction(str(settings.tempo_bpm))
    grid_seconds = Fraction(1, 4) * 60 / bpm
    origin = Fraction(str(settings.first_downbeat_seconds)) - Fraction(settings.pickup_beats) * 60 / bpm
    limit = Fraction(str(max_displacement_seconds))
    changes, unresolved, decisions = [], [], []
    seen, touched = set(), set()
    chains = []
    for record in reviewed_evidence:
        ids = tuple(record.get('chain_ids', []))
        if ids and ids not in seen:
            chains.append(ids)
            seen.add(ids)
    processed_notes = 0
    for chain_index, ids in enumerate(chains):
        if chain_index >= max_chains:
            unresolved.append({'chain_ids': list(ids), 'reason': 'Chain review budget reached.'})
            continue
        if len(ids) < 3 or len(set(ids)) != len(ids) or any(nid not in source for nid in ids):
            unresolved.append({'chain_ids': list(ids), 'reason': 'Evidence must identify at least three distinct original source notes.'})
            continue
        chain = sorted((source[nid] for nid in ids), key=lambda n: (n['start'], n['id']))
        ids = tuple(n['id'] for n in chain)
        original_starts = [Fraction(str(n['start'])) for n in chain]
        gaps = [b-a for a,b in zip(original_starts, original_starts[1:])]
        if len({n['pitch'] for n in chain}) != 1 or any(not Fraction('0.09') <= gap <= Fraction('0.21') for gap in gaps):
            unresolved.append({'chain_ids': list(ids), 'reason': 'Original notes do not form the reviewed rapid same-pitch chain.'})
            continue
        retained = [(i,current[nid]) for i,nid in enumerate(ids) if nid in current]
        if not retained:
            continue
        if any(n['id'] in touched for _,n in retained):
            unresolved.append({'chain_ids': list(ids), 'reason': 'Overlapping chain evidence requires manual review.'})
            continue
        if processed_notes + len(retained) > max_notes:
            unresolved.append({'chain_ids': list(ids), 'reason': 'Note review budget reached.'})
            continue
        if any(_locked(n) or n.get('key_release_seconds') is not None or n.get('pedal_sustained') for _,n in retained):
            unresolved.append({'chain_ids': list(ids), 'reason': 'Manual timing or explicit physical/pedal release requires manual review.'})
            continue
        # Median residual proposes the phrase origin. Compare neighboring exact
        # integer cells and accept only fits meeting the hard displacement bound.
        residual = median(start-i*grid_seconds for i,start in enumerate(original_starts))
        seed = math.floor((residual-origin)/grid_seconds)
        fits = []
        for cell in range(max(0,seed-1), max(0,seed+2)+1):
            assigned = [origin+(cell+i)*grid_seconds for i in range(len(chain))]
            errors = [abs(a-b) for a,b in zip(assigned, original_starts)]
            if max(errors) <= limit:
                fits.append((sum(e*e for e in errors), max(errors), cell, assigned))
        if not fits:
            unresolved.append({'chain_ids': list(ids), 'reason': 'No consecutive-sixteenth phrase fits every original source onset within 100 ms.'})
            continue
        _, worst_error, cell, assigned = min(fits)
        for i,n in retained:
            before = {'start': n['start'], 'end': n['end']}
            start = assigned[i]
            end = max(start+grid_seconds, origin+_snap((Fraction(str(n['end']))-origin)/grid_seconds, Fraction(1))*grid_seconds)
            # Use ORIGINAL chain position, even if the following source event
            # was declined. Never fill the missing event's cell with a long hold.
            if i+1 < len(chain):
                end = min(end, assigned[i+1])
            after = {'start': float(start), 'end': float(end)}
            processed_notes += 1
            touched.add(n['id'])
            if before == after:
                continue
            edit = {'note_id': n['id'], 'source_note_id': ids[i], 'action': 'phrase_quantization',
                    'before': before, 'after': after,
                    'source_timing': {'start': source[ids[i]]['start'], 'end': source[ids[i]]['end']},
                    'source_chain_index': i, 'source_chain_ids': list(ids),
                    'grid_cell': cell+i, 'onset_displacement_seconds': float(start-original_starts[i]),
                    'reason': 'Explicit consecutive-sixteenth arrangement timing preserves distinct audio-reviewed reattacks; original timing remains separate.'}
            previous = n.get('arrangement_provenance') or {}
            field_changes = [{ 'field': key, 'before': before[key], 'after': after[key], 'reason': edit['reason'] }
                             for key in ('start','end') if before[key] != after[key]]
            n.update(after)
            n['provenance'],n['locked'] = 'edited',False
            n['arrangement_provenance'] = {**previous, 'kind': 'derived_accompaniment',
                'changes': list(previous.get('changes', [])) + field_changes}
            n['phrase_quantization'] = {'source_chain_ids': list(ids), 'source_chain_index': i,
                'grid_cell': cell+i, 'max_onset_displacement_seconds': float(worst_error),
                'method': 'reviewed-consecutive-sixteenths-v1'}
            changes.append(edit)
        decisions.append({'source_chain_ids': list(ids), 'retained_ids': [n['id'] for _,n in retained],
                          'missing_ids': [nid for nid in ids if nid not in current],
                          'first_cell': cell, 'max_onset_displacement_seconds': float(worst_error)})
    return {'notes': result, 'changes': changes, 'unresolved': unresolved,
            'decisions': decisions, 'processed_notes': processed_notes,
            'grid_seconds': float(grid_seconds), 'origin_seconds': float(origin),
            'method': 'reviewed-consecutive-sixteenths-v1'}

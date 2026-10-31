"""Generated fixtures verify bounded fidelity-preserving hand repair."""
from copy import deepcopy

from scripts.audio_hand_repair import repair_audio_hands, repair_audio_local

SETTINGS = {'tempo_bpm': 60, 'quantization': 'sixteenth', 'readability_mode': 'accompaniment'}


def note(identifier, pitch, start=0, end=1, hand='left', role='harmony', **extra):
    return {'id': identifier, 'pitch': pitch, 'start': start, 'end': end,
            'velocity': 67, 'hand': hand, 'role': role, 'locked': False,
            'provenance': 'detected',
            'original_timing': {'start': start, 'end': end, 'pitch': pitch, 'velocity': 67},
            **extra}


def audible(n):
    return {key: n.get(key) for key in ('id', 'pitch', 'start', 'end', 'velocity', 'original_timing')}


def test_hand_only_repair_preserves_audio_evidence_and_logs_assignment():
    source = [note('bass', 36, role='bass', audio_review={'kind': 'corroborated_bass_source'}),
              note('lead', 60, role='melody')]
    original = deepcopy(source)
    result = repair_audio_hands(source, SETTINGS)
    assert source == original
    assert result['before']['summary']['error_count'] > 0
    assert result['after']['summary']['error_count'] == 0
    assert [audible(n) for n in result['notes']] == [audible(n) for n in source]
    assert len(result['changes']) == 1
    changed = next(n for n in result['notes'] if n['id'] == result['changes'][0]['note_id'])
    assert changed['provenance'] == 'edited' and changed['locked'] is False
    assert changed['arrangement_provenance']['changes'][-1]['field'] == 'hand'


def test_protected_three_register_conflict_remains_explicit_without_losing_notes():
    source = [note('bass', 36, role='bass', audio_review={'kind': 'corroborated_bass_source'}),
              note('riff', 60, role='riff', audio_review={'kind': 'restored_source_reattack'}),
              note('lead', 72, hand='right', role='melody')]
    result = repair_audio_local(source, SETTINGS)
    assert result['after']['summary']['error_count'] > 0
    assert result['initial_hand_only_unresolved'][0]['kind'] == 'not_two_colorable'
    assert [audible(n) for n in result['notes']] == [audible(n) for n in source]
    assert not any(change['field'] != 'hand' for change in result['changes'])


def test_only_unreviewed_secondary_material_may_change_octave():
    source = [note('bass', 36, role='bass', audio_review={'kind': 'corroborated_bass_source'}),
              note('secondary', 60, hand='right'),
              note('lead', 72, hand='right', role='melody')]
    result = repair_audio_local(source, SETTINGS)
    assert result['after']['summary']['error_count'] == 0
    actual = {n['id']: n for n in result['notes']}
    assert audible(actual['bass']) == audible(source[0])
    assert audible(actual['lead']) == audible(source[2])
    assert abs(actual['secondary']['pitch'] - source[1]['pitch']) == 12
    assert actual['secondary']['original_timing'] == source[1]['original_timing']
    assert {n['id'] for n in result['notes']} == {n['id'] for n in source}
    assert any(c['field'] == 'pitch' for c in result['changes'])


def test_shortened_secondary_release_keeps_physical_release_valid():
    source = [note('secondary', 60, end=1.5, key_release_seconds=1.5, pedal_sustained=True),
              note('lead', 72, end=2, hand='right', role='melody'),
              note('bass', 48, start=1, end=2, role='bass', audio_review={'kind': 'corroborated_bass_source'})]
    result = repair_audio_local(source, SETTINGS)
    actual = next(n for n in result['notes'] if n['id'] == 'secondary')
    assert result['after']['summary']['error_count'] == 0
    assert actual['end'] == actual['key_release_seconds'] == 1
    assert actual['pedal_sustained'] is True
    assert actual['pitch'] == 60
    assert actual['original_timing'] == source[0]['original_timing']
    assert {c['field'] for c in result['changes']} == {'end', 'key_release_seconds'}


def test_budgets_are_hard_and_manual_hand_assignments_are_preserved():
    source = [note('bass', 36), note('high', 60)]
    no_work = repair_audio_local(source, SETTINGS, max_audits=0)
    assert no_work['notes'] == source and no_work['audits'] == 0 and no_work['changes'] == []
    one_audit = repair_audio_local(source, SETTINGS, max_audits=1)
    assert one_audit['audits'] <= 1
    locked = [note('bass', 36, locked=True), note('high', 60, locked=True)]
    result = repair_audio_local(locked, SETTINGS)
    assert result['notes'] == locked and result['changes'] == []
    assert result['after']['summary']['error_count'] > 0


def test_compound_hand_exchange_avoids_a_temporary_four_key_load():
    # The long riff must leave the left hand before a later low bass attack.
    # Moving it alone creates four RH keys; exchanging a simultaneous upper
    # tone keeps three keys and preserves every source pitch and release.
    source = [
        note('left-low', 60, end=.25, audio_review={'kind': 'fixture'}),
        note('left-inner', 64, end=.75, audio_review={'kind': 'fixture'}),
        note('riff', 67, end=1, role='riff', audio_review={'kind': 'fixture'}),
        note('right-low', 58, end=1, hand='right', audio_review={'kind': 'fixture'}),
        note('right-inner', 62, end=.25, hand='right', audio_review={'kind': 'fixture'}),
        note('lead', 65, end=.25, hand='right', role='melody'),
        note('bass', 44, start=.75, end=1.5, role='bass', audio_review={'kind': 'fixture'}),
    ]
    hand_only = repair_audio_hands(source, SETTINGS)
    assert hand_only['after']['summary']['error_count'] > 0
    assert hand_only['changes'] == []
    result = repair_audio_local(source, SETTINGS)
    assert result['after']['summary']['error_count'] == 0
    assert result['after']['density']['over_target_frames'] == 0
    assert len(result['changes']) == 2
    assert all(change['field'] == 'hand' for change in result['changes'])
    assert [audible(n) for n in result['notes']] == [audible(n) for n in source]
    assert result['audits'] <= 384

"""Generated event fixtures test independent measurement, not musical quality."""
from copy import deepcopy
from fractions import Fraction

from scripts.audit_piano_density import compare, event_timeline, hand_density, retention, canonical_occupancy_check


def note(identifier, pitch=60, start=0, end=1, **extra):
    return {'id': identifier, 'pitch': pitch, 'start': start, 'end': end, 'hand': 'right',
            'velocity': 80, 'locked': False, 'provenance': 'detected',
            'original_timing': {'pitch': pitch, 'start': start, 'end': end}, **extra}


def score(**extra):
    return {'settings': {'tempo_bpm': 100, 'first_downbeat_seconds': 0, 'pickup_beats': '0',
                         'quantization': 'eighth', 'melody_quantization': 'sixteenth',
                         'transpose_semitones': 0, **extra}, 'tempo': {'bpm': 100}, 'origin_seconds': 0, 'parts': []}


def test_actual_canonical_segments_preserve_hands_across_ties_and_detect_duration_loss():
    rational = lambda n, d=1: {'numerator': n, 'denominator': d}
    segment = lambda offset: {'id': str(offset), 'kind': 'chord', 'offset': rational(offset),
                             'duration': rational(1), 'pitches': [60, 64],
                             'hand_pitches': {'left': [60], 'right': [64]}}
    document = score()
    document['parts'] = [{'staves': [{'measures': [{'start': rational(0), 'voices': [
        {'events': [segment(0), segment(1)]}]}]}]}]
    notes = [note('low', 60, 0, 1.2, hand='left'), note('high', 64, 0, 1.2)]
    assert canonical_occupancy_check(notes, document)['exact_hand_pitch_occupancy']
    document['parts'][0]['staves'][0]['measures'][0]['voices'][0]['events'][1]['duration'] = rational(1, 2)
    result = canonical_occupancy_check(notes, document)
    assert not result['exact_hand_pitch_occupancy']
    assert len(result['differences']) == 2


def test_duration_distribution_counts_unique_keys_and_releases_before_attacks():
    notes = [note('c'), note('c-double'), note('e',64), note('g',67), note('b',71),
             note('five',72,.5,1), note('next',74,1,2)]
    result = hand_density(event_timeline(notes), 3)['hands']['right']
    assert result['seconds_at_key_count'] == {'0': 1., '1': 1., '4': .5, '5': .5}
    assert result['maximum_keys'] == 5
    assert result['seconds_with_four_or_more_keys'] == 1
    assert result['attacks_at_held_key_count'] == {'1': 1, '4': 1, '5': 1}
    assert result['attacks_by_new_keys'] == {'1': 2, '4': 1}


def test_cues_and_unconfirmed_pedal_are_not_treated_as_piano_hand_relief():
    notes = [note('held', end=2, key_release_seconds=.5, pedal_sustained=False),
             note('cue',72,role='vocal_cue'), note('early-release',64,end=2,key_release_seconds=.5,pedal_sustained=True)]
    events = event_timeline(notes)
    assert len(events) == 2
    assert {n['id']:n['end'] for n in events} == {'held': Fraction(2), 'early-release': Fraction(1,2)}
    result = hand_density(events, 2)['hands']['right']
    assert result['seconds_at_key_count'] == {'1': 1.5, '2': .5}


def test_exact_half_grid_release_reveals_the_real_span_overlap():
    # Regression for the actual numeric boundary, using generated IDs/events.
    settings = score(first_downbeat_seconds=.017627)
    notes = [note('lower',59,131.93868480725624,132.467627,role='harmony'),
             note('inner',67,131.93868480725624,132.467627,role='harmony'),
             note('lead',77,132.467627,132.917627,role='melody')]
    events = event_timeline(notes, settings)
    assert events[0]['end'] == Fraction('132.617627')
    assert events[2]['start'] == Fraction('132.467627')
    result = hand_density(events)['hands']['right']
    assert result['maximum_span_semitones'] == 18
    assert result['maximum_keys'] == 3


def test_melody_has_an_independent_sixteenth_grid():
    events = event_timeline([note('lead',72,.15,.3,role='melody'), note('inner',60,.15,.3,role='harmony')],score())
    assert events[0]['start'] == Fraction(3,20)
    assert events[1]['start'] == Fraction(3,10)
    assert events[1]['end'] == Fraction(3,5)


def test_retention_cannot_hide_octaves_omissions_or_relabeling():
    before = [note('bass',48,role='bass',arrangement_provenance={'inferred_role':'bass'}),
              note('riff',64,role='riff'),note('lead',72,role='melody'),note('optional',67)]
    after = deepcopy(before[:-1]);after[0]['pitch']+=12;after[1]['role']='harmony'
    report = retention(before,after)
    assert report['omitted_ids'] == ['optional']
    assert report['pitch_change_intervals'] == {12:1}
    assert report['roles']['bass']['inferred']['pitch_changed'] == 1
    assert report['roles']['bass']['inferred']['pitch_class_changed'] == 0
    assert report['roles']['riff']['explicit']['role_changed'] == 1
    assert report['roles']['melody']['all']['retained'] == 1
    assert not report['unmarked_motifs_verified']


def test_raw_and_score_attack_retention_are_distinguished():
    before=[note('main',60,0,1), note('jittered-inner',64,.01,1)]
    after=before[:1]
    report=compare(before,after,score(),duration=2)
    assert not report['retention']['all_piano_attack_times_retained']
    assert report['score_grid_attack_retention']['all_piano_attack_times_retained']
    assert report['score_grid_attack_retention']['removed_pitch_class_attack_events']==1


def test_manual_default_locks_and_original_timing_changes_are_exposed():
    before=[note('manual',provenance='edited',locked=None),note('explicit-unlocked',provenance='edited',locked=False)]
    after=deepcopy(before);after[0]['original_timing']['end']=5
    report=retention(before,after)
    assert report['locked_or_manual']['before']==1
    assert report['locked_or_manual']['original_timing_changed']==1


def test_measurement_does_not_mutate_notes_or_depend_on_audio_origin_labels():
    notes=[note('a'),note('b',64,1,2)]
    original=deepcopy(notes)
    first=compare({'notes':notes,'source':'AI song'},notes,score(),duration=2)
    second=compare({'notes':notes,'source':'acoustic recording'},notes,score(),duration=2)
    assert first==second
    assert notes==original

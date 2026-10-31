"""Generated notes/PCM test editorial provenance and rendering, not song accuracy."""
from __future__ import annotations

import copy
import uuid

import numpy as np
import pretty_midi
import pytest

from scripts.refine_audio_comparison import restore_evidence, restore_source_register
from scripts.render_midi_comparison import render


def note(name, pitch=60, start=0, end=.3, velocity=64, **extras):
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, 'explicit-audio-review-test:' + name))
    return dict(id=identifier, pitch=pitch, start=start, end=end, velocity=velocity,
                part_id='part-1', provenance='detected', source_note_ids=[identifier],
                original_timing=dict(pitch=pitch,start=start,end=end,velocity=velocity), **extras)


def sources():
    first, missing = note('first'), note('missing',start=.4,end=.6,velocity=83)
    current = [{**copy.deepcopy(first), 'pitch':72,'start':.1,'end':.55,'velocity':92,'hand':'right','locked':False,'role':'harmony'}]
    bass = [note('bass',pitch=36,start=.2,end=.7)]
    repeated = [dict(id=missing['id'],pitch=60,start=.4,end=.6,velocity=83,
                     chain_ids=[first['id'],missing['id']],retained_neighbours=[{'id':first['id']}],
                     nearest_flux_peak_seconds=.405,peak_delta_ms=5)]
    accepted = [dict(id=bass[0]['id'],pitch=36,start=.2,end=.7,independent_support_fraction=.9)]
    return current, [first,missing], bass, repeated, accepted


def test_restore_source_timing_pitch_class_and_provenance_without_mutating_inputs():
    args = sources()
    baseline = copy.deepcopy(args)
    result, changes = restore_evidence(*args)
    assert args == baseline
    by_id = {n['id']:n for n in result}
    first = by_id[args[0][0]['id']]
    assert (first['start'],first['end'],first['velocity']) == (0,.3,64)
    assert first['pitch'] == 72 and first['provenance'] == 'edited'
    assert first['original_timing'] == args[0][0]['original_timing']
    restored = by_id[args[1][1]['id']]
    assert (restored['start'],restored['end'],restored['velocity']) == (.4,.6,83)
    assert restored['pitch'] == 72 and restored['pitch'] % 12 == args[1][1]['pitch'] % 12
    assert restored['provenance'] == 'edited' and restored['source_note_ids'] == args[1][1]['source_note_ids']
    assert restored['original_timing'] == args[1][1]['original_timing']
    assert {n['id'] for n in result} == {args[1][0]['id'],args[1][1]['id'],args[2][0]['id']}
    assert [c['action'] for c in changes] == ['restore_chain_timing','restore_source_reattack','add_observed_bass']
    by_id[args[2][0]['id']]['audio_review']['evidence']['independent_support_fraction'] = 0
    restored['original_timing']['pitch'] = 99
    assert args == baseline, 'Output metadata must not alias reviewed input evidence'


def test_locked_current_and_reinserted_source_events_retain_manual_choices():
    args = list(sources())
    args[0][0].update(locked=True,provenance='edited',hand='left',role='countermelody',voice_id='2')
    args[1][1].update(locked=True,provenance='edited',hand='left',voice_id='3',role='riff')
    args[2][0].update(locked=True,provenance='edited',hand='right',voice_id='4',role='countermelody')
    result,_ = restore_evidence(*args)
    by_id = {n['id']:n for n in result}
    assert by_id[args[0][0]['id']] == args[0][0]
    for source in (args[1][1],args[2][0]):
        for key,value in source.items():
            assert by_id[source['id']][key] == value


def test_only_explicit_observed_bass_candidates_are_added():
    args = list(sources())
    args[2].append(note('unselected-bass',pitch=40,start=1,end=1.5))
    result,_ = restore_evidence(*args)
    selected = [n for n in result if n.get('edition_source') == 'bass']
    assert len(selected) == 1
    assert {key:selected[0][key] for key in ('id','pitch','start','end','velocity','original_timing')} == {
        key:args[2][0][key] for key in ('id','pitch','start','end','velocity','original_timing')}


@pytest.mark.parametrize('invalid', ['unknown_candidate','unknown_chain','unknown_neighbour','duplicate_candidate','duplicate_current','invalid_uuid','evidence_pitch','evidence_time','unobserved_bass'])
def test_candidate_identity_or_stale_source_evidence_is_rejected(invalid):
    args = list(sources())
    unknown = str(uuid.uuid4())
    if invalid == 'unknown_candidate': args[3][0]['id'] = unknown
    elif invalid == 'unknown_chain': args[3][0]['chain_ids'].append(unknown)
    elif invalid == 'unknown_neighbour': args[3][0]['retained_neighbours'][0]['id'] = unknown
    elif invalid == 'duplicate_candidate': args[3].append(copy.deepcopy(args[3][0]))
    elif invalid == 'duplicate_current': args[0].append(copy.deepcopy(args[0][0]))
    elif invalid == 'invalid_uuid': args[1][0]['id'] = '../not-a-note'
    elif invalid == 'evidence_pitch': args[3][0]['pitch'] = 61
    elif invalid == 'evidence_time': args[4][0]['start'] = .25
    elif invalid == 'unobserved_bass': args[4][0]['id'] = unknown
    with pytest.raises(ValueError): restore_evidence(*args)


def test_restore_never_infers_chromatic_transposition():
    args = list(sources())
    args[0][0]['pitch'] = 61
    with pytest.raises(ValueError,match='chromatic'): restore_evidence(*args)


def register_inputs():
    source = note('register',pitch=67,start=.4,end=.9,velocity=72)
    current = {**copy.deepcopy(source),'pitch':43,'provenance':'edited','locked':False,'hand':'right',
               'audio_review':{'kind':'previous_review','nested':{'value':1}}}
    evidence = [{'id':source['id'],'current_pitch':43,'source_pitch':67,
                 'recommendation':'Explicit test fixture octave restoration','spectral':{'value':.7}}]
    return [current],[source],evidence


def test_register_restoration_changes_only_octave_and_review_provenance_without_aliasing():
    args = register_inputs()
    before = copy.deepcopy(args)
    notes, changes = restore_source_register(*args)
    assert args == before
    result = notes[0]
    assert result['pitch'] == 67 and result['pitch'] % 12 == args[0][0]['pitch'] % 12
    assert result['provenance'] == 'edited' and result['locked'] is False
    for field in ('id','start','end','velocity','hand','source_note_ids','original_timing'):
        assert result[field] == args[0][0][field]
    assert result['audio_review']['previous_review'] == args[0][0]['audio_review']
    assert changes == [{'id':args[0][0]['id'],'action':'restore_source_octave','before':43,'after':67,
                        'reason':'Explicit test fixture octave restoration'}]
    result['audio_review']['evidence']['spectral']['value'] = 0
    result['audio_review']['previous_review']['nested']['value'] = 0
    result['original_timing']['pitch'] = 99
    assert args == before


@pytest.mark.parametrize('invalid', ['missing_current','missing_source','unknown_id','duplicate_evidence','stale_current_pitch','stale_source_pitch','chromatic_pitch'])
def test_register_restoration_rejects_stale_ids_and_non_octave_evidence(invalid):
    args = list(register_inputs())
    if invalid == 'missing_current': args[0] = []
    elif invalid == 'missing_source': args[1] = []
    elif invalid == 'unknown_id': args[2][0]['id'] = str(uuid.uuid4())
    elif invalid == 'duplicate_evidence': args[2].append(copy.deepcopy(args[2][0]))
    elif invalid == 'stale_current_pitch': args[2][0]['current_pitch'] = 55
    elif invalid == 'stale_source_pitch': args[2][0]['source_pitch'] = 79
    elif invalid == 'chromatic_pitch':
        args[1][0]['pitch'] = 68
        args[2][0]['source_pitch'] = 68
    before = copy.deepcopy(args)
    with pytest.raises(ValueError):restore_source_register(*args)
    assert args == before


@pytest.mark.parametrize('manual', ['locked','edited_implicit_lock','manually_added_implicit_lock','fingering'])
def test_register_restoration_cannot_override_locks_or_manual_fingering(manual):
    args = list(register_inputs())
    if manual == 'locked': args[0][0]['locked'] = True
    elif manual == 'edited_implicit_lock': args[0][0].pop('locked')
    elif manual == 'manually_added_implicit_lock':
        args[0][0].pop('locked')
        args[0][0]['provenance'] = 'manually_added'
    elif manual == 'fingering': args[0][0]['fingering'] = 3
    before = copy.deepcopy(args)
    with pytest.raises(ValueError,match='locked/manual'):restore_source_register(*args)
    assert args == before


def midi_file(tmp_path, notes, name='fixture.mid', *, drum=False, controller=False):
    midi = pretty_midi.PrettyMIDI(initial_tempo=120,resolution=10000)
    instrument = pretty_midi.Instrument(program=0,is_drum=drum)
    instrument.notes = [pretty_midi.Note(velocity,pitch,start,end) for pitch,start,end,velocity in notes]
    if controller: instrument.control_changes = [pretty_midi.ControlChange(64,127,0)]
    midi.instruments.append(instrument)
    path = tmp_path/name
    midi.write(str(path))
    return path


def test_render_exact_origin_silence_and_linear_note_dynamics(tmp_path):
    bank = {60:(np.ones((2000,2),dtype=np.float32),1000)}
    path = midi_file(tmp_path,[(60,.1,.3,40),(60,.5,.7,80)])
    output = render(path,bank,sample_rate=1000,origin=.05,duration=1)
    assert output.shape == (1000,2)
    assert not output[:150].any() and not output[350:550].any() and not output[750:].any()
    assert output[200,0] == pytest.approx(40/127)
    assert output[600,0] == pytest.approx(80/127)
    assert output[600,0] == pytest.approx(2*output[200,0])
    np.testing.assert_array_equal(output[:,0],output[:,1])


def test_render_pitch_resampling_and_cropped_origin_keep_sample_phase(tmp_path):
    bank = {60:(np.tile(np.arange(2000,dtype=np.float32)[:,None]/2000,(1,2)),1000)}
    path = midi_file(tmp_path,[(72,.1,.8,127)])
    full = render(path,bank,sample_rate=1000,duration=1)
    assert full[200,0] == pytest.approx(.1)  # Octave up reads 200 source frames in 100 output frames.
    cropped = render(path,bank,sample_rate=1000,origin=-.2,duration=.4)
    np.testing.assert_allclose(cropped,full[200:600],atol=1e-7)
    assert cropped[0,0] > 0, 'Leading crop must not invent another attack'
    assert cropped[-1,0] > 0, 'Ending crop must not invent an early note release'


@pytest.mark.parametrize('options', [{'origin':float('nan')},{'duration':0},{'duration':-1},{'duration':float('inf')},{'duration':3602},{'sample_rate':0}])
def test_renderer_rejects_invalid_or_unbounded_clock(tmp_path,options):
    path = midi_file(tmp_path,[(60,0,.2,64)])
    with pytest.raises(ValueError):render(path,{60:(np.ones((1000,2)),1000)},**options)


@pytest.mark.parametrize('bank', [{},{60:(np.zeros(100),1000)},{60:(np.ones((2,1)),0)},{60:(np.full((2,1),float('nan')),1000)}])
def test_renderer_rejects_invalid_sample_banks(tmp_path,bank):
    path = midi_file(tmp_path,[(60,0,.2,64)])
    with pytest.raises(ValueError):render(path,bank)


@pytest.mark.parametrize('options', [{'drum':True},{'controller':True}])
def test_renderer_does_not_fabricate_unsupported_percussion_or_controller_behavior(tmp_path,options):
    path = midi_file(tmp_path,[(60,0,.2,64)],**options)
    with pytest.raises(ValueError):render(path,{60:(np.ones((1000,2)),1000)})

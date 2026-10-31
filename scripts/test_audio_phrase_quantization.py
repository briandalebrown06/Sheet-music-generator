"""Generated repeated-note fixtures; no claim of perceptual audio validation."""
from copy import deepcopy
import pytest

from scripts.audio_phrase_quantization import quantize_reviewed_phrases
from backend.app.score import build_score, write_score_midi

SETTINGS = {'tempo_bpm': 100, 'quantization': 'sixteenth', 'first_downbeat_seconds': .017627}


def note(identifier, start, end, pitch=70):
    return {'id': identifier, 'start': start, 'end': end, 'pitch': pitch,
            'velocity': 73, 'hand': 'right', 'role': 'riff', 'locked': False,
            'provenance': 'detected', 'original_timing': {'start': start, 'end': end, 'pitch': pitch}}


def test_opening_repeated_figure_exports_fourteen_distinct_attacks(tmp_path):
    import pretty_midi
    starts = [.12770975056689343, .26702947845804986, .40634920634920635,
              .5572789115646258, .6965986394557823, .8475283446712019,
              .9984580498866213, 1.1377777777777778, 1.3003174603174603,
              1.4512471655328798, 1.5905668934240362, 1.7329705215419502,
              1.8839002267573697, 2.1973696145124717]
    source = [note(f'attack-{i}', t, starts[i+1] if i+1<len(starts) else 2.3134693877551022)
              for i,t in enumerate(starts)]
    original = deepcopy(source)
    evidence = [{'chain_ids': [n['id'] for n in source[:13]]}]
    result = quantize_reviewed_phrases(source, source, evidence, SETTINGS)
    assert not result['unresolved']
    assert source == original
    assert [n['original_timing'] for n in result['notes']] == [n['original_timing'] for n in source]
    assert all(abs(c['onset_displacement_seconds']) <= .1 for c in result['changes'])
    assert result['notes'][-1] == source[-1]  # non-chain fourteenth attack untouched
    model = build_score(result['notes'], SETTINGS, title='GENERATED REPEATED-FIGURE FIXTURE')
    path = tmp_path/'fourteen.mid'
    write_score_midi(model, path)
    attacks = sorted(n.start for inst in pretty_midi.PrettyMIDI(str(path)).instruments for n in inst.notes if n.pitch==70)
    assert attacks == pytest.approx([.15*(i+1) for i in range(13)] + [2.25], abs=.0001)
    assert len(attacks) == len(set(round(t,6) for t in attacks)) == 14


def test_missing_original_chain_member_stays_an_empty_cell():
    source = [note(f'n-{i}', .14+.145*i, .14+.145*i+.14) for i in range(5)]
    unrelated = note('melody', .012, .25, 76)
    unrelated['role'] = 'melody'
    candidate = [source[i] for i in [0,2,3,4]] + [unrelated]
    result = quantize_reviewed_phrases(candidate, source, [{'chain_ids':[n['id'] for n in source]}],
                                        {**SETTINGS,'first_downbeat_seconds':0})
    assert {n['id'] for n in result['notes']} == {n['id'] for n in candidate}
    assert result['decisions'][0]['missing_ids'] == ['n-1']
    by_id = {n['id']:n for n in result['notes']}
    assert by_id['n-0']['start'] == .15 and by_id['n-0']['end'] == .30
    assert by_id['n-2']['start'] == .45  # not the next retained-note rank (.30)
    assert by_id['melody'] == unrelated
    for old in candidate:
        new = by_id[old['id']]
        assert all(new.get(k)==old.get(k) for k in ('pitch','velocity','hand','original_timing'))


def test_unacceptable_phase_fit_is_left_unchanged_and_reported():
    source = [note(f'n-{i}', .1+.1*i, .2+.1*i) for i in range(10)]
    result = quantize_reviewed_phrases(source,source,[{'chain_ids':[n['id'] for n in source]}],SETTINGS)
    assert result['notes'] == source and result['changes'] == []
    assert '100 ms' in result['unresolved'][0]['reason']


def test_manual_timing_and_budget_are_preserved():
    source = [note(f'n-{i}', .14+.145*i, .28+.145*i) for i in range(3)]
    evidence = [{'chain_ids':[n['id'] for n in source]}]
    no_budget = quantize_reviewed_phrases(source,source,evidence,SETTINGS,max_chains=0)
    assert no_budget['notes'] == source and no_budget['changes'] == []
    locked = deepcopy(source)
    locked[0]['locked'] = True
    result = quantize_reviewed_phrases(locked,source,evidence,SETTINGS)
    assert result['notes'] == locked and result['changes'] == []
    assert 'Manual timing' in result['unresolved'][0]['reason']

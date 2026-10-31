"""Development stored-event fixtures; these do not establish transcription accuracy."""
import copy
import json
import uuid
from pathlib import Path

from backend.tests.test_piano_api import piano_project, request_body, edit_note


def vocal_fixture(db, source_id):
    parent_id, melody_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.create_project(parent_id, 'DEVELOPMENT mix', 'upload', 'fixture.wav', 'audio/wav', input_mode='mixed')
    db.create_project(melody_id, 'DEVELOPMENT vocals', 'upload', 'fixture.wav', 'audio/wav', instrument_hint='voice')
    original = db.get_project(source_id)['notes'][0]
    notes = []
    for index, pitch in enumerate((72, 74, 71)):
        n = copy.deepcopy(original)
        n.update(id=str(uuid.uuid4()), pitch=pitch, start=float(index), end=index+.7, hand=None)
        n['original_timing'] = {k:n[k] for k in ('pitch','start','end','velocity')}
        notes.append(n)
    with db.connect() as c:
        c.execute('UPDATE projects SET parent_project_id=? WHERE id=?',(parent_id,source_id))
        c.execute("UPDATE projects SET parent_project_id=?,transcription_profile='vocals',status='completed',notes_json=?,notes_revision=1,note_count=3,duration_seconds=4,clip_duration_seconds=4 WHERE id=?",(parent_id,json.dumps(notes),melody_id))
    return melody_id


def test_three_distinct_editions_with_real_source_lineage(piano_project):
    client,db,source,settings=piano_project
    melody=vocal_fixture(db,source)
    before=copy.deepcopy(db.get_project(source))
    ids=[]
    for mode in ('faithful','accompaniment','solo_piano'):
        payload={**request_body(),'edition_mode':mode,'piano_settings':{'include_vocal_cue':True}}
        if mode!='faithful': payload.update(melody_project_id=melody,melody_notes_revision=1)
        result=client.post(f'/api/projects/{source}/piano-editions',json=payload)
        assert result.status_code==201,result.text
        edition=result.json();ids.append(edition['id'])
        assert edition['arrangement']['edition_mode']==mode
        assert edition['output_mode']==('transcription' if mode=='faithful' else 'accompaniment')
        assert client.post(f'/api/projects/{source}/piano-editions',json=payload).json()['id']==edition['id']
        reopened=client.get(f"/api/projects/{edition['id']}").json()
        assert reopened['notes']==edition['notes']
        assert all(n.get('source_project_id') in {source,melody} for n in edition['notes'])
        if mode=='faithful':
            assert [(n['id'],n['pitch'],n['start'],n['end'],n['velocity']) for n in edition['notes']]==[(n['id'],n['pitch'],n['start'],n['end'],n['velocity']) for n in before['notes']]
        else:
            assert len([n for n in edition['notes'] if n['edition_source']=='vocal'])==3
            assert {n['role'] for n in edition['notes'] if n['edition_source']=='vocal'}=={'vocal_cue' if mode=='accompaniment' else 'melody'}
        state=client.get(f"/api/projects/{edition['id']}/score").json()
        assert state['score']['edition_mode']==mode
        updated=client.put(f"/api/projects/{edition['id']}/score",json={'notes_revision':1,'revision':1,'settings':state['score']['settings']})
        assert updated.status_code==200,updated.text
        assert updated.json()['score']['edition_mode']==mode
    assert len(set(ids))==3
    assert {p['id'] for p in client.get(f'/api/projects/{source}/piano-editions').json()['editions']}==set(ids)
    assert db.get_project(source)==before


def test_edition_validates_melody_revision_family_and_requirement(piano_project):
    client,db,source,_=piano_project
    base=f'/api/projects/{source}/piano-editions'
    assert client.post(base,json=request_body()).status_code==422
    payload={**request_body(),'edition_mode':'solo_piano'}
    assert client.post(base,json=payload).status_code==422
    melody=vocal_fixture(db,source)
    payload.update(melody_project_id=melody,melody_notes_revision=9)
    assert client.post(base,json=payload).status_code==409
    payload['melody_notes_revision']=1
    with db.connect() as c: c.execute('UPDATE projects SET parent_project_id=NULL WHERE id=?',(melody,))
    assert client.post(base,json=payload).status_code==422


def test_musical_evidence_does_not_rewrite_source_and_estimates_stay_labelled(piano_project):
    client,db,source,_=piano_project
    before=copy.deepcopy(db.get_project(source))
    evidence=client.get(f'/api/projects/{source}/musical-analysis')
    assert evidence.status_code==200,evidence.text
    assert evidence.json()['notes_revision']==1
    assert db.get_project(source)==before
    state=db.get_score(source)
    settings={**state['score']['settings'],'tempo_bpm':100,'key_signature':-3,'estimated_fields':['tempo_bpm','key_signature'],'confirmed_fields':[]}
    result=client.post(f'/api/projects/{source}/piano-editions',json={**request_body(),'edition_mode':'faithful','settings':settings})
    assert result.status_code==201,result.text
    score=db.get_score(result.json()['id'])['score']
    assert score['tempo']['source']=='estimated'
    assert any('Key signature -3 fifths is estimated' in text for text in score['assumptions'])


def test_separate_melodic_grid_preserves_short_attacks_and_compiler_timing():
    from fractions import Fraction
    from backend.app.piano import _timeline
    from backend.app.score import ScoreSettings, build_score, fraction
    settings=ScoreSettings(tempo_bpm=100,quantization='eighth',melody_quantization='sixteenth',piano_split=True,readability_mode='accompaniment')
    notes=[{'id':str(uuid.uuid4()),'pitch':pitch,'start':start,'end':end,'role':'melody','hand':'right','part_id':'lead','velocity':80,'provenance':'detected'} for pitch,start,end in [(60,.0,.15),(62,.15,.3)]]
    score=build_score(notes,settings,title='DEVELOPMENT short lead',clip_duration_seconds=1)
    timeline,_=_timeline(notes,settings)
    assert timeline[0]['_release']==timeline[1]['_start']==.15
    sounded=[e for p in score['parts'] for s in p['staves'] for m in s['measures'] for v in m['voices'] for e in v['events'] if e['kind']!='rest']
    assert len(sounded)==2
    assert [fraction(e['offset']) for e in sounded]==[Fraction(0),Fraction(1,4)]
    assert all(fraction(e['duration'])==Fraction(1,4) for e in sounded)


def test_vocal_deletion_removes_dependent_editions_and_revoices(piano_project):
    from backend.app.project_lifecycle import cleanup_deleted_projects
    client, db, source, settings = piano_project
    melody = vocal_fixture(db, source)
    faithful_response = client.post(f'/api/projects/{source}/piano-editions', json={**request_body(), 'edition_mode': 'faithful'})
    assert faithful_response.status_code == 201, faithful_response.text
    faithful = faithful_response.json()['id']
    derived = []
    for mode in ('accompaniment', 'solo_piano'):
        response = client.post(f'/api/projects/{source}/piano-editions', json={
            **request_body(), 'edition_mode': mode, 'melody_project_id': melody,
            'melody_notes_revision': 1, 'piano_settings': {'include_vocal_cue': True}})
        assert response.status_code == 201, response.text
        identifier = response.json()['id']
        assert response.json()['melody_source_id'] == melody
        assert list((settings.data_dir / 'projects' / identifier).glob('*/melody-source-notes.json'))
        derived.append(identifier)
    revoice = client.post(f'/api/projects/{derived[-1]}/accompaniment', json=request_body())
    assert revoice.status_code == 201, revoice.text
    assert revoice.json()['melody_source_id'] == melody
    derived.append(revoice.json()['id'])
    assert client.delete(f'/api/projects/{melody}').status_code == 202
    for identifier in derived:
        assert client.get(f'/api/projects/{identifier}').status_code == 404
        assert client.get(f'/api/projects/{identifier}/score/exports/musicxml?revision=1').status_code == 404
    for _ in range(3):
        cleanup_deleted_projects(settings, db)
    assert all(not (settings.data_dir / 'projects' / identifier).exists() for identifier in derived)
    assert all(db.get_project(identifier) is None for identifier in derived)
    assert client.get(f'/api/projects/{source}').status_code == 200
    assert client.get(f'/api/projects/{faithful}').status_code == 200


def test_same_family_wrong_owner_and_inaccessible_melody_are_hidden(piano_project):
    client, db, source, settings = piano_project
    melody = vocal_fixture(db, source)
    before_projects = len(db.list_projects())
    before_directories = set((settings.data_dir / 'projects').iterdir())
    payload = {**request_body(), 'edition_mode': 'solo_piano', 'melody_notes_revision': 1}
    with db.connect() as c:
        c.execute('UPDATE projects SET owner_id=? WHERE id=?', ('another-user', melody))
    for inaccessible in (melody, str(uuid.uuid4())):
        response = client.post(f'/api/projects/{source}/piano-editions', json={**payload, 'melody_project_id': inaccessible})
        assert response.status_code == 404, response.text
        assert response.json()['detail'] == 'Project not found.'
    assert len(db.list_projects()) == before_projects  # no derived project was published
    assert client.get(f'/api/projects/{melody}').status_code == 404
    assert set((settings.data_dir / 'projects').iterdir()) == before_directories


def test_stale_melody_revision_is_fenced_at_publication(piano_project, monkeypatch):
    from backend.app import piano_editions
    client, db, source, settings = piano_project
    melody = vocal_fixture(db, source)
    before = set(p['id'] for p in db.list_projects())
    before_directories = set((settings.data_dir / 'projects').iterdir())
    original = piano_editions.prepare_edition
    def change_melody_after_snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.connect() as c:
            c.execute('UPDATE projects SET notes_revision=notes_revision+1 WHERE id=?', (melody,))
        return result
    monkeypatch.setattr(piano_editions, 'prepare_edition', change_melody_after_snapshot)
    response = client.post(f'/api/projects/{source}/piano-editions', json={
        **request_body(), 'edition_mode': 'solo_piano', 'melody_project_id': melody, 'melody_notes_revision': 1})
    assert response.status_code == 409, response.text
    assert 'vocal source changed' in response.json()['detail']
    assert set(p['id'] for p in db.list_projects()) == before
    assert set((settings.data_dir / 'projects').iterdir()) == before_directories
    assert db.get_project(source)['notes_revision'] == 1


def test_melody_clip_origin_and_duration_must_match(piano_project):
    client, db, source, _ = piano_project
    melody = vocal_fixture(db, source)
    payload = {**request_body(), 'edition_mode': 'solo_piano', 'melody_project_id': melody, 'melody_notes_revision': 1}
    for column, value in (('source_origin_seconds', 1), ('clip_duration_seconds', 5)):
        with db.connect() as c:
            c.execute('UPDATE projects SET source_origin_seconds=0,clip_duration_seconds=4 WHERE id=?', (melody,))
            c.execute(f'UPDATE projects SET {column}=? WHERE id=?', (value, melody))
        response = client.post(f'/api/projects/{source}/piano-editions', json=payload)
        assert response.status_code == 422, response.text
        assert 'matching origins and durations' in response.json()['detail']


def test_legacy_melody_dependencies_backfill_and_propagate_without_crossing_owners(piano_project):
    _, db, source, _ = piano_project
    melody = vocal_fixture(db, source)
    legacy_ids = [str(uuid.uuid4()) for _ in range(7)]
    for identifier in legacy_ids:
        db.create_project(identifier, 'DEVELOPMENT legacy edition', 'upload', 'fixture.wav', 'audio/wav')
    first, revoice, cycle, malformed, foreign, missing, nonobject = legacy_ids
    with db.connect() as c:
        c.execute('UPDATE projects SET arrangement_source_id=?,arrangement_json=? WHERE id=?',
                  (source, json.dumps({'melody_source_project_id': melody}), first))
        c.execute('UPDATE projects SET arrangement_source_id=? WHERE id=?', (first, revoice))
        c.execute('UPDATE projects SET arrangement_source_id=? WHERE id=?', (revoice, cycle))
        c.execute('UPDATE projects SET arrangement_source_id=? WHERE id=?', (cycle, first))
        c.execute('UPDATE projects SET arrangement_json=? WHERE id=?', ('{bad json', malformed))
        c.execute('UPDATE projects SET owner_id=?,arrangement_json=? WHERE id=?',
                  ('another-user', json.dumps({'melody_source_project_id': melody}), foreign))
        c.execute('UPDATE projects SET arrangement_json=? WHERE id=?', (json.dumps({'melody_source_project_id': str(uuid.uuid4())}), missing))
        c.execute('UPDATE projects SET arrangement_json=? WHERE id=?', ('[]', nonobject))
    db.initialize()
    db.initialize()  # repeatable startup, including a legacy cycle
    with db.connect() as c:
        links = {r['id']: r['melody_source_id'] for r in c.execute('SELECT id,melody_source_id FROM projects')}
    assert all(links[identifier] == melody for identifier in (first, revoice, cycle))
    assert all(links[identifier] is None for identifier in (malformed, foreign, missing, nonobject))
    assert db.request_deletion(melody, 'local')
    with db.connect() as c:
        deleted = {r['id']: bool(r['deleted_at']) for r in c.execute('SELECT id,deleted_at FROM projects')}
    assert all(deleted[identifier] for identifier in (first, revoice, cycle))
    assert not deleted[source]


def test_dependency_migration_recovers_an_already_requested_vocal_deletion(piano_project):
    _, db, source, _ = piano_project
    melody = vocal_fixture(db, source)
    edition, revoice = str(uuid.uuid4()), str(uuid.uuid4())
    for identifier in (edition, revoice):
        db.create_project(identifier, 'DEVELOPMENT legacy private copy', 'upload', 'fixture.wav', 'audio/wav')
    with db.connect() as c:
        c.execute('UPDATE projects SET arrangement_source_id=?,arrangement_json=? WHERE id=?',
                  (source, json.dumps({'melody_source_project_id': melody}), edition))
        c.execute('UPDATE projects SET arrangement_source_id=? WHERE id=?', (edition, revoice))
    # The old schema could tombstone the vocal without discovering its JSON-only
    # copies. Startup restores the edges and finishes that authorized deletion.
    assert db.request_deletion(melody, 'local')
    with db.connect() as c:
        assert c.execute('SELECT deleted_at FROM projects WHERE id=?', (edition,)).fetchone()[0] is None
    db.initialize()
    with db.connect() as c:
        for identifier in (edition, revoice):
            row = c.execute('SELECT melody_source_id,deleted_at,status FROM projects WHERE id=?', (identifier,)).fetchone()
            assert row['melody_source_id'] == melody
            assert row['deleted_at'] is not None and row['status'] == 'canceled'
            assert not c.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','processing')", (identifier,)).fetchone()
    assert db.get_project(source) is not None


def test_explicit_solo_octave_is_separate_audited_and_preserves_raw(piano_project):
    client,db,source,_=piano_project
    melody=vocal_fixture(db,source)
    raw=copy.deepcopy(db.get_project(melody)['notes'])
    payload={**request_body(),'edition_mode':'solo_piano','melody_project_id':melody,'melody_notes_revision':1}
    original=client.post(f'/api/projects/{source}/piano-editions',json=payload).json()
    response=client.post(f'/api/projects/{source}/piano-editions',json={**payload,'melody_octave_shift':12})
    assert response.status_code==201,response.text
    upper=response.json()
    assert upper['id']!=original['id']
    assert upper['arrangement']['melody_octave_shift']==12
    assert 'Melody +1 octave' in upper['title']
    original_by_id={n['id']:n for n in raw}
    for n in upper['notes']:
        if n.get('edition_source')=='vocal':
            before=original_by_id[n['source_note_id']]
            assert n['pitch']==before['pitch']+12
            assert n['original_timing']==before['original_timing']
    assert db.get_project(melody)['notes']==raw
    assert client.post(f'/api/projects/{source}/piano-editions',json={**request_body(),'edition_mode':'faithful','melody_octave_shift':12}).status_code==422


def test_lighter_saved_solo_preserves_lead_lineage_and_density_settings(piano_project):
    client,db,source,_=piano_project
    melody=vocal_fixture(db,source)
    original=client.post(f'/api/projects/{source}/piano-editions',json={
        **request_body(),'edition_mode':'solo_piano','melody_project_id':melody,
        'melody_notes_revision':1,'melody_octave_shift':12,
        'piano_settings':{'target_keys_per_hand':4},
    })
    assert original.status_code==201,original.text
    before=original.json(); base=f"/api/projects/{before['id']}"
    before_score=copy.deepcopy(db.get_score(before['id']))
    payload={'notes_revision':1,'score_revision':1,'piano_settings':{'target_keys_per_hand':2}}
    response=client.post(base+'/accompaniment',json=payload)
    assert response.status_code==201,response.text
    after=response.json()
    assert after['id']!=before['id']
    assert after['arrangement']['edition_mode']=='solo_piano'
    assert after['arrangement']['settings']['target_keys_per_hand']==2
    assert after['arrangement']['density_before'] is not None
    assert after['arrangement']['density_after']['target_keys_per_hand']==2
    assert after['arrangement']['melody_octave_shift']==12
    assert after['melody_source_id']==melody
    assert after['arrangement']['readability_changes']==[]
    protected=lambda notes:{n['id']:tuple(n.get(k) for k in ('pitch','start','end','original_timing','source_note_id','source_project_id'))
                            for n in notes if n.get('edition_source')=='vocal'}
    assert protected(after['notes'])==protected(before['notes'])
    assert client.get(base).json()==before
    assert db.get_score(before['id'])==before_score
    assert client.post(base+'/accompaniment',json=payload).json()['id']==after['id']
    saved=client.get(f"/api/projects/{after['id']}/score").json()
    assert 'melody +1 octave' in saved['score']['edition_label']
    check=client.get(f"/api/projects/{after['id']}/playability").json()
    assert check['report']['density']['target_keys_per_hand']==2
    assert client.get(f"/api/projects/{after['id']}").json()['notes']==after['notes']


def test_density_target_has_bounded_inputs(piano_project):
    client,_,source,_=piano_project
    for target in (0,6):
        response=client.post(f'/api/projects/{source}/accompaniment',json={
            **request_body(),'piano_settings':{'target_keys_per_hand':target}})
        assert response.status_code==422

"""Stored-note DEVELOPMENT fixtures verify derivation, not transcription accuracy."""
import copy
import io
import json
import uuid
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.performance import write_performance_midi
from backend.app.piano import ENGINE_VERSION
from backend.app.score import ScoreSettings, build_score, score_to_musicxml


@pytest.fixture
def piano_project(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    db = app.state.db
    identifier = str(uuid.uuid4())
    db.create_project(identifier, 'DEVELOPMENT oversized piano fixture', 'upload', 'fixture.wav', 'audio/wav', instrument_hint='piano', tempo_hint=120)
    job = db.claim_job('test-fixture')
    notes = []
    for start, pitches in [(0, [48, 60, 64, 67, 72, 76]), (1, [50, 62, 65, 69, 74, 77]), (2, [43, 55, 59, 62, 67, 71])]:
        for pitch in pitches:
            nid = str(uuid.uuid4())
            notes.append({'id': nid, 'pitch': pitch, 'start': start, 'end': start+.75, 'velocity': 80,
                'part_id': 'part-1', 'hand': 'left', 'provenance': 'detected', 'model_score': None,
                'source_note_ids': [nid], 'original_timing': {'pitch': pitch,'start':start,'end':start+.75,'velocity':80}, 'score_timing':None})
    directory = tmp_path/'projects'/identifier
    output = directory/job['id']
    output.mkdir(parents=True)
    with wave.open(str(directory/'source'),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(22050)
        stream.writeframes(b'\0\0'*(22050*4))
    # The API fixture does not claim these stored notes came from silence.
    write_performance_midi(notes,output/'raw-performance.mid')
    (output/'raw-note-events.json').write_text(json.dumps({'note_events': [[n['start'],n['end'],n['pitch'],.7,[]] for n in notes]}))
    db.record_decoded(job,'test-fixture',4,0,4)
    db.finish(job,'test-fixture',notes=notes,duration=4,exports=['raw-midi','midi'])
    score = build_score(notes,ScoreSettings(tempo_bpm=120,piano_split=True),title='DEVELOPMENT fixture',clip_duration_seconds=4,source_notes_revision=1)
    db.save_score(identifier,1,0,score,score_to_musicxml(score))
    with TestClient(app) as client:
        yield client,db,identifier,settings


def request_body():
    return {'notes_revision':1,'score_revision':1,'piano_settings':{'left_reach':9,'right_reach':9}}


def edit_note(note,**updates):
    keys=('id','pitch','start','end','velocity','part_id','voice_id','hand','locked','role','pedal_sustained','key_release_seconds','fingering')
    return {**{key:note[key] for key in keys if key in note},**updates}


def test_arrangement_is_separate_idempotent_and_reopens(piano_project):
    client,db,identifier,settings=piano_project
    original=copy.deepcopy(db.get_project(identifier));original_score=copy.deepcopy(db.get_score(identifier))
    before=client.get(f'/api/projects/{identifier}/playability')
    assert before.status_code==200,before.text
    assert before.json()['report']['issues']
    response=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body())
    assert response.status_code==201,response.text
    derived=response.json()
    assert derived['id']!=identifier and derived['output_mode']=='accompaniment'
    assert derived['arrangement_source_id']==identifier
    assert derived['arrangement']['changes']
    assert derived['arrangement']['engine']==ENGINE_VERSION
    assert derived['arrangement']['generation']==1
    assert derived['arrangement']['readability_version']==1
    assert db.get_project(identifier)==original and db.get_score(identifier)==original_score
    assert client.post(f'/api/projects/{identifier}/accompaniment',json=request_body()).json()['id']==derived['id']
    assert len(db.list_projects())==2
    assert all(n.get('hand') in {'left','right'} for n in derived['notes'])
    source=(settings.data_dir/'projects'/identifier/'source').read_bytes()
    assert (settings.data_dir/'projects'/derived['id']/'source').read_bytes()==source
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(f"/api/projects/{derived['id']}").json()['notes']==derived['notes']
        assert reopened.get(f"/api/projects/{derived['id']}/score").json()['score']==db.get_score(derived['id'])['score']
    assert client.post(f'/api/projects/{identifier}/accompaniment',json={**request_body(),'notes_revision':0}).status_code==409


def test_exports_transpose_checks_and_manual_locks(piano_project):
    import pretty_midi
    from music21 import converter
    client,db,identifier,_=piano_project
    derived=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body()).json()
    base=f"/api/projects/{derived['id']}"
    state=client.get(base+'/score').json()
    before_pitches=[n['pitch'] for n in derived['notes']]
    musical={**state['score']['settings'],'transpose_semitones':12}
    saved=client.put(base+'/score',json={'notes_revision':1,'revision':1,'settings':musical})
    assert saved.status_code==200,saved.text
    assert [n['pitch'] for n in db.get_project(derived['id'])['notes']]==before_pitches
    score=saved.json()['score']
    assert 'playability' in score
    assert client.get(base+'/playability').json()['score_revision']==2
    assert client.get(base+'/score/exports/midi?revision=1').status_code==409
    midi=pretty_midi.PrettyMIDI(io.BytesIO(client.get(base+'/score/exports/midi?revision=2').content))
    xml=converter.parseData(client.get(base+'/score/exports/musicxml?revision=2').text)
    assert {n.pitch for i in midi.instruments for n in i.notes}=={p.midi for n in xml.recurse().notes for p in n.pitches}
    assert set(before_pitches)=={n.pitch-12 for i in midi.instruments for n in i.notes}
    note=derived['notes'][0]
    updates=[edit_note(n,locked=True,fingering=1) if n['id']==note['id'] else edit_note(n) for n in derived['notes']]
    review=client.put(base+'/review',json={'notes_revision':1,'score_revision':2,'settings':musical,'notes':updates})
    assert review.status_code==200,review.text
    reopened=client.get(base).json()
    locked=next(n for n in reopened['notes'] if n['id']==note['id'])
    assert locked['locked'] and locked['fingering']==1
    assert 'playability' in review.json()['score_state']['score']
    again=client.post(base+'/accompaniment',json={'notes_revision':2,'score_revision':3,'piano_settings':{'left_reach':7,'right_reach':7}})
    assert again.status_code==201,again.text
    assert again.json()['arrangement']['generation']==2
    assert again.json()['title'].endswith(' · Revoice 2')
    kept=next(n for n in again.json()['notes'] if n['id']==note['id'])
    assert (kept['pitch'],kept['start'],kept['end'],kept['hand'],kept['fingering'])==(locked['pitch'],locked['start'],locked['end'],locked['hand'],1)


def test_pedal_metadata_and_source_deletion(piano_project):
    from backend.app.project_lifecycle import cleanup_deleted_projects
    client,db,identifier,settings=piano_project
    original=db.get_project(identifier)
    note=original['notes'][0]
    edits=[edit_note(n,pedal_sustained=True,key_release_seconds=None) if n['id']==note['id'] else edit_note(n) for n in original['notes']]
    assert client.put(f'/api/projects/{identifier}/notes',json={'revision':1,'notes':edits}).status_code==422
    derived=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body()).json()
    assert client.delete(f'/api/projects/{identifier}').status_code==202
    assert client.get(f"/api/projects/{derived['id']}").status_code==404
    cleanup_deleted_projects(settings,db)
    cleanup_deleted_projects(settings,db)
    assert not (settings.data_dir/'projects'/identifier).exists()
    assert not (settings.data_dir/'projects'/derived['id']).exists()
    assert db.list_projects()==[]


def test_arrangement_owner_csrf_and_descendant_urls(piano_project):
    from dataclasses import replace
    from backend.tests.test_auth_security import ORIGIN, PASSWORD
    _,db,identifier,settings=piano_project
    secured=replace(settings,app_mode='hosted',public_origin=ORIGIN)
    app=create_app(secured)
    app.state.auth_store.create_user('alice',PASSWORD)
    app.state.auth_store.create_user('bob',PASSWORD+' second')
    with TestClient(app,base_url=ORIGIN) as alice, TestClient(app,base_url=ORIGIN) as bob:
        def signin(client,user,password):
            r=client.post('/api/auth/login',json={'username':user,'password':password},headers={'Origin':ORIGIN})
            assert r.status_code==200,r.text
            return r.json(),{'Origin':ORIGIN,'X-CSRF-Token':r.json()['csrf_token']}
        a,ah=signin(alice,'alice',PASSWORD);_,bh=signin(bob,'bob',PASSWORD+' second')
        with db.connect() as c:
            c.execute('UPDATE projects SET owner_id=? WHERE id=?',(a['user']['id'],identifier))
        base=f'/api/projects/{identifier}'
        assert bob.get(base+'/playability').status_code==404
        assert bob.post(base+'/accompaniment',json=request_body(),headers=bh).status_code==404
        assert alice.post(base+'/accompaniment',json=request_body(),headers={'Origin':ORIGIN}).status_code==403
        response=alice.post(base+'/accompaniment',json=request_body(),headers=ah)
        assert response.status_code==201,response.text
        child=response.json()
        assert child['owner_id']==a['user']['id']
        for suffix in ['', '/audio', '/score', '/playability', '/score/exports/midi', '/score/exports/pdf']:
            assert bob.get(f"/api/projects/{child['id']}"+suffix).status_code==404


def test_vocal_only_source_does_not_invent_piano_accompaniment(piano_project):
    client,db,identifier,_=piano_project
    with db.connect() as c:
        c.execute("UPDATE projects SET transcription_profile='vocals',instrument_hint='voice' WHERE id=?",(identifier,))
    response=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body())
    assert response.status_code==422
    assert 'vocal melody' in response.json()['detail']
    assert len(db.list_projects())==1


def test_derivation_quota_retains_source_and_leaves_no_private_copy(piano_project):
    from dataclasses import replace
    from backend.tests.test_auth_security import ORIGIN, PASSWORD
    _,db,identifier,settings=piano_project
    app=create_app(replace(settings,app_mode='hosted',public_origin=ORIGIN,max_projects_per_user=1))
    app.state.auth_store.create_user('quota-user',PASSWORD)
    with TestClient(app,base_url=ORIGIN) as client:
        login=client.post('/api/auth/login',json={'username':'quota-user','password':PASSWORD},headers={'Origin':ORIGIN}).json()
        with db.connect() as connection:
            connection.execute('UPDATE projects SET owner_id=? WHERE id=?',(login['user']['id'],identifier))
        original=copy.deepcopy(db.get_project(identifier))
        response=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body(),
                            headers={'Origin':ORIGIN,'X-CSRF-Token':login['csrf_token']})
    assert response.status_code==429,response.text[:300]
    assert db.get_project(identifier)==original
    assert {path.name for path in (settings.data_dir/'projects').iterdir()}=={identifier}


def test_movement_revoice_preserves_saved_performance_timing(piano_project):
    client,db,identifier,_=piano_project
    derived=client.post(f'/api/projects/{identifier}/accompaniment',json=request_body()).json()
    base=f"/api/projects/{derived['id']}"
    state=client.get(base+'/score').json()
    # An intentional small performance offset must not be snapped by a second
    # readability pass when the user requests only a movement refinement.
    first=derived['notes'][0]['id']
    notes=[edit_note(n,start=.017,locked=False) if n['id']==first else edit_note(n) for n in derived['notes']]
    saved=client.put(base+'/review',json={'notes_revision':1,'score_revision':1,'settings':state['score']['settings'],'notes':notes})
    assert saved.status_code==200,saved.text[:300]
    before=copy.deepcopy(db.get_project(derived['id']))
    result=client.post(base+'/accompaniment',json={'notes_revision':2,'score_revision':2,'piano_settings':{}})
    assert result.status_code==201,result.text[:300]
    after=result.json()
    assert {n['id']:(n['start'],n['end']) for n in after['notes']}=={n['id']:(n['start'],n['end']) for n in before['notes']}
    assert after['arrangement']['readability_changes']==[]
    assert after['arrangement']['refinement'] is not None
    assert db.get_project(derived['id'])==before

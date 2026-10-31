"""Ownership, durable deletion, real upload idempotency and quota regressions."""
from __future__ import annotations

import io
import json
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.database import Database, IdempotencyConflict, QuotaError
from backend.app.main import create_app
from backend.app.project_lifecycle import artifact_lease, cleanup_deleted_projects

ORIGIN = 'https://score.example.test'
PASSWORD = 'Explicit fixture password 72441'


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as value:
        value.setnchannels(1); value.setsampwidth(2); value.setframerate(22050)
        value.writeframes(b'\x00\x01' * 22050)
    return stream.getvalue()


@pytest.fixture
def environment(tmp_path):
    settings = Settings(data_dir=tmp_path/'data', app_mode='hosted', public_origin=ORIGIN)
    app = create_app(settings)
    app.state.auth_store.create_user('alice', PASSWORD)
    app.state.auth_store.create_user('bob', PASSWORD+' second')
    with TestClient(app, base_url=ORIGIN) as alice, TestClient(app, base_url=ORIGIN) as bob:
        headers=[]
        for client, name, password in ((alice,'alice',PASSWORD),(bob,'bob',PASSWORD+' second')):
            state=client.post('/api/auth/login', json={'username':name,'password':password},headers={'Origin':ORIGIN}).json()
            headers.append({'Origin':ORIGIN,'X-CSRF-Token':state['csrf_token']})
        yield settings, app.state.db, alice, bob, headers[0], headers[1]


def upload(client, headers, key='submission-fixture-1', **fields):
    return client.post('/api/projects',headers={**headers,'Idempotency-Key':key},
        data={'title':'EXPLICIT DEVELOPMENT FIXTURE','clip_end_seconds':'1',**fields},
        files={'file':('fixture.wav',wav_bytes(),'audio/wav')})


def test_every_resource_checks_owner_and_legacy_is_not_claimed(environment):
    settings, db, alice, bob, ah, bh = environment
    project=upload(alice,ah).json(); identifier=project['id']
    assert project['owner_id']==alice.get('/api/auth/session').json()['user']['id']
    assert alice.get(f'/api/projects/{identifier}/audio').status_code==200
    assert bob.get('/api/projects').json()==[]
    private=['','/audio','/analysis-audio','/waveform','/playback/raw','/score','/score/musicxml',
             '/score/exports/pdf','/score/exports/midi','/score/exports/json','/exports/midi',
             '/artifacts/raw-model-output','/stems','/stems/vocals/audio','/separation-manifest']
    for suffix in private:
        response=bob.get(f'/api/projects/{identifier}{suffix}')
        assert response.status_code==404,(suffix,response.text)
        assert response.headers['cache-control']=='no-store'
    for action in ('cancel','retry','stems/vocals/transcribe'):
        assert bob.post(f'/api/projects/{identifier}/{action}',headers=bh).status_code==404
    assert bob.delete(f'/api/projects/{identifier}',headers=bh).status_code==404
    unknown=str(uuid.uuid4())
    assert bob.get(f'/api/projects/{unknown}').status_code==404
    assert not (settings.data_dir/'.locks'/f'{unknown}.lock').exists()
    legacy=str(uuid.uuid4());db.create_project(legacy,'LEGACY FIXTURE','upload','x.wav','audio/wav')
    assert alice.get(f'/api/projects/{legacy}').status_code==404
    assert len(alice.get('/api/projects').json())==1


def test_upload_idempotency_is_persistent_owner_scoped_and_content_bound(environment):
    settings, db, alice, bob, ah, bh = environment
    first=upload(alice,ah);assert first.status_code==201
    replay=upload(alice,ah);assert replay.status_code==201
    assert first.json()['id']==replay.json()['id']
    assert len(list((settings.data_dir/'projects').iterdir()))==1
    changed=upload(alice,ah,title='DIFFERENT FIXTURE SETTINGS')
    assert changed.status_code==409
    other=upload(bob,bh);assert other.status_code==201 and other.json()['id']!=first.json()['id']
    reopened=Database(settings.database_path,settings=settings)
    with reopened.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM submission_keys').fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==2


def test_deletion_hides_all_routes_then_cleans_after_inflight_lease(environment):
    settings, db, alice, bob, ah, bh=environment
    project=upload(alice,ah).json();identifier=project['id'];directory=settings.data_dir/'projects'/identifier
    with artifact_lease(settings,identifier):
        response=alice.delete(f'/api/projects/{identifier}',headers=ah)
        assert response.status_code==202 and response.json()['status']=='deleting'
        assert alice.get(f'/api/projects/{identifier}/audio').status_code==404
        assert alice.get('/api/projects').json()==[]
        cleanup_deleted_projects(settings,db)
        assert directory.exists(), 'An in-flight response or process still owns the file lease'
    reopened=Database(settings.database_path,settings=settings)
    cleanup_deleted_projects(settings,reopened)
    assert not directory.exists() and reopened.get_project(identifier) is None
    with reopened.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        assert conn.execute('SELECT fingerprint FROM submission_keys').fetchone()[0]==''
        assert conn.execute('SELECT COUNT(*) FROM processing_usage').fetchone()[0]==1
    assert upload(alice,ah).status_code==409, 'Deleted upload key cannot resurrect its project'


def raw_project(db, owner='owner', **kwargs):
    identifier=str(uuid.uuid4())
    return db.create_project(identifier,'EXPLICIT TEST FIXTURE','upload','fixture.wav','audio/wav',owner_id=owner,**kwargs)


def test_atomic_concurrent_submission_keys_and_quotas(tmp_path):
    settings=Settings(data_dir=tmp_path,auth_enabled=True,max_active_jobs_per_user=1,max_jobs_per_day=2)
    db=Database(settings.database_path,settings=settings);db.initialize()
    def create(_):
        return raw_project(db,idempotency_key='same-key',fingerprint='same-fixture-content')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(create,range(2)))
    assert results[0]['id']==results[1]['id']
    with pytest.raises(QuotaError,match='Concurrent'):
        raw_project(db)
    db.cancel(results[0]['id']);second=raw_project(db)
    db.cancel(second['id']);db.request_deletion(second['id'],'owner');db.purge_deleted_project(second['id'])
    with pytest.raises(QuotaError,match='Daily'):
        raw_project(db)


def test_storage_reservation_and_project_count_limits(tmp_path):
    settings=Settings(data_dir=tmp_path,auth_enabled=True,max_projects_per_user=1,max_storage_mb_per_user=16)
    db=Database(settings.database_path,settings=settings);db.initialize()
    with pytest.raises(QuotaError,match='Storage'):
        raw_project(db,reserved_bytes=32*1024*1024)
    first=raw_project(db,reserved_bytes=1)
    with pytest.raises(QuotaError,match='Project quota'):
        raw_project(db)
    db.request_deletion(first['id'],'owner')
    with pytest.raises(QuotaError,match='Project quota'):
        raw_project(db)
    db.purge_deleted_project(first['id'])
    assert raw_project(db)['status']=='queued'


def test_restart_recovery_does_not_bypass_daily_quota(tmp_path):
    settings=Settings(data_dir=tmp_path,auth_enabled=True,max_jobs_per_day=1)
    db=Database(settings.database_path,settings=settings);db.initialize()
    project=raw_project(db);job=db.claim_job('dead-worker',lease_seconds=-1)
    assert db.claim_job('replacement') is None
    state=db.get_project(project['id'])
    assert state['status']=='failed' and len(state['jobs'])==1
    assert 'Daily processing quota' in state['jobs'][0]['error']
    assert not db.finish(job,'dead-worker',notes=[])


def test_deleting_parent_cascades_children_and_fences_active_attempt(tmp_path):
    settings=Settings(data_dir=tmp_path);settings.prepare()
    db=Database(settings.database_path);db.initialize()
    parent=raw_project(db,input_mode='mixed');job=db.claim_job('fixture')
    assert db.finish(job,'fixture',separation_bundle={'stems':[{'category':'bass','duration_seconds':1}], 'manifest':{'explicit_test_fixture':True}})
    child_id=str(uuid.uuid4())
    child,_=db.create_stem_child(parent['id'],'bass',child_id,lambda *args:None)
    active=db.claim_job('child-worker')
    for identifier in (parent['id'],child_id):
        directory=tmp_path/'projects'/identifier;directory.mkdir();(directory/'source').write_text('explicit fixture bytes')
    assert child['owner_id']==parent['owner_id']
    assert db.request_deletion(parent['id'],'owner')
    assert not db.owns_job(active['id'],'child-worker')
    assert not db.finish(active,'child-worker',notes=[])
    cleanup_deleted_projects(settings,db)
    assert not list((tmp_path/'projects').iterdir())
    assert db.pending_deletions()==[]


def test_actual_artifact_storage_limit_includes_saved_edits(tmp_path):
    settings=Settings(data_dir=tmp_path,auth_enabled=True,max_storage_mb_per_user=16)
    settings.prepare();db=Database(settings.database_path,settings=settings);db.initialize()
    project=raw_project(db);directory=tmp_path/'projects'/project['id']/'edits';directory.mkdir(parents=True)
    with (directory/'explicit-test-bytes').open('wb') as fixture:
        fixture.truncate(16*1024*1024+1)
    with pytest.raises(QuotaError,match='Private artifact storage'):
        db.check_storage_usage('owner')

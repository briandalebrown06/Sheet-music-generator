"""Real password/session security with disposable accounts; no production secrets."""
from __future__ import annotations

import errno
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.auth import current_user_id, install_auth_routes
from backend.app.auth_store import AuthStore, hash_password, token_digest, verify_password
from backend.app.config import Settings
from backend.app.security import LocalOnlyMiddleware
from backend.app import security

ORIGIN = "https://score.example.test"
PASSWORD = "Explicit test fixture password 72441"


def auth_app(settings):
    settings.prepare()
    app = FastAPI()
    install_auth_routes(app, settings, settings.database_path)
    app.add_middleware(LocalOnlyMiddleware, settings=settings)
    @app.get("/api/projects/{path:path}")
    def private(path):
        return {"actor":current_user_id()}
    @app.post("/api/private")
    @app.delete("/api/private")
    def mutate():
        return {"actor":current_user_id()}
    @app.get("/api/health")
    def health():
        return {"status":"ok"}
    return app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data",app_mode="hosted",public_origin=ORIGIN)


@pytest.fixture
def app(settings):
    app = auth_app(settings)
    app.state.auth_store.create_user("alice",PASSWORD)
    app.state.auth_store.create_user("bob",PASSWORD + " second")
    return app


def login(client, username="alice", password=PASSWORD):
    response = client.post("/api/auth/login",json={"username":username,"password":password},headers={"Origin":ORIGIN})
    assert response.status_code == 200, response.text
    return response


def test_password_hash_has_unique_salt_and_actual_scrypt():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$131072$8$1$")
    assert PASSWORD not in first
    assert verify_password(PASSWORD,first)
    assert not verify_password("wrong",first)
    assert not verify_password(PASSWORD,first.replace("131072","2"))
    with pytest.raises(ValueError):
        hash_password("short")


@pytest.mark.parametrize("suffix", ["abc", "abc/audio", "abc/stems/vocals/audio", "abc/exports/midi", "abc/score/exports/pdf", "abc/artifacts/raw-model-output"])
def test_all_private_url_categories_require_session(settings,suffix):
    with TestClient(auth_app(settings),base_url=ORIGIN) as client:
        response=client.get("/api/projects/"+suffix)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/auth/session").json() == {"authentication_required":True,"user":None,"csrf_token":None}


def test_session_cookie_csrf_rotation_revoke_and_two_actors(app,settings):
    with TestClient(app,base_url=ORIGIN) as alice, TestClient(app,base_url=ORIGIN) as bob:
        first=login(alice)
        assert all(flag in first.headers["set-cookie"] for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "__Host-scorecraft_session="))
        token=alice.cookies.get(settings.session_cookie_name)
        state=first.json()
        assert state["user"]["username"] == "alice" and state["csrf_token"]
        assert alice.get("/api/auth/session").json()==state
        assert alice.get("/api/projects/example/audio").json()["actor"]==state["user"]["id"]
        with app.state.auth_store.connection() as conn:
            stored=conn.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
            assert token not in stored and stored==token_digest(token)
        other=login(bob,"bob",PASSWORD+" second").json()
        assert other["user"]["id"] != state["user"]["id"]
        headers={"Origin":ORIGIN,"X-CSRF-Token":state["csrf_token"]}
        assert alice.post("/api/private",headers=headers).status_code==200
        assert alice.delete("/api/private",headers=headers).status_code==200
        assert alice.post("/api/private",headers={"Origin":ORIGIN}).status_code==403
        assert alice.post("/api/private",headers=[(b"Origin",ORIGIN.encode()),(b"X-CSRF-Token",b"\xff")]).status_code==403
        assert alice.post("/api/private",headers={"X-CSRF-Token":state["csrf_token"]}).status_code==403
        assert bob.post("/api/private",headers=headers).status_code==403
        assert alice.post("/api/private",headers={**headers,"Origin":"https://evil.example"}).status_code==403
        new=login(alice).json()
        assert new["csrf_token"] != state["csrf_token"]
        assert app.state.auth_store.session(token) is None
        assert alice.post("/api/auth/logout",headers={"Origin":ORIGIN,"X-CSRF-Token":new["csrf_token"]}).status_code==204
        assert alice.get("/api/projects/example").status_code==401
        assert bob.get("/api/projects/example").status_code==200


def test_expiry_disabled_account_and_password_reset_revoke(app,settings):
    with TestClient(app,base_url=ORIGIN) as client:
        login(client)
        with app.state.auth_store.connection() as conn:
            conn.execute("UPDATE auth_sessions SET expires_at=?",(time.time()-1,))
        assert client.get("/api/projects/private").status_code==401
        login(client)
        app.state.auth_store.set_password("alice",PASSWORD+" replacement")
        assert client.get("/api/projects/private").status_code==401
        assert client.post("/api/auth/login",json={"username":"alice","password":PASSWORD},headers={"Origin":ORIGIN}).status_code==401
        login(client,password=PASSWORD+" replacement")
        app.state.auth_store.disable_user("alice")
        assert client.get("/api/projects/private").status_code==401


def test_login_origin_content_limits_generic_errors_and_no_secret_echo(app):
    with TestClient(app,base_url=ORIGIN) as client:
        body={"username":"alice","password":PASSWORD}
        assert client.post("/api/auth/login",json=body).status_code==403
        assert client.post("/api/auth/login",json=body,headers={"Origin":"https://evil.example"}).status_code==403
        assert client.post("/api/auth/login",content="{}",headers={"Origin":ORIGIN,"Content-Type":"text/plain"}).status_code==415
        assert client.post("/api/auth/login",content=b"x"*5000,headers={"Origin":ORIGIN,"Content-Type":"application/json"}).status_code==413
        wrong=client.post("/api/auth/login",json={"username":"alice","password":"wrong"},headers={"Origin":ORIGIN})
        absent=client.post("/api/auth/login",json={"username":"absent","password":"wrong"},headers={"Origin":ORIGIN})
        assert wrong.status_code==absent.status_code==401 and wrong.json()==absent.json()
        secret="secret-test-value-"*30
        malformed=client.post("/api/auth/login",json={"username":"alice","password":secret},headers={"Origin":ORIGIN})
        assert malformed.status_code==422 and secret not in malformed.text
        assert client.get("/api/health",headers={"Host":"evil.example"}).status_code==403
        assert client.get("/api/health",headers={"Host":"score.example.test/path"}).status_code==400


def test_persistent_throttle_is_atomic_and_does_not_store_raw_identity(settings):
    settings.prepare();store=AuthStore(settings.database_path);store.initialize()
    identity="explicit-fixture-private-address"
    with ThreadPoolExecutor(max_workers=6) as pool:
        values=list(pool.map(lambda _:AuthStore(settings.database_path).rate_allowed(identity,3)[0],range(8)))
    assert sum(values)==3
    assert not AuthStore(settings.database_path).rate_allowed(identity,3)[0]
    with store.connection() as conn:
        assert identity not in str([tuple(row) for row in conn.execute("SELECT * FROM auth_rate_buckets")])


def test_http_login_and_write_rate_limits(settings):
    settings=replace(settings,login_rate_limit_per_minute=1,write_rate_limit_per_minute=1)
    app=auth_app(settings);app.state.auth_store.create_user("alice",PASSWORD)
    with TestClient(app,base_url=ORIGIN) as client:
        state=login(client).json()
        limited=client.post("/api/auth/login",json={"username":"bob","password":"wrong"},headers={"Origin":ORIGIN})
        assert limited.status_code==429 and int(limited.headers["retry-after"])>0
        headers={"Origin":ORIGIN,"X-CSRF-Token":state["csrf_token"]}
        assert client.post("/api/private",headers=headers).status_code==200
        assert client.post("/api/private",headers=headers).status_code==429


def test_local_anonymous_actor_stays_local_and_hosted_config_fails_closed(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path/"data")
    with TestClient(auth_app(settings)) as client:
        assert client.get("/api/projects/private").json()["actor"]=="local"
        assert client.get("/api/auth/session").json()["authentication_required"] is False
    for origin in (None,"http://example.com","https://example.com/path","https://u:p@example.com","https://example.com/","https://example.com?x=1"):
        with pytest.raises(ValueError):
            replace(settings,app_mode="hosted",public_origin=origin).prepare()
    monkeypatch.setenv("APP_MODE","hosted");monkeypatch.setenv("AUTH_ENABLED","false")
    monkeypatch.setenv("PUBLIC_ORIGIN",ORIGIN)
    with pytest.raises(ValueError,match="cannot disable"):
        Settings.from_env()


@pytest.mark.parametrize("storage_errno", [errno.ENOSPC, errno.EDQUOT])
def test_request_spool_storage_failure_is_private_and_closes(tmp_path,monkeypatch,storage_errno):
    spools=[]
    class FullSpool(security.SpooledTemporaryFile):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            spools.append(self)
        def rollover(self):
            raise OSError(storage_errno,"private fixture storage path /tmp/private-recording")
    monkeypatch.setattr(security,"SpooledTemporaryFile",FullSpool)
    app=auth_app(Settings(data_dir=tmp_path/"data"))
    with TestClient(app) as client:
        response=client.post("/api/private",content=b"a"*(1024*1024+1))
    assert response.status_code==507
    assert response.json()=={"detail":"Temporary upload storage is full. Try again later."}
    assert "/tmp/" not in response.text and "actor" not in response.text
    assert len(spools)==1 and spools[0].closed


def test_spool_handler_does_not_swallow_downstream_storage_errors(tmp_path):
    app=auth_app(Settings(data_dir=tmp_path/"data"))
    @app.post("/api/downstream-error")
    def downstream_error():
        raise OSError(errno.ENOSPC,"downstream fixture error")
    with TestClient(app,raise_server_exceptions=False) as client:
        assert client.post("/api/downstream-error",content=b"small").status_code==500

"""Session routes and request actor, shared by all project authorization paths."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth_store import AuthStore, token_digest
from .config import Settings

_actor: ContextVar[str | None] = ContextVar("scorecraft_actor", default=None)
_session: ContextVar[dict | None] = ContextVar("scorecraft_session", default=None)
_login_capacity = BoundedSemaphore(2)


def current_user_id() -> str:
    actor = _actor.get()
    if actor is None:
        raise HTTPException(401, "Sign in to access this workspace.")
    return actor


def current_session() -> dict | None:
    return _session.get()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256, repr=False)


def install_auth_routes(app: FastAPI, settings: Settings, database_path: Path) -> None:
    store = AuthStore(database_path)
    store.initialize()
    app.state.auth_store = store

    @app.exception_handler(RequestValidationError)
    async def private_validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's usual error detail includes rejected input values. Avoid
        # echoing passwords or private note/audio payloads into error collectors.
        errors = [{key:value for key,value in error.items() if key in {"loc", "msg", "type"}}
                  for error in exc.errors()]
        return JSONResponse({"detail":errors}, status_code=422)

    def payload(session: dict | None):
        return {"authentication_required": settings.authentication_required,
            "user": session["user"] if session else None,
            "csrf_token": session["csrf_token"] if session else None}

    @app.get("/api/auth/session")
    def session_info():
        return payload(current_session())

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response):
        if not settings.authentication_required:
            raise HTTPException(409, "Sign-in is disabled in the local anonymous workspace.")
        # Account bucket complements the network/client bucket in middleware.
        allowed, retry = store.rate_allowed("login-account:" + token_digest(body.username.strip().casefold()), 5, 900)
        if not allowed:
            raise HTTPException(429, "Too many sign-in attempts. Try again later.", headers={"Retry-After":str(retry)})
        if not _login_capacity.acquire(blocking=False):
            raise HTTPException(429, "Sign-in is busy. Try again shortly.", headers={"Retry-After":"2"})
        try:
            user = store.authenticate(body.username, body.password)
        finally:
            _login_capacity.release()
        if not user:
            raise HTTPException(401, "Invalid username or password.")
        token, session = store.create_session(user["id"], settings.session_ttl_seconds,
                                               request.cookies.get(settings.session_cookie_name))
        response.set_cookie(settings.session_cookie_name, token, max_age=settings.session_ttl_seconds,
            httponly=True, secure=settings.session_secure, samesite="lax", path="/")
        return payload(session)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        store.revoke(request.cookies.get(settings.session_cookie_name))
        response.delete_cookie(settings.session_cookie_name, httponly=True,
            secure=settings.session_secure, samesite="lax", path="/")
        return None

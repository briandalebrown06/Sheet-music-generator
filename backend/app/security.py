from __future__ import annotations

import errno
import hmac
import ipaddress
import re
from http.cookies import CookieError, SimpleCookie
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import _actor, _session
from .auth_store import AuthStore
from .config import Settings


class LocalOnlyMiddleware:
    """Local loopback guard or hosted session boundary, before body parsing.

    The historical name is retained for local compatibility. Hosted mode never
    trusts forwarded headers itself; only the configured ingress may supply
    trusted proxy metadata to Uvicorn. Artifact routes use the same boundary.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        settings.validate_access()
        self.app, self.settings = app, settings
        self.auth = AuthStore(settings.database_path)
        self.auth.initialize()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope["headers"]
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in raw_headers}

        async def secure_send(message):
            if message["type"] == "http.response.start":
                blocked = {b"cache-control", b"x-content-type-options", b"referrer-policy"}
                message = {**message, "headers":[(k,v) for k,v in message.get("headers",[]) if k.lower() not in blocked] + [
                    (b"cache-control",b"no-store"), (b"x-content-type-options",b"nosniff"),
                    (b"referrer-policy",b"no-referrer"), (b"x-frame-options",b"DENY")
                ]}
            await send(message)

        async def reject(status: int, detail: str, retry: int | None = None):
            extra = {"Retry-After":str(retry)} if retry else None
            await JSONResponse({"detail":detail}, status, headers=extra)(scope, receive, secure_send)

        try:
            if sum(k.lower() == b"host" for k,_ in raw_headers) != 1:
                raise ValueError()
            authority = urlsplit("//" + headers.get("host", ""))
            host = authority.hostname
            authority.port
            if authority.username or authority.password or authority.path or authority.query or authority.fragment:
                raise ValueError()
        except ValueError:
            await reject(400, "Invalid Host header.")
            return
        peer = (scope.get("client") or ("", 0))[0]
        if self.settings.app_mode == "hosted":
            expected = urlsplit(self.settings.public_origin)
            if (host, authority.port or 443) != (expected.hostname, expected.port or 443):
                await reject(403, "This Host is not configured for this service.")
                return
            allowed_origins = {self.settings.public_origin}
        else:
            try:
                local_peer = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                local_peer = peer == "testclient"
            if host not in {"localhost", "127.0.0.1", "::1", "testserver"} or not local_peer:
                await reject(403, "This single-user server accepts local connections only.")
                return
            allowed_origins = set(self.settings.allowed_origins)
        origin = headers.get("origin")
        if origin and origin not in allowed_origins:
            await reject(403, "This browser origin is not allowed.")
            return
        if headers.get("sec-fetch-site") == "cross-site":
            await reject(403, "Cross-site API requests are not allowed.")
            return
        path, method = scope["path"], scope["method"]
        unsafe = method not in {"GET", "HEAD", "OPTIONS"}
        upload_chunk = method == "PUT" and re.fullmatch(r"/api/uploads/[0-9a-fA-F-]{36}/chunks/[0-9]+", path) is not None
        public = path in {"/api/health", "/api/auth/session", "/api/auth/login"}
        session = None
        actor = "local"
        if self.settings.authentication_required:
            cookie = SimpleCookie()
            try:
                cookie.load(headers.get("cookie", ""))
                morsel = cookie.get(self.settings.session_cookie_name)
                session = self.auth.session(morsel.value if morsel else None)
            except CookieError:
                session = None
            actor = session["user"]["id"] if session else None
            if not public and not session and method != "OPTIONS":
                await reject(401, "Sign in to access this workspace.")
                return
            if unsafe:
                # Login is protected against login-CSRF by an exact origin and
                # JSON-only body, plus Sec-Fetch-Site rejection above.
                if origin not in allowed_origins:
                    await reject(403, "A matching browser Origin is required.")
                    return
                if path == "/api/auth/login":
                    if headers.get("content-type", "").split(";",1)[0].lower() != "application/json":
                        await reject(415, "Sign-in requires a JSON request.")
                        return
                elif not session or not hmac.compare_digest(headers.get("x-csrf-token", "").encode("utf-8"), session["csrf_token"].encode("ascii")):
                    await reject(403, "The session verification token is missing or expired. Reload and try again.")
                    return
            # Fixed-window quotas are persisted across API instances/restarts.
            if path != "/api/health" and method != "OPTIONS":
                if path == "/api/auth/login":
                    key, limit = "login-client:" + peer, self.settings.login_rate_limit_per_minute
                else:
                    key, limit = "api:" + (actor or "peer:" + peer), self.settings.api_rate_limit_per_minute
                allowed, retry = self.auth.rate_allowed(key, limit)
                if not allowed:
                    await reject(429, "Request rate limit reached. Try again later.", retry)
                    return
                if unsafe and path != "/api/auth/login":
                    # A 500 MB transfer has sixty bounded chunks. Its byte
                    # transfer budget is separate from note/settings mutations,
                    # while the general account request limit still applies.
                    bucket = "upload-chunk:" if upload_chunk else "write:"
                    limit = getattr(self.settings, "upload_chunk_rate_limit_per_minute", 120) if upload_chunk else self.settings.write_rate_limit_per_minute
                    allowed, retry = self.auth.rate_allowed(bucket + actor, limit)
                    if not allowed:
                        await reject(429, "Change rate limit reached. Try again later.", retry)
                        return
        actor_token, session_token = _actor.set(actor), _session.set(session)
        try:
            if unsafe:
                # The bounded spool precedes multipart/JSON parsing. Length is
                # advisory; actual byte counting also covers chunked requests.
                is_upload = path in {"/api/projects", "/api/audio-preview"} and method == "POST"
                limit = self.settings.max_upload_bytes + 65536 if is_upload else (4096 if path == "/api/auth/login" else 8 * 1024 * 1024)
                length = headers.get("content-length")
                if length and (not length.isdigit() or int(length) > limit):
                    await reject(413, "Request body exceeds the configured size limit.")
                    return
                spool_directory = self.settings.data_dir / ".request-spool"
                spool_directory.mkdir(mode=0o700, exist_ok=True)
                with SpooledTemporaryFile(max_size=1024 * 1024, dir=spool_directory) as body:
                    size = 0
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > limit:
                            await reject(413, "Request body exceeds the configured size limit.")
                            return
                        try:
                            body.write(chunk)
                        except OSError as exc:
                            if exc.errno not in {errno.ENOSPC, errno.EDQUOT}:
                                raise
                            # A rollover can exhaust the private temporary
                            # volume before the request reaches its handler.
                            await reject(507, "Temporary upload storage is full. Try again later.")
                            return
                        if not message.get("more_body", False):
                            break
                    body.seek(0)
                    replayed = False
                    async def replay() -> dict:
                        nonlocal replayed
                        if replayed:
                            return await receive()
                        data = body.read(65536)
                        more = body.tell() < size
                        replayed = not more
                        return {"type":"http.request", "body":data, "more_body":more}
                    await self.app(scope,replay,secure_send)
            else:
                await self.app(scope,receive,secure_send)
        finally:
            _actor.reset(actor_token)
            _session.reset(session_token)

# Authentication and private project access

The hosted mode is a provisioned-account service. There is no public registration or emailed password recovery. Operators create accounts through the offline CLI; never place passwords in command arguments, environment files, shell history, container images, or source control.

## Modes and setup

`APP_MODE=local` keeps the original loopback-only workspace and uses the internal owner `local`. Local guards are not authentication. Set `AUTH_ENABLED=true` to exercise accounts on loopback during development. An optional local `PUBLIC_ORIGIN=http://127.0.0.1:5174` allows that exact loopback UI origin for isolated tests.

`APP_MODE=hosted` always requires authentication. `AUTH_ENABLED=false` is rejected. Set one exact `PUBLIC_ORIGIN=https://score.example.com`, without a trailing slash, path or credentials. The API checks the corresponding Host and rejects foreign origins. The deployment proxy must terminate TLS, strip untrusted forwarding metadata and be the only network path to the API. Uvicorn must trust only that proxy's configured IP. The middleware itself does not infer identity or origin from arbitrary forwarded headers.

From the configured API environment:

```sh
python -m backend.app.admin create-user alice
# Enter a unique password twice at the hidden prompts.
python -m backend.app.admin set-password alice
python -m backend.app.admin disable-user alice
```

Passwords require 12–256 characters (at most 1,024 UTF-8 bytes). Changing a password or disabling an account revokes all its sessions. Disabled accounts retain their recordings until the owner/operator explicitly deletes the projects; disabling is not deletion.

Existing anonymous local projects are never auto-assigned to a newly registered hosted user. To migrate them, first back up the complete data volume and stop API/worker processes, then run:

```sh
python -m backend.app.admin claim-local-projects alice
```

This explicitly assigns all `local`-owned projects to that account. Check the target account before restarting services. The normal HTTP API offers no ownership-claim endpoint.

## Credentials and sessions

Password hashes use Python's standard-library `hashlib.scrypt`, with a unique 16-byte random salt, `N=131072`, `r=8`, `p=1`, and a 32-byte derived key. This is the 128 MiB configuration in the current [OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html); [Python 3.11 documents the OpenSSL-backed API](https://docs.python.org/3.11/library/hashlib.html#hashlib.scrypt). Parameter validation prevents unreviewed database hash settings from causing arbitrary KDF cost. At most two login KDFs execute concurrently per API process. Unknown accounts perform the same KDF as known accounts, and failed login responses do not reveal account existence.

Sessions use fresh 256-bit random opaque tokens. Only token SHA-256 digests are stored in SQLite; these high-entropy token digests are distinct from password hashing. A successful login rotates any prior presented session. Sessions expire after `SESSION_TTL_SECONDS` (default 28,800 seconds); there is no automatic sliding extension. At most 20 sessions are retained per account. Password reset, disable and logout invalidate server-side sessions.

Hosted cookies are named `__Host-scorecraft_session` and have `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`, with no Domain. The local authenticated mode uses `scorecraft_session` without Secure because localhost development can use HTTP. The frontend stores no session credential in localStorage or URLs. Browser audio players and export downloads authenticate through the same cookie as API calls.

For authenticated mutations, the server requires an exact permitted Origin and a per-session synchronizer token in `X-CSRF-Token`. The token comes from the session response and stays in frontend memory. Login requires a matching Origin and JSON content, preventing simple cross-origin form login. Cross-site Fetch Metadata is rejected. This follows the [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html); SameSite is additional protection, not the sole defense.

## Authorization, rate limits and private artifacts

Every private API route requires a valid session when authentication is enabled, including source recordings, waveform data, stems, note/model artifacts, scores, manifests and all export URLs. Project lookup and list/create operations additionally use the authenticated owner ID. A guessed project ID does not grant access. Child parts inherit the parent owner. An unauthenticated health response reveals only service status. Private responses have `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

SQLite fixed-window request buckets survive API restarts and are updated atomically. Defaults are 300 API requests/minute per account, 60 mutation requests/minute per account, and 10 login attempts/minute per network peer. Login also has a five-attempt/account/15-minute limit. The client-IP limit relies on correctly configured trusted proxy forwarding; it must not accept spoofed public forwarding headers. Buckets retain hashed identifiers rather than raw usernames/IPs and are removed after 24 hours. Fixed windows may permit bursts across a boundary; use ingress connection/body/time limits as well. Processing/project/storage quotas are separately enforced by project admission.

All request bodies are bounded before multipart/JSON parsing, including requests without Content-Length. Upload bodies use the configured audio-byte limit plus bounded form overhead; sign-in bodies are capped at 4 KiB and other mutation bodies at 8 MiB. Validation errors omit rejected values so credentials and private payloads are not echoed into error collectors. Production deployment disables HTTP access logs; do not enable request-body, cookie, authorization-header or multipart logging in the proxy or error tracker.

## Scope and verification

Targeted tests exercise real password hashes, two independent accounts, hashed session storage, token rotation, expiration, revocation, disabled accounts, CSRF failures, protected media/export URLs, malformed Host/Origin/body requests, rate limits across independent store instances and redacted validation errors. Full-project ownership/deletion and authenticated browser checks are documented in the deployment verification report.

This is an initial account system for a controlled launch, not a claim of independent penetration testing. MFA, self-service password recovery, organization sharing, audit-event delivery, breached-password checks and distributed rate-limit infrastructure are not implemented. Before launch, operators must verify TLS/cookie behavior through the real domain, ingress spoofing resistance, user provisioning, storage permissions, backup/restore and deletion behavior on the target host.

# Deployment audit · 2026-10-27

The app is a **deployment candidate with explicit launch gates**, not a production-readiness certification. No public deployment was performed. Tests used original generated fixtures and disposable data; the existing user recording was not edited or deleted.

## Changes and findings fixed

- Added provisioned accounts, real scrypt hashes, revocable/expiring opaque sessions, hosted Secure/HttpOnly cookies, CSRF/Origin/Host checks and fail-closed hosted configuration.
- Added owner-aware project lists and every private recording/stem/note/score/export route. Child parts inherit ownership; legacy local projects require explicit offline migration.
- Added persistent request limits, atomic per-user project/storage/processing quotas and content-bound upload idempotency. A lost submission response now retries the same project instead of creating duplicate work.
- Added durable tombstones and process/file leases for project deletion. Reads disappear immediately; active jobs are canceled/fenced; worker sweeps remove project/child files and DB records after leases release. Empty coordination locks remain intentionally.
- Corrected keyboard focus wrapping in the deletion dialog; session expiration/logout unmounts private scores and audio players.
- Removed native PDF diagnostics from persistent application logs. Proxy runtime errors now omit request metadata, including private URLs/custom CSRF headers; access logging is disabled.
- Added explicit 507 errors for full upload/request-spool storage, without logging private paths.
- Upgraded the actual runtime and Linux locks to Torch 2.14.1 to fix the reported Torch advisory. Real model regressions and the complete multitrack evaluation passed with unchanged quality metrics. Remaining legacy dependencies are documented as a launch gate.
- Added hard proxy request timeouts. Removed the official Caddy binary's inherited bind capability in the non-root image to avoid its conflict with `cap_drop: ALL`.
- Added pinned deployment images, per-architecture hashed Linux dependency locks, model/account setup and fail-closed preflight. Linux dependency resolution was actually checked; Linux installation/inference was not.

## Executed checks

| Check | Actual result |
| --- | --- |
| Full Python suite with real Basic Pitch/UMX-HQ enabled | **218 passed**, five upstream deprecation warnings |
| Audio-input browser regression | **90 assertions passed** |
| Solo review/edit/save/export browser regression | **71 assertions passed** |
| Mixed-source/stem/selected-part browser regression | **48 assertions passed** |
| Authenticated two-account/deletion/keyboard browser audit | **68 assertions passed** |
| Hosted loopback HTTPS/CSP/proxy browser audit | **26 assertions passed** with the production Caddy configuration adapted to loopback |
| Playback timing and scheduler tests | **8 passed** |
| Local fixture backup/restore rehearsal | 3 projects / 648 notes restored; SQLite integrity and all 18 artifact hashes matched |
| Frontend build, TypeScript and complete formatting | Passed; existing OSMD chunk-size advisory remains |
| Dependency consistency in actual local environment | `pip check` passed |
| Long recording and actual process-failure audits | Passed; measurements below |
| Compose 5.6.1 configuration validation | Passed with official checksum-verified validator |
| Caddy 2.11.7 configuration validation | Passed with official checksum-verified binary |
| Actual Caddy upstream-error privacy exercise | Real 502 logged; synthetic private URI/Cookie/Authorization/CSRF markers absent |
| Hosted preflight with disposable account/database and real installed weights | Passed |

The browser flow used actual decoding and model inference: listen → select clip → upload → process → rendered score → pitch/timing edit → save → reopen → MusicXML/MIDI/PDF export. The audio suite also used Chrome's actual MediaRecorder API with its generated test microphone, verified track cleanup, and distinguished mocked permission/device/unsupported/insecure-browser branches. Hardware microphone capture/listening was not performed.

Two independent accounts and an anonymous client could not access copied private media/stem/score/export URLs. Tests cover session revocation, logout, CSRF, rejected hostile origins, redacted validation errors and durable limits. The upload lost-response test commits a real browser request and then simulates loss of its response; retry returns the original project. Deletion tests confirm immediate 404, actual artifact removal, parent-child cascade, lease-safe delay, restart cleanup and refusal to resurrect a deleted idempotency key.

Keyboard checks include login, skip link, Enter note selection, arrow-key seeking, dialog Tab/Shift+Tab wrapping and Escape. Desktop/mobile screenshots and the authenticated vector PDF were visually inspected: readable notation, complete margins and no clipped systems. Tests ran in installed Chrome on macOS with emulated mobile viewports. Safari/Firefox, physical mobile, assistive-technology testing, physical printing and audio-device latency are unverified.

The additional ingress audit served the built frontend through real Caddy 2.11.7 on loopback HTTPS. The browser accepted the Secure/HttpOnly/SameSite `__Host-` session cookie, played a blob preview, completed real inference/OSMD/edit/save/export under the production CSP, and saw no CSP violations. Forged `X-Forwarded-For` was discarded: the persistent login limit used the actual loopback client. Anonymous and logged-out private exports returned 401. This used an internal CA and a test browser configured to ignore its untrusted certificate; it did not change the OS trust store or validate public ACME/DNS. Total browser assertions: **303**.

A stopped-fixture restore rehearsal copied coherent artifacts and used SQLite backup into isolated storage. All 18 artifact hashes, 3 projects and 648 notes matched and integrity checking passed. This is not a Docker-volume or offsite-backup restore test.

## Long audio and worker failures

`python scripts/audit_long_recordings.py` generated real audio and used the real API/database/worker:

| Fixture | Actual result |
| --- | --- |
| 300-second solo source, clip 0.25–299.75 s | Completed; 648 detected events, 299.500045 s decoded clip, raw MIDI available; worker wall time **6.705 s** |
| 301-second source, selected clip only 0–1 s | Failed clearly against the **300-second whole-source limit**; no notes fabricated |
| 90-second mixed source, clip 0.25–89.75 s | Completed; all four real stems exactly **89.5 s**, each original offset **0.25 s**; worker wall time **6.688 s** |

These long-audio measurements preceded the Torch 2.14.1 update and used Torch 2.11.0 for separation. Those wall times include each local worker attempt and are individual measurements, not throughput/latency guarantees. The fixture note count is not a quality score. Earlier source-separation peak RSS was approximately 928 MiB on a 12-second fixture. Linux CPU/GPU behavior and simultaneous-user capacity were not measured.

`python scripts/audit_worker_recovery.py GENERATED_MIXTURE.wav` sent **SIGKILL to a real worker during UMX-HQ separation**. A replacement with the audit's explicit two-second lease recovered within the saved attempt budget: two attempts, completed result, abandoned output removed. A second actual inference was canceled and its unpublished output was removed. Production keeps the normal lease settings; the short lease is confined to this audit script.

The backend suite additionally exercises corrupt containers, empty files, silence, unsupported/missing/corrupt models, bounded process/RSS/storage failure, stale worker publication, stale score/export revisions, concurrent uploads, conflicting edits and cleanup races. Injected lifecycle fixtures are labeled; the model/browser/long/recovery paths run real inference.

## Reproduction

From the installed local environment, with FFmpeg available:

```sh
RUN_MODEL_TESTS=1 RUN_SEPARATION_TESTS=1 .venv/bin/python -m pytest backend/tests scripts/test_evaluation.py scripts/test_mixed_evaluation.py -q
pnpm -C frontend build
pnpm -C frontend format:check
pnpm -C scripts test:audio
pnpm -C scripts test:review
pnpm -C scripts test:mixed
pnpm -C scripts test:deployment
# Requires Caddy; CADDY_BINARY can select its verified path.
pnpm -C scripts test:ingress
.venv/bin/python scripts/audit_long_recordings.py
# Use the printed disposable output path from that command:
.venv/bin/python scripts/audit_worker_recovery.py /path/to/DEVELOPMENT-90-second-mixture.wav
.venv/bin/python scripts/audit_proxy_privacy.py --caddy /path/to/verified/caddy
```

Browser scripts need installed Chrome/Playwright and use ports 8001/5174 (the ingress audit uses 8002/5191/5192); run them sequentially. `AUDIO_TEST_PYTHON` and `FFMPEG_BINARY` select the actual installed runtime/decoder. They do not access the default project data or hardware microphone.

## Precise launch gates

1. Resolve or explicitly review the current ML dependency advisories with documented reachable-code analysis; the dependency graph includes upstream legacy constraints. See [DEPENDENCY_ADVISORIES.md](DEPENDENCY_ADVISORIES.md). A successful resolver or `pip check` is not a security clearance.
2. Build/run both images on the chosen Linux architecture. Verify non-root volumes, CPU-only inference, all export formats, container resource limits, process cancellation/restart and deletion there. Docker Engine was unavailable here.
3. Provision the actual host/domain/accounts, validate public certificate issuance/renewal, Secure-cookie delivery, CSP, proxy spoofing resistance and private artifact access through the real ingress. No production credentials or DNS changes were made.
4. Define recording/backups/scratch retention, encrypt storage/backups, rehearse restore and deletion reconciliation, and install monitoring/alerts plus operator maintenance. Crash-orphan API scratch cleanup is currently a stopped-service operator task.
5. Run representative workload/load tests and real supported browser/device/microphone checks. Set quotas/capacity from those measurements. No commercial-song accuracy claim follows from these synthetic tests.

The [deployment runbook](DEPLOYMENT.md) contains exact setup commands and the demonstration checklist. [RETENTION.md](RETENTION.md) specifies what deletion does and what remains outside the application boundary.

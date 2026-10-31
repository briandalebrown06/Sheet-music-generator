# Deployment candidate and launch runbook

This repository now includes accounts, project authorization, quotas, durable deletion and a single-host deployment configuration. **It has not been deployed publicly or qualified as production-ready.** The full application and failure paths were exercised locally with real models, including a loopback HTTPS/CSP/session test through Caddy using an internal CA. Docker Engine is unavailable on the audit machine, so Linux image builds, container startup and real-domain TLS remain launch gates. See [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md) for exact evidence and remaining checks.

## Supported topology

One persistent Linux server runs three independent long-lived services:

- **Frontend/ingress:** compiled React assets served by Caddy 2.11.7. It terminates HTTPS and proxies `/api`; it never mounts recordings or the database.
- **Python API:** FastAPI on Python 3.11.18, reachable only from the private container network. Accounts, request validation, preview/edit/export and durable job admission live here. Upload requests enqueue analysis; they do not run model inference.
- **Worker:** a separate Python process with bounded per-attempt child processes. It has no network interface, loads preinstalled models and maintains job leases/recovery and deletion cleanup.
- **Database and queue:** SQLite WAL on the named `private_data` local volume stores accounts, sessions, owned projects, quota ledgers and durable jobs. No Redis service is required for this topology. Keep one API container and one worker; this is not a horizontal scaling design.
- **Private artifacts:** the same private POSIX local volume contains originals, stems, raw detections, edits and score artifacts. All HTTP access passes authentication plus owner checks. Weights and TLS state use separate volumes.

SQLite WAL and the application file locks require a single host and local filesystem. Do not put the volume on NFS or share it between hosts. Moving to multiple hosts requires a deliberately implemented database/queue/object-storage design; changing a connection string is insufficient. [SQLite WAL requirements](https://www.sqlite.org/wal.html).

GitHub Pages can serve static files but cannot run this Python worker, private SQLite storage or authentication API. Short-lived frontend/serverless request handlers are not the inference host.

## Exact first deployment commands

Install Docker Engine and the Compose plugin on the target Linux server. Commands below run from a complete checkout. Base image manifests are pinned by digest; the frontend uses a frozen pnpm lock. Python/runtime dependencies are fully resolved in architecture-specific hashed lockfiles for Linux amd64/arm64. Both graphs were resolved and verified to have compatible binary wheels; this is not an actual Linux installation or inference test. The build deliberately receives current Debian security packages; retain the built image digests and dependency inventory for each release instead of assuming a rebuild is bit-identical forever.

```sh
cp deploy/production.env.example deploy/.env
chmod 600 deploy/.env
# Edit APP_DOMAIN and ACME_EMAIL, then review limits in deploy/.env.
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml build
# Explicitly download and checksum-verify the four UMX-HQ checkpoints.
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm setup
# Provision an account at hidden password prompts; never put it in an env file.
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm setup python -m backend.app.admin create-user alice
# Preflight checks installed dependencies/models, account presence, DB integrity and disk space.
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm initialize
# Keep DNS/firewall restricted to staging until the demonstration below passes.
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d api worker frontend
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail=100 api worker frontend
```

Point the domain's A/AAAA records at this server and allow inbound TCP 80/443 to Caddy for HTTPS issuance/renewal. Do not publish port 8000 or the data volume. Caddy uses internal unprivileged ports 8080/8443 mapped to external 80/443; its inherited bind capability is removed so `cap_drop: ALL` works. Persist `tls_data` and `tls_config`. Test issuance against an appropriate staging domain before changing public DNS. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https), [Compose startup conditions](https://docs.docker.com/compose/how-tos/startup-order/).

The private network is `172.30.67.0/24`; Caddy is `172.30.67.2`. Uvicorn trusts forwarded client metadata **only** from that IP. If this subnet conflicts with the host, update both Compose networking and the API command together, then test spoofed forwarding headers. Do not broaden trust to every address. Caddy has bounded header/body/idle/write times; API concurrency and request sizes are also bounded. [Proxy trust documentation](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

## Configuration

`deploy/production.env.example` contains the complete Compose operator settings. Required values are `APP_DOMAIN` (hostname only) and `ACME_EMAIL`. Compose derives `PUBLIC_ORIGIN=https://APP_DOMAIN` and fixes `APP_MODE=hosted`, `AUTH_ENABLED=true`, private paths and explicit ONNX inference. Hosted startup rejects disabled authentication or a non-HTTPS origin. There is no password or JWT-signing secret in the environment: password hashes and revocable opaque sessions live in private SQLite.

| Settings | Default / purpose |
| --- | --- |
| `SESSION_TTL_SECONDS` | 28,800; sessions expire after eight hours |
| `API_RATE_LIMIT_PER_MINUTE`, `WRITE_RATE_LIMIT_PER_MINUTE` | 300 / 60 per account; durable fixed-window limits |
| `LOGIN_RATE_LIMIT_PER_MINUTE` | 10 per client IP, plus five attempts/account/15 minutes |
| `MAX_PROJECTS_PER_USER`, `MAX_STORAGE_MB_PER_USER` | 100 / 32,768 MiB; storage reserves anticipated analysis output and checks actual bytes |
| `MAX_ACTIVE_JOBS_PER_USER`, `MAX_JOBS_PER_DAY` | 3 / 30; includes selected parts, retries and restart recovery |
| `MAX_UPLOAD_BYTES`, `MAX_DURATION_SECONDS` | 500,000,000 bytes / 3,600 seconds for the full decoded source, even when selecting a short clip |
| `UPLOAD_CHUNK_BYTES`, `UPLOAD_TTL_SECONDS`, `MAX_ACTIVE_UPLOADS_PER_USER` | 8,388,608 bytes / 86,400 idle seconds / three unfinished sessions |
| `UPLOAD_CHUNK_RATE_LIMIT_PER_MINUTE` | 120 per account, also subject to the general API budget |
| `MAX_JOB_ATTEMPTS`, `JOB_TIMEOUT_SECONDS` | 3 / 7,200 seconds |
| `SEPARATION_TIMEOUT_SECONDS`, `SEPARATION_MEMORY_MB` | 14,400 seconds / 4,096 MiB monitored peak RSS |
| `MAX_SEPARATION_STORAGE_MB` | 16,384 MiB per mixed-project family |
| `SEPARATION_THREADS`, `SEPARATION_SEGMENT_SECONDS`, `SEPARATION_OVERLAP` | 2 CPU threads / 10 seconds / 0.25 |
| `PDF_TIMEOUT_SECONDS`, `PDF_MEMORY_MB` | 120 seconds / 1,024 MiB |

Decoder defaults are 600 seconds wall time, 300 seconds CPU, and 512 MiB Linux address-space limit. Caddy admits up to 500,065,536 bytes (the file ceiling plus multipart allowance); the primary large-file path sends 8 MiB requests. Body idle timeout is 30 seconds, total body timeout 30 minutes, response-header timeout 660 seconds, and write timeout 15 minutes. An interrupted chunk can resume from the saved offset. Raising the file ceiling requires updating ingress too; adding another proxy requires checking its own limits/timeouts. No unresolved local 25 MB proxy ceiling remains.

Multipart and decoder scratch use the private persistent volume (`TMPDIR=/data/.scratch`, plus `/data/.request-spool`) rather than the bounded RAM-backed `/tmp`. Preflight requires at least 16 GiB free with the default mixed storage budget; provision additional space for retained projects, concurrent requests and backups. Raising application limits may require raising container memory, disk, ingress timeout and host capacity together. Do not treat a higher limit as measured capacity.

## Models and compute

Basic Pitch 0.4.0 ships its ONNX checkpoint with the package. Its upstream Python 3.11/Linux metadata also requires TensorFlow 2.15, so the lock retains that requirement rather than hiding an inconsistent installation with `--no-deps`; application inference still explicitly selects ONNX. UMX-HQ's four separately licensed checkpoints total 142,551,184 bytes and are installed only by the explicit setup command. Processing jobs cannot download replacement models. The private worker network is disabled. The deployment selects CPU Torch wheels and does not provision CUDA/GPU support. See [SEPARATION_ENGINE.md](SEPARATION_ENGINE.md) for code/weight licenses, maintenance status and checksums, and [AUTH_SECURITY.md](AUTH_SECURITY.md) for account/session semantics.

Measured locally on macOS arm64/Python 3.11.18 before the Torch upgrade (Torch 2.11.0): 12-second UMX-HQ inference used about 928 MiB peak process RSS; a generated 300-second solo recording completed a 299.5-second clip in 6.705 seconds of worker wall time, and an 89.5-second mixed clip completed separation in 6.688 seconds. These are individual synthetic-fixture measurements on the development machine, **not Linux-host capacity, latency guarantees or transcription-accuracy benchmarks**. Container limits are 3 GiB API, 5 GiB worker and 256 MiB frontend, with two CPU allocations each for API/worker. Provision headroom beyond those budgets (for example a 4-vCPU, 12-GiB staging host) and measure your actual workload before choosing a paid production plan. This sizing is an engineering starting point, not a benchmark result.

The large-audio update separately measured a real 720-second, 127 MB performance through resumable HTTP transfer and real Basic Pitch, including forced API/worker restarts. All 720 seconds and the final MIDI note were retained; model-process peak RSS was 971,063,296 bytes on macOS arm64. See [LARGE_AUDIO.md](LARGE_AUDIO.md) for complete measurements and their scope. These do not qualify Linux container or concurrency capacity.

Four 3,600-second stereo FLOAT32 stems require about 5.08 GB before source, normalized audio, selected-part copies, checkpoints and saved revisions. Monitor both disk free space and per-user quota reservations. One-hour mixed separation, GPU, Linux inference and load/concurrent-user capacity were not measured in this update.

## Transcription limitations

Detected notes are estimates of the performance, not a finished professional score. Separation leaks between stems; vocals follow one predominant melody rather than every singer/harmony, bass can have octave or note-boundary errors, and the residual “other” category remains experimental. Drums are audition-only. Vibrato cleanup, constant-tempo quantization, voice/staff allocation and key/downbeat assumptions require human review. No accompaniment is silently generated. Synthetic-fixture results do not establish accuracy on mastered commercial songs; see [MIXED_EVALUATION.md](MIXED_EVALUATION.md) for measured false detections, leakage and timing errors.

## Retention, deletion and operations

Originals, stems, raw detections and saved revisions are retained until explicitly deleted; there is no automatic age-based recording expiry. Canceling a job does not delete its original input. Failed/canceled unpublished mixed-attempt files are cleaned by the worker; successful results and edited notes remain. See [RETENTION.md](RETENTION.md) for precise deletion and temporary-file behavior.

Keep the worker running continuously: it performs inference, expired-lease recovery and physical deletion. Watch health/worker heartbeats, failed jobs, deletion backlog, free disk, memory/OOM events, restart counts, certificate expiry and backup success. Docker restart policies restart exited containers, **not merely unhealthy ones**; alert on unhealthy status and investigate before restarting. Inspect opaque job IDs/error categories; do not enable request-body, cookie or private-score logging.

The image runs as UID/GID 10001 with a read-only root, bounded writable `/tmp`, dropped capabilities and no-new-privileges. New named volumes inherit prepared ownership. Existing imported volumes may need operator ownership repair while services are stopped. Never use a public bucket/static file route for private artifacts. Host administrators can read plaintext volume contents; use host-volume encryption and restricted administrative access.

Back up the complete `private_data` volume with API and worker stopped so database and artifact references are consistent. Keep backups encrypted with an explicit expiry policy; project deletion does not erase older backups. Model weights can be restored separately or redownloaded and checksum-verified. For an operator-controlled snapshot:

```sh
mkdir -p .backups
chmod 700 .backups
docker compose --env-file deploy/.env -f deploy/compose.yaml stop api worker
# Use a new filename each time; this command includes accounts/session hashes and private audio.
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --user 0:0 -v "$PWD/.backups:/backup" setup sh -c 'umask 077; tar -C /data -czf /backup/scorecraft-snapshot.tar.gz .'
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d api worker
```

Encrypt/copy that snapshot to the approved backup destination and apply the operator's expiry policy. Do not upload it to source control. Rehearse restore onto an **empty isolated** volume/server; extract with numeric ownership preserved, run preflight and verify account isolation, source/score checksums and the full demonstration before routing traffic. A live copy of only the `.sqlite3` file can omit WAL state and does not capture coherent artifacts; [SQLite backup documentation](https://www.sqlite.org/backup.html).

Before upgrades, snapshot the stopped volume, build new images, run staging checks and record image digests plus `python -m pip freeze`. Database migrations are additive but no automatic downgrade is supplied; rollback means restoring the compatible pre-upgrade data snapshot and images together. Regularly rebuild/test OS and dependency security updates rather than leaving pinned versions untouched indefinitely. Never run `docker compose down -v` against recordings you intend to keep.

## Demonstration and launch gates

1. Sign in as user A. Upload WAV/MP3/M4A/FLAC or record through the browser, listen, select a clip and submit. Reload while queued and confirm the same project remains.
2. Watch real stages, open the score, select a note with mouse and keyboard, change pitch/timing, undo/redo, save and reopen. Play original, edited and quantized modes; export and open MusicXML, score MIDI, raw MIDI and PDF.
3. Upload a mixed song. Audition all returned categories, select vocals or bass and repeat review/export. Verify drums remain audition-only and “other” is labeled experimental.
4. In another account and an anonymous browser, open copied source/stem/score/export URLs. They must return 404/401 without content. Verify the hosted cookie's Secure/HttpOnly/SameSite attributes, CSRF rejection, logout and expired-session behavior through the real HTTPS proxy.
5. Upload the generated 12-minute 127 MB fixture through the deployed proxy. Interrupt a chunk, reload/reselect and resume; confirm byte checksum, full decoded timeline and final MIDI note. Test 500,000,000/500,000,001-byte and 3,600/3,600.1-second limits independently. Retry with the same submission key; it must not create a second project. Test corrupt/silent inputs, missing weights, canceled jobs, a killed/restarted worker and quota/rate-limit errors.
6. Edit after exporting and verify stale revisions are rejected. Delete an active project and a mixed parent; access must disappear immediately and disk/DB artifacts must be reclaimed after active leases release. Restart the worker during cleanup and confirm completion.
7. Check mobile viewport, Tab/Shift+Tab, Enter/Space, Escape dialogs, focus visibility and audio seeking. Test the actual browsers/devices you will support, including a hardware microphone.
8. Complete Linux image build/startup/inference, real-domain TLS/proxy/CSP tests, ingress spoofing tests, backup/restore, deletion-retention operations and a representative concurrency/load test. Confirm alerting and an operator owns upgrades/backups/account recovery. Only then remove staging access restrictions for the intended controlled launch.

Public DNS, a production host, TLS issuance, backup infrastructure, monitoring and production credentials have **not** been created by this task. MFA, self-service recovery, team sharing, distributed storage/queues and a formal independent security assessment remain outside this initial deployment candidate.

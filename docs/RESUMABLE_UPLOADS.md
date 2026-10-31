# Resumable private audio transfers

Large files use an owner-scoped upload session before becoming a project. The default file limit is **500,000,000 bytes** (decimal 500 MB), independently of the **3,600-second decoded-source duration** limit. The selected clip does not bypass either limit. The browser sends bounded chunks; the API never holds the complete large upload in memory.

## Protocol

All endpoints use the existing session cookie, owner checks, Origin/CSRF protection, and private `no-store` responses. An upload ID is an opaque UUID, not a bearer credential. Foreign owners receive 404, unauthenticated hosted requests receive 401.

1. `POST /api/uploads`, with `Idempotency-Key` and JSON `filename`, `size_bytes`, and optional project metadata, creates or resumes a transfer. The same key/details returns the same session; conflicting details return 409. Filename extensions are admitted here, but file signatures and actual decoding are validated separately.
2. `GET /api/uploads/{id}` returns `id`, transfer `status`, acknowledged `offset`, `size_bytes`, `chunk_size`, `expires_at`, optional `project_id` and server `sha256`, and a `chunks` manifest containing each acknowledged `{offset, size_bytes, sha256}`. `preview_url` is available after all bytes have arrived but before submission.
3. `PUT /api/uploads/{id}/chunks/{offset}` sends `application/octet-stream` and mandatory hexadecimal `X-Chunk-SHA256`. The default chunk size is 8,388,608 bytes; all chunks except the last must have the advertised size. Only contiguous, checksum-verified bytes advance the saved offset. Replaying identical bytes after a lost response is idempotent. Changed bytes, incorrect offsets, empty chunks, excessive lengths, and bad hashes are rejected.
4. Optional `GET /api/uploads/{id}/preview` validates/decodes the full uploaded source and serves a cached mono WAV using normal ranged audio responses. Point a native audio element at this protected URL rather than fetching the entire WAV into a JavaScript blob. No second source transfer is needed. The cached preview is published by atomic rename only after successful decoding; interrupted partial previews are rebuilt.
5. `POST /api/uploads/{id}/complete` verifies the complete byte count and every saved chunk hash again while computing a full-file SHA-256. An optional JSON body overrides current project/clip/hint fields; omitted fields retain the session's settings. The original moves into a generated private project directory without a full-file copy. One transaction creates the project/job and marks the transfer completed. Repeating completion returns the same project; changing already-submitted settings returns 409. A completed **transfer** means a durable project was queued, not that transcription has finished.
6. `DELETE /api/uploads/{id}` discards an unfinished transfer. Once submitted, use the normal project deletion endpoint; transfer deletion cannot silently delete a project.

Project metadata fields are `title`, `source` (`upload` or `microphone`), `input_mode` (`solo` or `mixed`), `clip_start_seconds`, `clip_end_seconds`, `instrument_hint`, `tempo_hint`, `time_signature_hint`, `minimum_note_seconds`, and `merge_gap_seconds`. The API validates the same selection/hint constraints as the existing multipart route.

Before resuming from a reselected local file, the browser verifies every acknowledged chunk against that file. Matching only filename or size is insufficient. The SHA-256 manifest and final full-file hash are saved privately with the project in `upload-manifest.json`; model inference still consumes the preserved original and independently validates actual decoded duration/content.

## Durability, quotas, and cleanup

Private paths are generated under `DATA_DIR/uploads/{uuid}` and `DATA_DIR/projects/{uuid}`. No client filename is used as a storage path. Each chunk is flushed and fsynced before its manifest/offset commits. A restart truncates unacknowledged tail bytes before appending the next chunk. A crash between moving the original and publishing the project can be retried from its already-received source, without uploading it again. Nonblocking POSIX file leases prevent concurrent requests or cleanup from modifying the same transfer at once; a busy transfer returns 503 with `Retry-After: 1`.

Unfinished uploads reserve their source bytes and a maximum-size controlled mono preview against the account's storage quota. They also reserve project admission slots and have a separate default cap of three unfinished sessions per owner. Completion replaces its own transfer reservation with the project's analysis/storage reservation; account processing quotas still apply. If admission fails, the complete original remains resumable rather than being discarded.

Transfer chunks use a dedicated default **120 requests/minute/account** limit, while still consuming the general account request budget. This permits a default 500 MB transfer's sixty chunks without exhausting the separate note/settings mutation limit. Metadata creation, completion, and discard still consume the normal write budget. Clients must respect 429/503 retry responses and continue from the server's acknowledged offset.

`UPLOAD_TTL_SECONDS` defaults to 86,400 seconds and is refreshed by acknowledged chunks. Reads do not extend it. The worker and opportunistic API sweep remove expired/discarded transfer files, previews, uncommitted project directories, and chunk metadata after active leases release. Cleanup failures remain retryable. Successful submission removes its temporary transfer directory; its project-owned original remains until project deletion. Opaque expired/discarded replay markers remain for up to seven days beyond expiry, without filenames, settings, or audio hashes. Backups and SQLite/WAL retention remain subject to the deployment retention policy; this is not forensic erasure.

The compatibility multipart endpoints remain available. Their middleware spool now uses private `.request-spool` storage; deployment uses private persistent scratch for the multipart parser as well, avoiding a 500 MB request filling a small RAM-backed `/tmp`. The new large-file path only spools one bounded chunk at a time. See the deployment configuration for disk and proxy settings; no public/serverless hosting capacity is implied by local checks.

## Verification

`backend/tests/test_resumable_uploads.py` uses generated WAV bytes and temporary databases. It covers restart/resume, uncommitted-tail recovery, lost-response replay, metadata conflicts, independent declared-size and clip-duration bounds, chunk/full-file corruption, malformed audio headers, interrupted requests, crash-after-move recovery, atomic/idempotent project publication, two-account ownership, CSRF, separate transfer rate limits, storage/session quotas, expiry/leases, and real FFmpeg streamed preview/clip reuse.

```sh
DATA_DIR=/tmp/scorecraft-upload-test-default-app PATH="$PWD/.runtime/bin:$PATH" \
  .venv/bin/python -m pytest backend/tests/test_resumable_uploads.py -q
```

The explicit `DATA_DIR` isolates the module's default API instance; every actual test also uses its own temporary data directory. Large-file and long-recording capacity evidence is reported separately by the full end-to-end audit; declaring a 500 MB session in a unit test alone does not prove that 500 MB was transferred or analyzed.

# Audio input workflow

The studio handles one local file or one microphone take per project. Recording and playback happen in the browser. Transcription starts only after recording has stopped, its final audio blob is ready, and the user chooses **Create transcription**. There is no live streaming transcription.

## Before submission

Upload WAV, MP3, M4A, FLAC, OGG, WebM, or MP4 audio. Browser playback and the installed FFmpeg build determine which codecs can be read. The extension and container signature are preliminary admission checks; neither is treated as proof that an entire file is valid.

Microphone access is requested only by **Start recording**. The client checks `MediaRecorder.isTypeSupported()` at runtime and uses the recorder's actual reported MIME type when constructing the file. It distinguishes permission denial, a missing microphone, device access problems, unsupported recording formats, and an insecure context. Recording requires HTTPS or a trustworthy local origin such as localhost. No microphone permission is requested just by opening the screen.

The user can stop, listen, discard, select start/end points in seconds, play the selected section, and optionally supply musical hints. The UI keeps a separate elapsed-time clock and waits for the final recorder event before enabling submission. Tracks are stopped when recording ends, fails, is cancelled, or the component unmounts; late permission results are released instead of starting a stale recording.

If the browser cannot play the original or read its duration, **Prepare playable preview** asks the local API to create a WAV playback copy. This action does not create a project, queue a job, or run the pitch model. Submission still preserves the original uploaded bytes.

Browser MIME support is a capability indication, not a guarantee that capture will succeed. Permission promises may remain pending, and recorder chunk intervals are not accurate timers. These decisions follow [MDN's MediaRecorder format documentation](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/isTypeSupported_static), [getUserMedia requirements and errors](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia), and [dataavailable event timing](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/dataavailable_event).

## Persistent submission contract

Defaults are 500,000,000 bytes and 3,600 decoded seconds, independently enforced. The current client uses resumable 8 MiB chunks for files above 8 MiB; smaller inputs retain `POST /api/projects`. Actual network upload progress is shown separately from processing stages. Pause/retry keeps verified chunks; after reloading the same tab, reselect the same original file to resume. A bounded manifest is kept in session storage, scoped to origin/account; no audio bytes or credentials are stored there. Acknowledged chunks are hashed again against the reselected file before continuing.

`POST /api/uploads` creates an owned, expiring session with `Idempotency-Key`, `filename`, `size_bytes` and the project metadata below. `GET /api/uploads/{id}` returns its durable offset and checksums. `PUT /api/uploads/{id}/chunks/{offset}` accepts a full advertised chunk (or the remaining final bytes) plus `X-Chunk-SHA256`; only contiguous, verified chunks advance the offset. `POST /api/uploads/{id}/complete` verifies the entire stored file and publishes one project/job, including after a lost response. It accepts updated clip/hint fields, so preview does not require uploading again. `GET /api/uploads/{id}/preview` serves a range-capable normalized WAV after transfer; `DELETE /api/uploads/{id}` discards unfinished content. Every route uses existing authentication/owner/CSRF protections. See [LARGE_AUDIO.md](LARGE_AUDIO.md) for expiry, quotas and test evidence.

`POST /api/projects` still accepts a multipart `file`, `title`, `source` (`upload` or `microphone`), and the following optional fields:

| Field | Meaning |
| --- | --- |
| `clip_start_seconds` | Start offset within the original audio; defaults to zero |
| `clip_end_seconds` | End within the original; omit to use the decoded end of the recording |
| `instrument_hint` | `voice`, `piano`, `guitar`, `bass`, `violin`, `flute`, or `other` |
| `tempo_hint` | Integer 30–300, in quarter-note beats per minute |
| `time_signature_hint` | `2/4`, `3/4`, `4/4`, `6/8`, `9/8`, or `12/8` |

The API rejects invalid metadata and unsafe/oversized inputs, then persists the original and a queued processing job. The worker performs actual full-stream decoding and selected-clip validation. A corrupt, unsupported, over-duration, or silent input results in a persistent failed job with an actionable message. Receiving a project ID does not mean decoding or transcription has succeeded.

Project responses include:

- `duration_seconds` and `original_duration_seconds`: the decoded original duration, once known.
- `clip_start_seconds`, `clip_end_seconds`, and `clip_duration_seconds`: resolved to actual normalized PCM sample boundaries after validation.
- `instrument_hint`, `tempo_hint`, and `time_signature_hint`: saved across refreshes and retries.
- `analysis_available`: whether a validated normalized selection is available, including when subsequent model inference fails.

Selections must contain at least 0.1 seconds. Endpoints must be finite and within the decoded recording. A maximum 50 ms tolerance accommodates small differences in browser duration reporting; the resolved range is stored. The configured duration limit applies to the **whole original**, so choosing a short excerpt does not bypass it.

Detected note times are **relative to the selected clip**. Add `clip_start_seconds` to locate a note within the original recording. Tempo and meter hints initialize canonical notation settings only. Missing tempo can be suggested from regular onsets or falls back to a labeled 120 BPM assumption; missing meter assumes 4/4. The instrument hint labels the draft part and does not select a different pitch-detection model. These hints are not inferred musical facts. See [NOTATION.md](NOTATION.md) for manual correction and preview.

## Original and analysis audio

The original file is retained under a server-generated UUID project directory and a fixed server-selected filename. The original filename is display metadata only. Each processing attempt writes its own normalized selection as mono, 22,050 Hz, 16-bit PCM WAV. The intermediate decoded full recording is removed after selection.

- `GET /api/projects/{id}/audio` returns the untouched original.
- `GET /api/projects/{id}/analysis-audio` returns the validated selection when available.
- `POST /api/audio-preview` returns a transient decoded WAV; it never inserts project or job records.

SQLite stores project metadata, clip offsets, hints, status, and attempt history. An additive schema migration preserves projects created by the earlier foundation. Browser refresh does not remove submitted projects; unsent recordings remain browser-local and are not promised to survive a page reload.

## Decoder validation and limits

FFmpeg is launched through a list of arguments, without a shell. Its protocols and demuxers are allowlisted, audio is selected explicitly, and decoding errors cause failure. The worker decodes the bounded original before copying the selected PCM frames, so corruption outside the selection is not deliberately skipped. An over-duration sentinel detects excess length without unbounded decoding.

Limits include configured upload bytes and decoded duration, subprocess wall time, CPU time, output bytes, a single decoder/filter thread, and a per-allocation FFmpeg cap. A separate launcher applies OS CPU, file-size, descriptor, and core-dump limits on POSIX. Linux also receives an address-space cap. macOS does **not** claim a hard aggregate RAM cap; FFmpeg's `-max_alloc` limits individual heap allocations. See the official [FFmpeg common options](https://github.com/FFmpeg/FFmpeg/blob/master/doc/fftools-common-opts.texi) and [FFmpeg processing options](https://github.com/FFmpeg/FFmpeg/blob/master/doc/ffmpeg.texi).

The selected clip is checked for silence after normalization. It is rejected only when both peak and RMS level are at or below `SILENCE_THRESHOLD_DBFS` (default −70 dBFS). This avoids treating quiet notes separated by pauses as silence. It is an input-quality gate, not a model-confidence score.

See `.env.example` for `MAX_UPLOAD_BYTES`, `MAX_DURATION_SECONDS`, `UPLOAD_CHUNK_BYTES`, `DECODE_TIMEOUT_SECONDS`, `DECODE_CPU_SECONDS`, `DECODE_MEMORY_MB`, and `SILENCE_THRESHOLD_DBFS`. The legacy `MAX_UPLOAD_MB` uses MiB and is read only if `MAX_UPLOAD_BYTES` is absent. Keep API and worker settings aligned and restart both after configuration changes.

## Verification boundaries

Use [VERIFICATION.md](VERIFICATION.md) for actual test results. Browser automation with a test microphone validates application behavior, not a physical microphone, operating-system permission panel, or every device/browser combination. Synthetic audio fixtures are labeled in test code and are never substituted for model output.

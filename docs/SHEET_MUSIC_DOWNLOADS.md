# Sheet music and downloads · verification

Completed transcription projects now open with **Your sheet music** directly below the project header. The inline score, **View sheet music**, **Full-screen score**, and PDF/MIDI/MusicXML downloads are available without opening settings. A mixed recording must first have a selected part transcribed; its completed part project uses the same results screen.

Older completed projects without a canonical score automatically receive an initial score from their saved notes and provisional musical defaults when opened. This preserves every performance note and its revision. Existing saved scores and manually selected settings are loaded as they are, without replacement by defaults.

PDF is labeled **Printable sheet music**, MusicXML **Editable sheet music**, and the main MIDI download uses the current saved canonical score. Raw-performance MIDI remains separately labeled in the preserved-performance section. Downloads use saved corrections, quantization, and pitch transposition. Unsaved edits disable downloads and show a visible **Save changes** action so an older saved version is not silently downloaded.

One click fetches or generates an export and downloads it. An inline preparation message remains visible while it runs. Success leaves a **Download … again** link; failure leaves the specific error and a retry action beside that format. Saving a new revision invalidates earlier ready links. Clicking a ready link checks the server revision again, so a save in another tab cannot silently download old bytes. Navigation/logout releases private download blobs. Server filenames identify the song title, written key-signature pair, and saved revision, for example:

```text
DEVELOPMENT-Sheet-Music_D-major-B-minor_score-r2.pdf
DEVELOPMENT-Sheet-Music_D-major-B-minor_score-r2.mid
DEVELOPMENT-Sheet-Music_D-major-B-minor_score-r2.musicxml
```

The current score model stores a key signature without a major/minor mode, so filenames include both possibilities. This label does not claim automatic key detection. Transposition shifts canonical note pitches; the current UI's written key signature is separately editable.

## Executed browser audit

On 2026-10-27, `pnpm -C scripts test:sheet-music` passed **144 assertions** using installed Chrome on macOS, real FFmpeg decoding, the installed Basic Pitch ONNX model, SQLite, a background worker, OSMD, and the actual export engines. The test uploaded an explicitly labeled generated melody, selected a five-second clip, and obtained 11 real detected events. No expected notes were substituted for model output.

The audit verified:

- Actions appear automatically on completion, without reloading, and immediately when reopening a completed project.
- All four primary actions fit in the opening 1440×1000 desktop and 390×844 mobile viewports, outside menus, with targets at least 44 pixels high and wide.
- The primary section contains an actual rendered score. View focuses the inline preview; full-screen displays the score, Escape closes it, and focus returns to the trigger.
- One click generates and downloads a real PDF, with observable preparation status and a retained direct-download link.
- A deliberately injected HTTP 503 shows its explanation and retry action; retry downloads actual MIDI successfully.
- Unsaved note and transposition changes block earlier exports. Saving clears prior ready links and produces revision 2 files.
- Downloaded MusicXML reparses with the corrected pitch plus a two-semitone transposition. MIDI has that same sounding pitch and the timing from the saved rational score.
- The downloaded PDF parses as one A4 page, 595.2756×841.8898 points. Raw MIDI remains a separate option.
- Reopening retains the correction, mobile download returns the same saved filename, and no unhandled browser exceptions occur.
- Initial score-record and MusicXML load failures show the error and **Retry loading sheet music** in the primary section, without a misleading create-score action. Removing the injected failure and clicking retry restores actual notation/downloads.
- An unavailable PDF engine is explained beside its disabled button while MIDI and MusicXML remain available.
- A real second save through the API advances the score revision. Clicking the earlier ready link fetches again, receives HTTP 409, shows its explanation, and produces no stale download.
- Reopening an existing score preserves its manually selected key/transposition and does not advance the score revision.
- A legacy fixture removes only the disposable project's canonical score row. Reopening actually constructs/saves notation, renders the score, enables all three downloads, and downloads a real MIDI file without opening settings. Every saved note value and the performance-note revision remain unchanged.

The desktop and mobile screenshots were visually inspected. Primary downloads are prominent and readable; the narrow inline score remains in its labeled horizontal scrolling area. No physical mobile-device, other-browser, printing, or transcription-accuracy improvement claim follows from this audit. The transport failures and unavailable-PDF health response are test-only injected responses; successful retries and cross-tab revision rejection use the real backend. The legacy fixture changes only isolated test storage, never the normal project database.

The separate hosted ingress audit passed **34 assertions**, including actual PDF/MIDI/MusicXML browser downloads under the production Content Security Policy, authenticated HTTPS sessions, and private download-blob cleanup on logout. It used real Caddy on loopback with an internal test certificate; it did not validate Linux containers, public DNS, or ACME issuance. **44 focused backend tests** also passed for notation/review/export behavior, revision protection, and descriptive filenames.

## Reproduction

Use the existing local setup with Python models, FFmpeg, frontend dependencies, Playwright, and Chrome installed. Run from the repository root:

```sh
pnpm -C scripts test:sheet-music
```

The script starts disposable API/worker/storage on port 8001 and Vite on port 5174; it does not modify the normal project database. Run it sequentially with other browser suites using those ports. `NOTATION_TEST_PYTHON`, `FFMPEG_BINARY`, and `CHROME_CHANNEL` can select installed runtimes. It prints its temporary report directory and saves desktop/mobile screenshots, downloaded files, parsed results, and service logs there. If setting `SMOKE_ARTIFACT_DIR`, use a new isolated directory with a basename beginning `scorecraft-sheet-music-`; the legacy fixture explicitly checks this before modifying its test database.

## Updating an existing installation

No new runtime dependency or environment variable is required. Rebuild the frontend and restart the Python API; keep the existing database, private artifacts, and worker configuration. For the documented Compose topology, rebuild images and recreate services using the existing `deploy/.env` and named volumes. This feature does not clear the outstanding public-launch gates in [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md).

```sh
pnpm -C frontend build
# Restart the local API using the existing README command, or on the deployment host:
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d api worker frontend
```

Browser API behavior was checked against [MDN native modal dialogs](https://developer.mozilla.org/en-US/docs/Web/API/HTMLDialogElement/showModal), [Content-Disposition filenames](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Disposition), and [object URL cleanup](https://developer.mozilla.org/en-US/docs/Web/API/URL/revokeObjectURL_static). The full-screen score uses a modal dialog occupying the viewport; it does not request OS-level browser fullscreen.

The existing real review regression also passed **71 assertions** after the layout change, covering playback, note selection, undo/redo, editing, saves, and exports. The final legacy-score addition was then exercised by the 144-assertion focused suite.

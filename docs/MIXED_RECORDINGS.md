# Mixed recordings and selected-part scores

Choose **Solo voice or instrument** for the existing direct Basic Pitch workflow, or **Full song / mixed recording** to separate the selected audio clip first. Recording still stops before analysis starts; there is no live transcription.

A mixed project preserves its original audio and queues a durable separation job. After completion, audition the actual **vocals**, **bass**, **drums**, and **other** stems. Choosing a supported category creates a persistent child project with its own transcription job, raw artifacts, editable notes, canonical score and exports. Selecting the same category again reopens that child, preserving edits. You can create separate scores for several categories.

- **Vocals:** estimates one predominant melody using pYIN. Vocal harmonies, breath, backing singers, glides and leakage can confuse it. A vocal stem is not a promise of one isolated singer.
- **Bass:** uses a monophonic pYIN profile with a longer analysis window and lower pitch range. Review pitch octaves and note boundaries.
- **Other:** uses Basic Pitch and remains experimental. This category can contain several pitched instruments and residual audio; it is not an isolated guitar or piano.
- **Drums:** audition only. The API rejects pitched transcription of this category. Drum notation requires a separately implemented and evaluated engine.

These results are **transcription**, estimates of notes present in the chosen audio. **Arrangement** means newly created accompaniment or simplified parts. This application does not generate accompaniment or silently turn an arrangement into a claimed detection.

## Engines and installation

Open-Unmix UMX-HQ is installed and run locally behind `SeparatorAdapter`. It was selected after actual installation/inference and separate code/weight license checks. Demucs-compatible implementations were evaluated, but the official Demucs weight license is currently described as unclear by its maintainer. The older UMX-HQ reference implementation has an explicit MIT-licensed author weight deposit. See [SEPARATION_ENGINE.md](SEPARATION_ENGINE.md) for current maintenance evidence, dependency pins, compute measurements and license sources.

```sh
.venv/bin/python -m pip install -r backend/requirements-separation.txt
.venv/bin/python backend/install_separation.py
.venv/bin/python -m pip check
```

Restart the API and worker afterward. Use the same installed environment for both in this initial local setup. The explicit installer downloads and verifies four pretrained files; processing jobs never download models. Missing or incompatible dependencies fail clearly. Solo processing does not require the separator.

## Part transcription and vibrato

Vocal and bass inference uses [librosa 0.11 pYIN](https://librosa.org/doc/0.11.0/generated/librosa.pyin.html), an F0/voicing estimator with [ISC-licensed code](https://github.com/librosa/librosa/blob/0.11.0/LICENSE.md) and no learned checkpoint. It operates on mono 22,050 Hz audio with a 256-sample hop. Vocals cover MIDI 36–96 with 2,048-sample frames; bass covers MIDI 21–60 with 4,096-sample frames. Those ranges are processing assumptions, not verified instrument identities.

Raw F0, voicing flags, voicing probabilities, RMS and frame times are saved in `raw-model-output.npz`. Raw discrete events and MIDI remain separate from cleaned editable notes. Cleanup applies a short median filter within each voiced interval, persistent semitone hysteresis, minimum note length and deep amplitude-dip reattack detection. It does not interpolate across silence. The global Basic Pitch fragment-merge gap is not used by the pYIN profiles; their explicit settings are recorded in the manifest. Wide vibrato and portamento can still fragment, repeated notes without clear attacks can merge, and users can correct these events in the editor.

pYIN voicing scores are not calibrated note-accuracy percentages. Velocity is a voicing-derived audition proxy, not measured playing force. Raw pYIN MIDI uses discrete pitches; the preserved F0 arrays retain the continuous curve for inspection. Synthetic playback uses simple tones.

Thirty-second core windows have half-second context on both sides, anchored to the same sample-hop grid. Overlap context is discarded and the global frame sequence is segmented into notes once, preserving offsets and avoiding independent duplicate notes at chunk boundaries. A real sustained-note test crosses that boundary.

## Timing and reproducibility

Separation runs on the selected stereo clip at 44,100 Hz. Every output stem has exactly the same frame count, starts at zero within that selected clip, and retains the parent's offset within the original recording. No trimming, time stretching or per-stem normalization occurs.

A selected-part child owns a copy of its chosen stem. Its editable note times are local to that stem. To compare a child note with the original song:

`original_song_seconds = child.source_origin_seconds + child.clip_start_seconds + note.start`

Canonical quantized beats remain separate. Review playback uses the existing score-to-audio map, and displays the additional original-song offset. The child's **Separated stem** playback uses the actual separated audio, while the parent retains the full original recording.

The separation manifest records model/runtime versions, all checkpoint checksums and license provenance, input/output checksums, exact sample counts, segmentation, overlap, Wiener settings and CPU thread settings. Each part's transcription manifest includes its separation provenance, category/profile, parent ID and original-song offset. These allow a run to be understood and repeated without inferring which model or defaults were used.

## Durable jobs and limits

Separation and selected-part analysis share the existing queued/processing/completed/failed/canceled job lifecycle, leases, stage messages, cancellation and bounded retry/restart recovery. There are no invented completion percentages or runtime predictions. Duplicate category selection is serialized in SQLite and reopens the existing child.

Defaults are a 500,000,000-byte upload limit and 3,600-second decoded original duration. The duration limit applies to the entire uploaded source even when selecting a short clip. See [LARGE_AUDIO.md](LARGE_AUDIO.md) for measured solo capacity and untested one-hour mixed separation. Additional mixed limits are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `SEPARATION_SEGMENT_SECONDS` | 10 | Bounded model context per segment |
| `SEPARATION_OVERLAP` | 0.25 | Fraction of neighboring context overlapped |
| `SEPARATION_THREADS` | 2 | Torch CPU threads |
| `SEPARATION_TIMEOUT_SECONDS` | 14400 | Maximum separation-attempt wall time |
| `SEPARATION_MEMORY_MB` | 4096 | Peak process RSS threshold monitored during mixed analysis on POSIX |
| `MAX_SEPARATION_STORAGE_MB` | 16384 | Parent, stems, child input copies and analysis outputs combined, in MiB |

Memory/storage are checked synchronously at processing stages and before publication, as well as periodically by the watchdog; it is not a hard instantaneous reservation or a full operating-system sandbox. Model context and output bounds prevent unbounded full-song tensors. Windows memory/process behavior has not been verified; the tested path is local macOS CPU inference. Reduce clip/segment length or raise a limit only when the machine has adequate resources. Four 300-second stereo float stems require about 423 MB before source/analysis/child copies.

Failed and canceled mixed attempts lose their unpublished generated files after the worker stops them. Original uploads, saved edits, successful stems and child inputs remain. Attempts and errors remain in job history. No automated deletion of user recordings is performed.

## API

- `POST /api/projects` accepts multipart `input_mode=solo|mixed`; omitted means solo.
- `GET /api/projects/{id}/stems` returns actual categories, audition URLs, support/profile details and existing child links.
- `GET /api/projects/{id}/stems/{category}/audio` streams the real aligned WAV.
- `POST /api/projects/{id}/stems/{category}/transcribe` creates a child (201) or reopens the existing category child (200). Drums are rejected with 422.
- `GET /api/projects/{id}/separation-manifest` downloads processing provenance.
- Existing job cancellation/retry, review editing, notation and export endpoints work for child projects.

## Verification

```sh
RUN_MODEL_TESTS=1 RUN_SEPARATION_TESTS=1 .venv/bin/python -m pytest backend/tests scripts/test_evaluation.py scripts/test_mixed_evaluation.py -q
pnpm -C scripts test:mixed
.venv/bin/python scripts/evaluate_mixed.py
```

The Chrome integration uploads a generated mixture, auditions all four real stems, selects bass, runs real pYIN, edits/saves, reparses XML/MIDI/PDF and reopens the project. It also checks a nonzero original offset, stem/raw preservation, duplicate selection, drum rejection and desktop/mobile layout. Tests use disposable project data and never edit the user's recording. See [MIXED_EVALUATION.md](MIXED_EVALUATION.md) for measured leakage and transcription errors and [VERIFICATION.md](VERIFICATION.md) for actual test coverage and limitations.

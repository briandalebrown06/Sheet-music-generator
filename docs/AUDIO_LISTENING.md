# Audio-model review

`scripts/listen_audio_compare.py` sends actual WAV samples through a local
MOSS-Music audio encoder and asks a question about those samples. It does not
read note events or MIDI, call a hosted model, or change saved scores. The
current chat model still has no direct audio input; reported findings must be
attributed to the audio model that produced them. A model response is not ground truth,
calibrated accuracy, human listening, or pianist approval.

## Actual local evaluation, 2026-10-27

The pinned 4-bit model was installed and run on real audio excerpts in the
separate environment. No audio was uploaded. It correctly described a sampled
piano passage as instrumental keyboard music, but **failed the controls needed
for musical corrections**:

- It called a passage shifted up two semitones identical, or the same absolute
  pitches, in both short-answer and sampled thinking trials.
- A concatenated passage played at 75% of the original tempo was called
  identical. A sampled thinking trial instead invented a half-step pitch shift
  and said the tempo was identical.
- An identical-audio control was recognized in some runs, while another run
  claimed it had no access to the supplied audio.
- Initial greedy thinking responses exhausted 1,024 tokens; a 4,096-token
  retry timed out at 240 seconds. Those attempts are not usable reviews.

Successful short-answer calls took 3.34–13.51 seconds in these limited trials;
recorded MLX peak allocation was approximately 6.4–6.5 GB. These are observations
from this host, not a deployment benchmark or an accuracy estimate. The runner's
21 contract tests pass, but software correctness does not establish the model's
musical reliability. Private input hashes, responses, settings and failure
records are under `output/audio-listening/`.

**No score edits were made from these local model responses.** The prepared
local whole-song review was not accepted as a reliable correction path.

## Google audio review, 2026-10-28

The user explicitly authorized sending the original recording and corrected
piano audio to Google for review. Gemini 3.8 Flash was exercised through AI
Studio with Medium thinking and no search, code execution, or other tools.
The initial six-attachment comparison failed: the interface renamed/reordered
attachments, and the response invented pitch differences between identical
audio. That response is not accepted as evidence.

Three independent trials then used one WAV per request: excerpt A, one second
of silence, then excerpt B. Source PCM was preserved exactly and expected
answers were withheld from prompts. The responses correctly identified:

- A two-semitone upward shift as higher pitch, unchanged tempo and melodic contour.
- Identical excerpts as unchanged pitch, tempo and sequence.
- A version at 75% tempo as slower, with unchanged pitch and sequence.

This limited control gate supports proceeding with an ordered audio comparison;
it is not a benchmark, calibrated accuracy, or proof that Gemini can identify
exact wrong notes. Responses and input hashes remain in ignored private output
under `output/audio-listening/google-review/single-file/`, including
`test-1-response.private.txt`, `test-2-response.private.txt`, and
`test-3-response.private.txt`.

Two full-song comparison MP3s have now been uploaded, one for the solo edition
and one for singer accompaniment. Each places the original at 0–153.08 seconds,
silence until 155 seconds, and the saved revision-2 piano render at 155–309
seconds. Piano audio was resampled to the original's 48 kHz without changing
gain; concatenated PCM equality was checked before lossy 192-kbps MP3 encoding.
Both MP3s decode to exactly 309 seconds. Prompts request findings in original-song
coordinates and distinguish expected arrangement omissions from transcription
errors. Both full-song responses reported no actionable errors, but their broad
claims are **not accepted as musical validation**. Shorter comparisons produced
contradictions: an opening review described the piano as entirely monophonic
with no bass, although the exact rendered MIDI has 77 notes in eight seconds,
30 below MIDI 60, with 5.28 seconds of polyphony. Fresh rendering and the uploaded
excerpt match the saved audio exactly. An ending review suggested quieter low
attacks, while the original's low-frequency energy also increases in that
passage. Neither claim alone justifies changing the score.

An unpublished audition candidate reduces 26 late low-note velocities to 80%
of their saved values (rounded), keeping pitches, timing, IDs and original timing
unchanged. MIDI and PCM before the affected passage were checked. In the blind
original/candidate/saved-version comparison, Gemini chose the quieter candidate
but described it as firmer and heavier in the low end than the saved version.
The candidate's 40–250 Hz RMS was actually 1.90 dB lower over 148.7–152.8 seconds.
That explanation reverses the controlled change, so the audition was not
accepted as evidence for a correction. This is an unpublished arrangement
audition, not a verified transcription correction. **No candidate
has been saved to a project and no replacement PDF or MIDI export has been
published.** Findings and the candidate manifest remain in the same ignored
private output tree. Both saved editions remain at notes/score revision 2.
Reliable musical diagnosis remains unresolved; these trials do not justify an
automated score-correction feature. Any accepted edit needs corroboration and a
new comparison.

AI Studio displayed “No API key selected”; billing status was not verified.
Drive permission was declined, and the UI used Temporary Chat. Its notice says
uploaded files may persist in Drive and logging still applies, so temporary
chat must not be described as zero retention. Google's
[data-use terms](https://ai.google.dev/gemini-api/terms) apply; unpaid and
billing-enabled services have different data-use rules. Private audio, account
details and response captures are not included in source control.

The three user-supplied skills (`gemini-api-dev`, `gemini-live-api-dev`, and
`gemini-omni-flash-api`) were installed as exact copies of the archive's nine
skill/reference/script files. Archive SHA-256:
`25a842b6a8de37d8a75eed89fb427b4bd595dac71f5b7a529dbe4f84b3e1bab5`.
They provide integration guidance, not model access or demonstrated musical
accuracy. No Gemini SDK dependency was added to the application environment.

## Verified compatibility and licenses

The inspected host is an Apple Silicon Mac (`arm64`, `Mac17,3`) with 24 GiB
unified memory, macOS 27.0.1 and about 486 GiB free disk at inspection. No audio
understanding runtime was installed in the transcription `.venv`; keep this
review environment separate.

- [mlx-audio 0.6.2](https://github.com/Blaizzy/mlx-audio) is MIT licensed and
  requires Python 3.10+, MLX 0.32.4 and Transformers 5.14+.
- The [MOSS-Music adapter](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/stt/models/moss_music/README.md)
  accepts audio and a text prompt for music understanding.
- The selected [MLX community conversion](https://huggingface.co/mlx-community/MOSS-Music-8B-Thinking-4bit)
  and [original model](https://huggingface.co/OpenMOSS-Team/MOSS-Music-8B-Thinking)
  declare Apache 2.0 model licensing. The conversion is not an official OpenMOSS release.
- Pinned weight revision: `b14123a419b6254b92d9e55b8a5b6f7285e05c70`.
  Its safetensors file is 6,017,835,521 bytes, plus tokenizer/configuration files.
  This file size is not peak memory or a performance measurement.

The adapter's 16 kHz frontend encodes the supplied audio with time markers.
For multiple clips, a complete ChatML prompt needs one audio placeholder per
clip. The runner builds those placeholders in A/B/C order. It limits each clip
to 30 seconds, total input to 60 seconds, and cases to 64. Start with short,
matched passages rather than comparing two entire tracks in one prompt.

## Run

Install separately from the existing transcription environment (Python 3.11 on
macOS arm64; the recorded dependency lock is platform-specific):

```sh
python3.11 -m venv .venv-listening
.venv-listening/bin/python -m pip install -r backend/requirements-listening-macos-arm64-py311.lock.txt
.venv-listening/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    'mlx-community/MOSS-Music-8B-Thinking-4bit',
    revision='b14123a419b6254b92d9e55b8a5b6f7285e05c70',
    local_dir='.runtime/listening/models/MOSS-Music-8B-Thinking-4bit',
    allow_patterns=['*.safetensors', '*.json', '*.txt', '*.jinja', 'README.md'],
)
PY
```

Only installation/model download needs internet. Audio inference uses local
files. With the isolated environment and pinned model installed/downloaded:

```sh
.venv-listening/bin/python scripts/listen_audio_compare.py \
  --model /absolute/path/to/pinned/local/model \
  --manifest /absolute/path/to/cases.json \
  --output /absolute/path/to/review.private.json \
  --max-tokens 1536 --timeout 600 --memory-gib 16
```

If thinking exhausts the token budget without a final answer, an optional
`--no-thinking` flag inserts the exact empty thinking block that the pinned
checkpoint's `chat_template.jinja` emits for `enable_thinking=False`. This is a
prompt mode, not a musical-quality improvement claim. Evaluate it with the same
blind audio controls. The default remains unchanged. Any generation reaching
its token limit is marked incomplete, even if some final text was produced.

Optional sampling flags are `--temperature`, `--top-p`, `--top-k` and `--seed`.
Defaults remain greedy (`0`, `1`, `0`, no seed). A reproducible experimental
control can use `--temperature .6 --top-p .95 --top-k 20 --seed 0`, following
[the Qwen3 backbone's thinking-mode guidance](https://huggingface.co/Qwen/Qwen3-8B#best-practices).
That guidance is not evidence that the MOSS conversion correctly judges musical
differences. Record the settings and evaluate the same blind controls. A seed
does not guarantee identical output across different software or hardware.

Manifest example (the two WAV files must already exist):

```json
{
  "cases": [{
    "id": "blind-01",
    "audio": ["/absolute/clip-a.wav", "/absolute/clip-b.wav"],
    "prompt": "Compare the melodic contour and attack pattern. Describe only differences you can identify from the audio, and state uncertainty."
  }]
}
```

Each case runs in a fresh process, with Hub offline/telemetry-disabled settings,
a hard process timeout, a 256 MiB MLX cache limit and a configurable MLX
allocation limit. The allocation limit covers MLX, not every byte of Python or
operating-system memory. The output records input hashes and durations, model
file hashes, runtime versions, token counts, elapsed time, peak process RSS and
MLX allocation. The runtime removes complete tagged thinking blocks; the runner
rejects empty responses and remaining incomplete thinking tags. Untagged
reasoning-like prose cannot be reliably distinguished automatically from answer
text, so responses need review before being presented. Incomplete or failed cases
are explicitly recorded; a failed run is never replaced with an invented review.
Timeout, keyboard interruption and parent SIGTERM terminate and reap the active
case's process group. Canceling an active run preserves previously finished
responses, records the active case as canceled, and marks remaining cases as
not run. It does not automatically resume or start another case.

Use blind controls before trusting musical suggestions: identical audio, known
rhythm changes, known pitch changes, and changed clip order. Withhold expected
answers and note data from the prompt. Repeatedly contradictory or invented
details disqualify a proposed correction. Any accepted score edit must retain
source provenance, regenerate the audio, and be compared again.

## Tests

```sh
.venv/bin/python -m pytest scripts/test_listen_audio_compare.py -q
```

Twenty-one generated-PCM contract tests verify ordered multiple-audio prompts,
duration and truncation checks, hashes, local-only input paths, unique IDs,
control-token rejection, the optional checkpoint prompt suffix, incomplete
generation handling, process-group cleanup, canceled-run preservation, sampling
flags and bounds, and removal of tagged reasoning blocks. These tests do not
establish the model's musical ability. Real inference results belong in private
per-run output files, including failures and known limitations.

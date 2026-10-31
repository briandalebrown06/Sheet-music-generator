# Dependency and model review

Reviewed against upstream documentation on **2026-10-27**. This records selection rationale; the manifests and lockfiles are the source of truth for installed versions. A package resolving successfully does not prove that model inference works on a given platform.

## Compatibility decisions

| Component | Reviewed choice | Reason and constraints |
| --- | --- | --- |
| Frontend | React 19, TypeScript, Vite | No frontend existed to preserve. React's current documented release is 19.3.0. Vite 8.3.5 is current; Vite 7.3 also receives important fixes. Use a maintained version and lock the resolved dependencies. |
| JavaScript runtime | Node 24 LTS or Node 22 >=22.12 | Vite requires Node 20.19+ or 22.12+; Node 20 is already EOL. Node 22 and 24 are LTS. |
| Python | Python 3.11 for the analysis environment | Basic Pitch 0.4.0 declares support through 3.11. Python 3.11 receives security fixes through October 2027. The host's Python 3.15 is not an appropriate Basic Pitch target. |
| Transcription | Spotify Basic Pitch 0.4.0 behind an adapter | Real note-event inference, best suited to one instrument at a time. Keep this optional and outside the API process so model installation/runtime failures do not prevent uploads or project management. |
| Notation | music21 9.9.1 compatibility baseline | Requires Python >=3.10 and NumPy >=1.26.4. It constructs scores and exports MusicXML/MIDI. Pinning a 9.x version avoids silently inheriting current music21 10's changed requirements. |
| Browser score | OpenSheetMusicDisplay 2.2.1 installed | Loads MusicXML and renders SVG in a DOM container with TypeScript declarations. It is a renderer; note editing and playback need application-owned state and controls. |
| Audio normalization | System FFmpeg | Decode to bounded-duration, mono, 22,050 Hz PCM before inference; select audio explicitly and impose size/time limits. Call an argument list, without a shell, and limit input protocols. |

Primary sources: [React versions](https://react.dev/versions), [Vite supported versions](https://vite.dev/releases), [Vite requirements](https://vite.dev/guide/), [Node release status](https://nodejs.org/en/about/previous-releases), [Python lifecycle](https://devguide.python.org/versions/), [Basic Pitch 0.4.0 metadata](https://github.com/spotify/basic-pitch/blob/v0.4.0/pyproject.toml), [music21 9.9.1 metadata](https://github.com/cuthbertLab/music21/blob/v9.9.1/pyproject.toml), [music21 export API](https://music21.org/music21docs/usersGuide/usersGuide_08_installingMusicXML.html), [OSMD 2.2.1 metadata](https://raw.githubusercontent.com/opensheetmusicdisplay/opensheetmusicdisplay/2.2.1/package.json), [OSMD usage](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay/wiki/Getting-started), [FFmpeg options](https://ffmpeg.org/ffmpeg.html).

### Basic Pitch runtime caveat

Basic Pitch's release README explicitly limits its claimed Apple M1 support to Python 3.10, which reached EOL on October 1, 2026. Do not describe Python 3.11 on Apple Silicon as upstream-certified. A native ONNX installation needs an actual inference smoke test; otherwise use a compatible isolated Linux worker environment. Avoid installing the worker into the system Python.

The package's platform markers pull TensorFlow `>=2.4.1,<2.15.1` on non-Darwin Python >=3.11, and CoreML on Darwin. An explicit ONNX model path can select ONNX inference, but adding the `onnx` extra does **not** remove those other declared dependencies. A NumPy 1.26.4 constraint is a candidate intersection with music21 9.9.1 and TensorFlow 2.15; resolver and runtime checks remain necessary. Do not solve incompatible installations by silently disabling dependency checks. See the [release README](https://github.com/spotify/basic-pitch/blob/v0.4.0/README.md), [platform markers](https://github.com/spotify/basic-pitch/blob/v0.4.0/pyproject.toml), and [runtime loader](https://github.com/spotify/basic-pitch/blob/v0.4.0/basic_pitch/inference.py).

## Licenses: code and weights checked separately

| Asset | Evidence and conclusion |
| --- | --- |
| Basic Pitch source code | Spotify's release [LICENSE](https://github.com/spotify/basic-pitch/blob/v0.4.0/LICENSE) is Apache-2.0, with a separate [NOTICE](https://github.com/spotify/basic-pitch/blob/v0.4.0/NOTICE). Preserve both when redistributing relevant artifacts. |
| Basic Pitch ICASSP 2022 model weights | Independently inspected the official [model directory](https://github.com/spotify/basic-pitch/tree/v0.4.0/basic_pitch/saved_models/icassp_2022) and [package manifest](https://github.com/spotify/basic-pitch/blob/v0.4.0/MANIFEST.in): TensorFlow, CoreML, TFLite and ONNX weights are shipped in the Apache-2.0 repository. Spotify explicitly describes [open-sourcing the model](https://engineering.atspotify.com/2022/6/meet-basic-pitch). This supports using the official bundled weights under the repository's Apache-2.0 terms. No distinct weight-specific license was found; this conclusion uses repository scope and Spotify's statement, not a third-party model mirror's label. Keep original provenance and notices; reassess any replacement weights separately. |
| music21 code | [BSD-3-Clause](https://github.com/cuthbertLab/music21/blob/v9.9.1/music21/license.txt). Corpus scores and third-party content have separate copyrights; the app must not present those scores as inferred results. |
| OSMD | [BSD-3-Clause](https://raw.githubusercontent.com/opensheetmusicdisplay/opensheetmusicdisplay/2.2.1/LICENSE). Its transitive fonts/libraries retain their own notices. |
| React / Vite / TypeScript | [MIT](https://github.com/facebook/react/blob/v19.3.0/LICENSE) / [MIT](https://github.com/vitejs/vite/blob/v8.3.5/packages/vite/package.json) / [Apache-2.0](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt). |
| FFmpeg binary | [LGPL-2.1-or-later by default; GPL if applicable components are enabled](https://ffmpeg.org/legal.html). Inspect the actual binary with `ffmpeg -version` and `ffmpeg -L`; a system installation's build flags determine its license. This repository does not vendor an FFmpeg binary. |

No training datasets or upstream example recordings are included as application content. Basic Pitch's NOTICE identifies Vocadito test recordings as CC-BY-4.0; that is separate from code and model licensing.

## Scope and later stages

Basic Pitch supplies estimated pitches, timing and amplitudes, not a professionally engraved score. Tempo, meter, quantization, rests, ties and voice allocation require explicit notation decisions and user correction. Canonical MusicXML/MIDI export uses music21. The review workspace adds a verified Verovio/svglib/ReportLab PDF engraving path; its pinned versions, separate code/font licenses and visual checks are documented in [PDF_EXPORT.md](PDF_EXPORT.md). Mixed recordings now use the replaceable Open-Unmix UMX-HQ adapter. Its code and author-published checkpoints were reviewed separately (both MIT), and the actual Python/Torch environment passed CPU inference. This is an older reference implementation, not a claim of active upstream maintenance. See [SEPARATION_ENGINE.md](SEPARATION_ENGINE.md) for the current Demucs-compatible alternatives, license findings, pinned versions, checksum-verified installer and compute requirements. Vocal/bass parts use librosa 0.11 pYIN (ISC, no learned weights); other pitched stems remain experimental Basic Pitch transcription. Full processing and evaluation details are in [MIXED_RECORDINGS.md](MIXED_RECORDINGS.md).

## Local validation update

The implementation was subsequently validated on this Mac with Python 3.11.18 and explicit ONNX Runtime 1.31.1. Actual inference and MusicXML/MIDI round trips passed; see [VERIFICATION.md](VERIFICATION.md) for the fixture, versions, and limits of that evidence. This does not change the upstream Apple Silicon support statement.

## Canonical notation update

The notation implementation retains the tested music21 9.9.1 environment and adds OpenSheetMusicDisplay 2.2.1, released October 12, 2026. Its official release documents fixes for multiple voices, ties, and repeated zoom/resize rendering. The frontend loads its published browser build and TypeScript declarations; no model weights are involved in this renderer. The installed package is pinned in the frontend manifest and lockfile. Code is BSD-3-Clause, independently checked against the release LICENSE; transitive fonts/libraries retain their notices. See [the official release](https://opensheetmusicdisplay.org/blog/opensheetmusicdisplay-2-2-1-released-66-bug-fixes-smarter-dynamics-cleaner-slurs/) and [the notation model and controls](NOTATION.md).

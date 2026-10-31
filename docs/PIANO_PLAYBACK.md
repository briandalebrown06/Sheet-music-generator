# Piano review playback

The review workspace offers **Sampled piano** and **Simple tones** for the edited performance, raw detections, and quantized score. Original recording playback is unchanged. The sampled option improves the available timbre using recorded piano attacks and natural decay; no audible comparison or human-pianist validation is claimed.

Samples load only after the user presses Play. Only sample roots needed by the selected notes/loop are fetched, from the application's own `/samples/salamander/` assets. There is no runtime CDN or upload of private audio. Pause, seek, project/note changes, sound changes, and unmount cancel pending loading and stop scheduled voices. Failures are visible with a retry action and an explicit Simple tones selection; playback never silently substitutes a sound.

The two sounds share the audio-clock scheduler and canonical score mapping. Sample pitch uses a nearest recorded key and a deterministic playback-rate ratio. Saved timing, velocity, source IDs, cursor, loops and cue exclusion remain unchanged. Seeking into a held note resumes the matching sample offset instead of replaying its attack. A recorded natural tail may finish before a very long held event; the sample is not looped into an artificial sustain, and the saved note duration/cursor/export remain unchanged. Velocity scales volume in this one-layer MP3 subset; this is not the full sixteen-layer SFZ piano, acoustic modeling, or a performed interpretation. No timing randomization, automatic accompaniment, or fabricated dynamics are added.

Thirty MP3 assets total **2,012,677 bytes**. Cached decoded audio is capped at **192 MiB**, unused sample roots are released when the required set changes, and decoding is serialized. Individual downloads are limited to 512 KiB with a 15-second request deadline; decoded samples must be 1–2 channels and at most 40 seconds. The complete set has about 423.66 seconds of recorded audio, approximately 155.15 MiB as stereo 48 kHz float PCM; normal narrower note ranges load a subset. Memory or decode failures leave a clear retry/fallback choice.

## Source and license

The samples were recorded by **Alexander Holm**, from the Salamander Grand Piano (Yamaha C5), under **Creative Commons Attribution 3.0 Unported**. This is a sample-content license, separate from the application's code dependencies. The specific files are unmodified MP3 bytes from [Tonejs/audio at pinned commit efd8296360f9526e379bfbe5c1698ff54d6a1d34](https://github.com/Tonejs/audio/tree/efd8296360f9526e379bfbe5c1698ff54d6a1d34/salamander). Its README expressly names the author and [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). The [original instrument repository](https://github.com/sfzinstruments/SalamanderGrandPiano) documents the full library. Attribution and the upstream notice ship with the app at `frontend/public/samples/salamander/` and are linked beside the player.

Only these 30 public sample files are excepted from the repository's audio ignore rules; private uploads remain ignored. Download/checksum reproduction:

```sh
python3 scripts/download_piano_samples.py --verify-only
# Restore missing public assets from the pinned source, verifying every SHA-256:
python3 scripts/download_piano_samples.py
```

The allowlist, sizes and SHA-256 digests are in `scripts/piano_samples.manifest.json`. The script reads no project audio. Installed MuseScore's MS Basic.sf3 was inspected as an alternative but is not redistributed or used: this browser sample bank avoids a new server synthesis dependency and a large SoundFont transfer.

## Verification

```sh
node --experimental-strip-types --test scripts/test_playback.ts
cd frontend
pnpm build
```

Generated buffers test exact scheduler time, rate, velocity gain, seeks, loops, stop cancellation, failed-load retry, canceled late decode, same-origin loading, cache reuse and resource caps. These are consistency checks, not device listening or musical-accuracy tests. Browser checks must be reported separately. Web Audio uses the documented [AudioBufferSourceNode](https://developer.mozilla.org/en-US/docs/Web/API/AudioBufferSourceNode) mechanism; buffers are reused while a new source node is created for each attack.

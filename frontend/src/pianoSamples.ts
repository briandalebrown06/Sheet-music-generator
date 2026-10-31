/** Local Salamander piano review samples. No project audio leaves this origin. */
export type PianoSample = { name: string; pitch: number };
export const PIANO_SAMPLE_ROOTS: PianoSample[] = [
  { name: "A0", pitch: 21 },
  ...Array.from({ length: 7 }, (_, octave) =>
    [
      ["C", 0],
      ["Ds", 3],
      ["Fs", 6],
      ["A", 9],
    ].map(([name, pitch]) => ({
      name: `${name}${octave + 1}`,
      pitch: 12 * (octave + 2) + Number(pitch),
    })),
  ).flat(),
  { name: "C8", pitch: 108 },
];
const MAX_ENCODED_BYTES = 512 * 1024;
const MAX_DECODED_BYTES = 192 * 1024 * 1024;
const SAMPLE_BASE = "/samples/salamander/";

export function pianoSampleForPitch(pitch: number): PianoSample {
  if (!Number.isInteger(pitch) || pitch < 0 || pitch > 127)
    throw new Error("Piano playback requires a MIDI pitch from 0 to 127.");
  return PIANO_SAMPLE_ROOTS.reduce((best, sample) =>
    Math.abs(sample.pitch - pitch) < Math.abs(best.pitch - pitch)
      ? sample
      : best,
  );
}

function abortError() {
  return new DOMException("Piano sample loading was canceled.", "AbortError");
}

async function boundedBytes(response: Response): Promise<ArrayBuffer> {
  const declared = Number(response.headers.get("content-length") ?? 0);
  if (declared > MAX_ENCODED_BYTES)
    throw new Error("A piano sample exceeds the download limit.");
  if (!response.body) {
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > MAX_ENCODED_BYTES)
      throw new Error("A piano sample exceeds the download limit.");
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_ENCODED_BYTES) {
        await reader.cancel();
        throw new Error("A piano sample exceeds the download limit.");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const data = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    data.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return data.buffer;
}

/** Per-player cache: at most 30 files, 192 MiB cached PCM, one decode in flight. */
export class PianoSampleBank {
  private buffers = new Map<number, AudioBuffer>();
  private generation = 0;
  private decodeQueue: Promise<void> = Promise.resolve();
  readonly context: BaseAudioContext;
  private fetcher: typeof fetch;

  constructor(
    context: BaseAudioContext,
    fetcher: typeof fetch = (...args) => globalThis.fetch(...args),
  ) {
    this.context = context;
    // Native Window.fetch rejects an arbitrary receiver such as this bank.
    this.fetcher = (...args) => fetcher(...args);
  }

  get decodedBytes() {
    return [...this.buffers.values()].reduce(
      (total, buffer) => total + buffer.length * buffer.numberOfChannels * 4,
      0,
    );
  }

  async prepare(
    pitches: number[],
    signal?: AbortSignal,
    progress?: (loaded: number, total: number) => void,
  ): Promise<void> {
    const generation = ++this.generation;
    const required = new Map(
      pitches.map((pitch) => {
        const root = pianoSampleForPitch(pitch);
        return [root.pitch, root] as const;
      }),
    );
    // A new piece does not retain unused buffers from a previous project.
    for (const pitch of this.buffers.keys())
      if (!required.has(pitch)) this.buffers.delete(pitch);
    const check = () => {
      if (signal?.aborted || generation !== this.generation) throw abortError();
    };
    let loaded = 0;
    check();
    progress?.(loaded, required.size);
    for (const root of [...required.values()].sort(
      (a, b) => a.pitch - b.pitch,
    )) {
      check();
      if (!this.buffers.has(root.pitch)) {
        const request = new AbortController();
        const cancel = () => request.abort();
        signal?.addEventListener("abort", cancel, { once: true });
        const deadline = setTimeout(cancel, 15000);
        try {
          const response = await this.fetcher(
            `${SAMPLE_BASE}${root.name}.mp3`,
            {
              signal: request.signal,
              credentials: "same-origin",
              mode: "same-origin",
            },
          );
          if (!response.ok)
            throw new Error(`Sample ${root.name} is unavailable.`);
          const bytes = await boundedBytes(response);
          check();
          const previous = this.decodeQueue;
          let release!: () => void;
          this.decodeQueue = new Promise<void>((resolve) => {
            release = resolve;
          });
          let buffer: AudioBuffer;
          try {
            await previous;
            check();
            buffer = await this.context.decodeAudioData(bytes);
            check();
          } finally {
            release();
          }
          const size = buffer.length * buffer.numberOfChannels * 4;
          if (
            buffer.duration <= 0 ||
            buffer.duration > 40 ||
            buffer.numberOfChannels < 1 ||
            buffer.numberOfChannels > 2 ||
            !Number.isFinite(size) ||
            size + this.decodedBytes > MAX_DECODED_BYTES
          )
            throw new Error(
              "The piano samples exceed the decoded memory limit.",
            );
          this.buffers.set(root.pitch, buffer);
        } catch (error) {
          check();
          const detail =
            error instanceof Error ? error.message : "Decode failed.";
          throw new Error(
            `Sampled piano could not load. Retry, or choose Simple tones. ${detail}`,
          );
        } finally {
          clearTimeout(deadline);
          signal?.removeEventListener("abort", cancel);
        }
      }
      progress?.(++loaded, required.size);
    }
  }

  sample(pitch: number) {
    const root = pianoSampleForPitch(pitch);
    const buffer = this.buffers.get(root.pitch);
    if (!buffer)
      throw new Error("Piano samples are not ready. Retry playback.");
    return {
      buffer,
      rootPitch: root.pitch,
      rate: 2 ** ((pitch - root.pitch) / 12),
    };
  }

  clear() {
    this.generation++;
    this.buffers.clear();
  }
}

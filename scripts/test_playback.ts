/** Deterministic playback math and scheduler checks; no claim of audio-device testing. */
import assert from "node:assert/strict";
import test from "node:test";
import {
  activeNoteIds,
  maximumPolyphony,
  midiFrequency,
  performancePlaybackNotes,
  playbackPosition,
  ReviewSynth,
  scorePlaybackNotes,
  validLoop,
} from "../frontend/src/playback.ts";

const rational = (numerator: number, denominator = 1) => ({
  numerator,
  denominator,
});
const event = (overrides = {}) => ({
  id: "fixture-note",
  kind: "note",
  offset: rational(0),
  duration: rational(1),
  pitches: [60],
  source_note_ids: ["source-a"],
  tie: null,
  velocity: 90,
  ...overrides,
});
const canonical = (measures, bpm = 120, origin = 0) => ({
  settings: {},
  assumptions: [],
  warnings: [],
  tempo: { bpm },
  origin_seconds: origin,
  parts: [{ id: "part-1", staves: [{ id: "staff-1", measures }] }],
});
const measure = (start, events) => ({
  start: rational(start),
  voices: [{ id: "1", events }],
});
const sounding = (start, end, pitch = 60) => ({
  id: `${pitch}:${start}`,
  sourceIds: [`source-${pitch}`],
  start,
  end,
  pitch,
  velocity: 100,
});

test("performance seconds, velocity and stable IDs are preserved", () => {
  const input = [
    { id: "stable", start: 0.133, end: 0.987, pitch: 64, velocity: null },
  ];
  const before = JSON.stringify(input);
  const output = performancePlaybackNotes(input);
  assert.deepEqual(output, [
    {
      id: "stable",
      sourceIds: ["stable"],
      start: 0.133,
      end: 0.987,
      pitch: 64,
      velocity: 64,
    },
  ]);
  assert.equal(JSON.stringify(input), before);
});

test("canonical pickup mapping and barline ties become one sustained tone", () => {
  const score = canonical(
    [
      measure(0, [event({ duration: rational(1), tie: "start" })]),
      measure(1, [
        event({ id: "fragment-2", duration: rational(2), tie: "stop" }),
      ]),
    ],
    120,
    -0.5,
  );
  const notes = scorePlaybackNotes(score);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].start, -0.5);
  assert.equal(notes[0].end, 1);
  assert.deepEqual(notes[0].sourceIds, ["source-a"]);
  assert.deepEqual(activeNoteIds(notes, 0.75), ["source-a"]);
  assert.deepEqual(activeNoteIds(notes, 1), []);
});

test("triplet/chord score playback uses rational beats and effective tempo", () => {
  const notes = scorePlaybackNotes(
    canonical(
      [
        measure(0, [
          event({
            kind: "chord",
            offset: rational(1, 3),
            duration: rational(2, 3),
            pitches: [60, 64, 67],
            source_note_ids: ["a", "b", "c"],
          }),
        ]),
      ],
      90,
      0.75,
    ),
  );
  assert.equal(notes.length, 3);
  assert.equal(notes[0].start, 0.75 + 2 / 9);
  assert.ok(Math.abs(notes[0].end - (0.75 + 2 / 3)) < 1e-10);
  assert.deepEqual(
    notes.map((note) => note.pitch),
    [60, 64, 67],
  );
  assert.deepEqual(activeNoteIds(notes, 1), ["a", "b", "c"]);
});

test("rearticulated notes are not merged without ties", () => {
  const notes = scorePlaybackNotes(
    canonical([
      measure(0, [
        event({ id: "a", source_note_ids: ["a"] }),
        event({ id: "b", source_note_ids: ["b"], offset: rational(1) }),
      ]),
    ]),
  );
  assert.equal(notes.length, 2);
  assert.equal(notes[0].end, notes[1].start);
});

test("chord playback preserves per-pitch balance and legacy fallback", () => {
  const notes = scorePlaybackNotes(
    canonical([
      measure(0, [
        event({
          kind: "chord",
          pitches: [62, 66, 69],
          pitch_velocities: { "62": 39, "66": 104 },
          velocity: 77,
        }),
      ]),
    ]),
  );
  assert.deepEqual(
    notes.map((note) => [note.pitch, note.velocity]),
    [
      [62, 39],
      [66, 104],
      [69, 77],
    ],
  );
});

test("tied chord playback retains each original attack dynamic", () => {
  const notes = scorePlaybackNotes(
    canonical(
      [
        measure(0, [
          event({
            kind: "chord",
            pitches: [60, 64],
            tie: "start",
            pitch_velocities: { "60": 43, "64": 99 },
          }),
        ]),
        measure(1, [
          event({
            id: "continuation",
            kind: "chord",
            pitches: [60, 64],
            tie: "stop",
            pitch_velocities: { "60": 120, "64": 20 },
          }),
        ]),
      ],
      60,
    ),
  );
  assert.deepEqual(
    notes.map((note) => [note.pitch, note.velocity, note.start, note.end]),
    [
      [60, 43, 0, 2],
      [64, 99, 0, 2],
    ],
  );
});

test("loop cursor shares exact first-cycle and subsequent-cycle mapping", () => {
  const loop = { start: 1, end: 2 };
  assert.deepEqual(playbackPosition(1.5, 0.25, 10, loop), {
    position: 1.75,
    cycle: 0,
    finished: false,
  });
  assert.deepEqual(playbackPosition(1.5, 0.5, 10, loop), {
    position: 1,
    cycle: 1,
    finished: false,
  });
  assert.deepEqual(playbackPosition(1.5, 2.75, 10, loop), {
    position: 1.25,
    cycle: 3,
    finished: false,
  });
  assert.deepEqual(playbackPosition(0, 0, 10, loop), {
    position: 1,
    cycle: 0,
    finished: false,
  });
  assert.deepEqual(playbackPosition(9, 5, 10, null), {
    position: 10,
    cycle: 0,
    finished: true,
  });
});

test("invalid loops and excessive overlapping tones are detected", () => {
  assert.equal(validLoop(0.5, 0.6, 2), true);
  assert.equal(validLoop(0.5, 0.51, 2), false);
  assert.equal(validLoop(2, 3, 2), false);
  assert.equal(validLoop(NaN, 1, 2), false);
  assert.equal(
    maximumPolyphony([sounding(0, 1), sounding(1, 2), sounding(1.5, 3)]),
    2,
  );
  assert.equal(midiFrequency(69), 440);
  assert.equal(midiFrequency(81), 880);
});

class TestAudioContext {
  currentTime = 10;
  destination = {};
  oscillators = [];
  createOscillator() {
    const oscillator = {
      frequency: { value: 0 },
      type: "",
      starts: [],
      stops: [],
      disconnected: false,
      connect() {},
      disconnect() {
        this.disconnected = true;
      },
      start(time) {
        this.starts.push(time);
      },
      stop(time) {
        this.stops.push(time);
      },
    };
    this.oscillators.push(oscillator);
    return oscillator;
  }
  createGain() {
    return {
      gain: { setValueAtTime() {}, linearRampToValueAtTime() {} },
      connect() {},
      disconnect() {},
    };
  }
}

test("synth schedules from AudioContext time and immediately cancels pending tones", () => {
  const context = new TestAudioContext();
  const synth = new ReviewSynth(context);
  try {
    synth.play([sounding(0, 1, 69)], 0.5, 2, null);
    assert.equal(context.oscillators.length, 1);
    const oscillator = context.oscillators[0];
    assert.equal(oscillator.frequency.value, 440);
    assert.equal(oscillator.starts[0], 10.025);
    assert.equal(oscillator.stops[0], 10.525);
    context.currentTime = 10.225;
    assert.ok(Math.abs(synth.position().position - 0.7) < 1e-10);
    synth.stop();
    assert.equal(oscillator.stops.at(-1), undefined);
    assert.equal(oscillator.disconnected, true);
  } finally {
    synth.stop();
  }
});

test("loop synthesis clips sustained notes at the loop end and schedules the next cycle", () => {
  const context = new TestAudioContext();
  const synth = new ReviewSynth(context);
  try {
    synth.play([sounding(0, 1)], 0.2, 1, { start: 0.2, end: 0.3 });
    assert.ok(context.oscillators.length >= 2);
    const [first, next] = context.oscillators;
    assert.ok(Math.abs(first.stops[0] - 10.125) < 1e-10);
    assert.ok(Math.abs(next.starts[0] - first.stops[0]) < 1e-10);
  } finally {
    synth.stop();
  }
});

test("explicit pedal extends sounding playback after key release without changing attacks or other parts", () => {
  const score = canonical([
    measure(0, [
      event({ id: "held", duration: rational(1), tie: "start" }),
      event({
        id: "tie",
        offset: rational(1),
        duration: rational(1),
        tie: "stop",
      }),
      event({
        id: "reattack",
        offset: rational(3),
        duration: rational(1, 2),
        source_note_ids: ["new-attack"],
      }),
    ]),
  ]);
  score.pedal_spans = [
    { part_id: "part-1", start: rational(0), end: rational(4) },
  ];
  score.parts.push({
    id: "other",
    staves: [
      {
        id: "s",
        measures: [measure(0, [event({ id: "unpedaled", pitches: [48] })])],
      },
    ],
  });
  const notes = scorePlaybackNotes(score);
  const tied = notes.find((n) => n.id === "held");
  assert.equal(tied.start, 0);
  assert.equal(tied.end, 2);
  assert.equal(notes.find((n) => n.id === "reattack").start, 1.5);
  assert.equal(notes.find((n) => n.id === "reattack").end, 2);
  assert.equal(notes.find((n) => n.id === "unpedaled").end, 0.5);
});

test("vocal cues stay visible in canonical notation but do not sound in pianist-only score playback", () => {
  const score = canonical([measure(0, [event()])]);
  score.parts.push({
    id: "vocal-cue",
    is_cue: true,
    staves: [
      {
        id: "cue",
        measures: [measure(0, [event({ id: "cue", pitches: [84] })])],
      },
    ],
  });
  assert.deepEqual(
    scorePlaybackNotes(score).map((n) => n.pitch),
    [60],
  );
  assert.equal(
    score.parts[1].staves[0].measures[0].voices[0].events[0].pitches[0],
    84,
  );
});

// Explicit generated buffers test scheduling and loading; they are not piano recordings.
const { PianoSampleBank, pianoSampleForPitch, PIANO_SAMPLE_ROOTS } =
  await import("../frontend/src/pianoSamples.ts");
class SampleAudioContext extends TestAudioContext {
  sources = [];
  decoded = 0;
  gains = [];
  async decodeAudioData() {
    this.decoded++;
    return { duration: 8, length: 8 * 44100, numberOfChannels: 2 };
  }
  createBufferSource() {
    const source = {
      buffer: null,
      playbackRate: { value: 1 },
      starts: [],
      stops: [],
      disconnected: false,
      connect() {},
      disconnect() {
        this.disconnected = true;
      },
      start(time, offset) {
        this.starts.push({ time, offset });
      },
      stop(time) {
        this.stops.push(time);
      },
    };
    this.sources.push(source);
    return source;
  }
  createGain() {
    const values = [];
    this.gains.push(values);
    return {
      gain: {
        setValueAtTime(value, time) {
          values.push({ value, time });
        },
        linearRampToValueAtTime(value, time) {
          values.push({ value, time });
        },
      },
      connect() {},
      disconnect() {},
    };
  }
}
const sampleResponse = () => new Response(new Uint8Array([1, 2, 3]));

test("piano sample roots cover the keyboard and preserve neighboring pitch by rate", () => {
  assert.equal(PIANO_SAMPLE_ROOTS.length, 30);
  assert.equal(pianoSampleForPitch(21).name, "A0");
  assert.equal(pianoSampleForPitch(60).name, "C4");
  assert.equal(pianoSampleForPitch(61).pitch, 60);
  assert.equal(pianoSampleForPitch(62).pitch, 63);
  assert.equal(pianoSampleForPitch(108).name, "C8");
  assert.equal(pianoSampleForPitch(0).pitch, 21);
  assert.equal(pianoSampleForPitch(127).pitch, 108);
  assert.throws(() => pianoSampleForPitch(NaN));
  assert.throws(() => pianoSampleForPitch(60.1));
});

test("sample loading uses only needed roots, same-origin URLs, and reusable bounded cache", async () => {
  const context = new SampleAudioContext();
  const requests = [];
  const bank = new PianoSampleBank(context, async (url, options) => {
    requests.push({ url, options });
    return sampleResponse();
  });
  await bank.prepare([60, 61, 60, 62]);
  assert.deepEqual(
    requests.map((r) => r.url),
    ["/samples/salamander/C4.mp3", "/samples/salamander/Ds4.mp3"],
  );
  assert.ok(
    requests.every(
      (r) =>
        r.options.mode === "same-origin" &&
        r.options.credentials === "same-origin",
    ),
  );
  assert.equal(bank.sample(61).rate, 2 ** (1 / 12));
  assert.equal(bank.decodedBytes, 2 * 8 * 44100 * 2 * 4);
  await bank.prepare([60]);
  assert.equal(requests.length, 2);
  assert.throws(() => bank.sample(62), /not ready/);
  bank.clear();
  assert.equal(bank.decodedBytes, 0);
});

test("native default fetch keeps its browser receiver and injected fetch is unbound", async () => {
  const original = globalThis.fetch;
  const receivers = [];
  globalThis.fetch = async function () {
    assert.equal(this, globalThis);
    receivers.push(this);
    return sampleResponse();
  };
  try {
    await new PianoSampleBank(new SampleAudioContext()).prepare([60]);
    const injected = async function () {
      assert.equal(this, undefined);
      return sampleResponse();
    };
    await new PianoSampleBank(new SampleAudioContext(), injected).prepare([60]);
    assert.equal(receivers.length, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("sample fetch receives an internal deadline signal and forwards user cancellation", async () => {
  const user = new AbortController();
  let requestSignal;
  const bank = new PianoSampleBank(
    new SampleAudioContext(),
    async (_url, options) => {
      requestSignal = options.signal;
      assert.notEqual(requestSignal, user.signal);
      return new Promise((_resolve, reject) => {
        requestSignal.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    },
  );
  const pending = bank.prepare([60], user.signal);
  user.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
  assert.equal(requestSignal.aborted, true);
  assert.equal(bank.decodedBytes, 0);
});

test("sample download deadline aborts its actual request and remains retryable", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let requestSignal;
  const bank = new PianoSampleBank(
    new SampleAudioContext(),
    async (_url, options) => {
      requestSignal = options.signal;
      return new Promise((_resolve, reject) => {
        requestSignal.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    },
  );
  const pending = bank.prepare([60]);
  const rejected = assert.rejects(pending, /Retry, or choose Simple tones/);
  t.mock.timers.tick(15000);
  await rejected;
  assert.equal(requestSignal.aborted, true);
  assert.equal(bank.decodedBytes, 0);
});

test("sampled playback preserves note clock, velocity, seek offset, and stop cancellation", async () => {
  const context = new SampleAudioContext();
  const bank = new PianoSampleBank(context, async () => sampleResponse());
  await bank.prepare([61]);
  const synth = new ReviewSynth(context);
  try {
    const notes = [sounding(0, 1, 61)];
    const before = JSON.stringify(notes);
    synth.play(notes, 0.5, 2, null, bank);
    assert.equal(context.oscillators.length, 0);
    assert.equal(context.sources.length, 1);
    const source = context.sources[0];
    assert.equal(source.playbackRate.value, 2 ** (1 / 12));
    assert.equal(source.starts[0].time, 10.025);
    assert.ok(Math.abs(source.starts[0].offset - 0.5 * 2 ** (1 / 12)) < 1e-10);
    assert.equal(source.stops[0], 10.525);
    assert.ok(
      context.gains[0].some(({ value }) => value === (0.1 * 100) / 127),
    );
    context.currentTime = 10.225;
    assert.ok(Math.abs(synth.position().position - 0.7) < 1e-10);
    assert.equal(JSON.stringify(notes), before);
    synth.stop();
    assert.equal(source.stops.at(-1), undefined);
    assert.equal(source.disconnected, true);
  } finally {
    synth.stop();
  }
});

test("sampled loop resumes the same sample segment without a fresh attack", async () => {
  const context = new SampleAudioContext();
  const bank = new PianoSampleBank(context, async () => sampleResponse());
  await bank.prepare([60]);
  const synth = new ReviewSynth(context);
  try {
    synth.play([sounding(0, 1)], 0.2, 1, { start: 0.2, end: 0.3 }, bank);
    assert.ok(context.sources.length >= 2);
    const [first, second] = context.sources;
    assert.ok(Math.abs(first.starts[0].offset - 0.2) < 1e-10);
    assert.ok(Math.abs(second.starts[0].offset - 0.2) < 1e-10);
    assert.ok(Math.abs(first.stops[0] - second.starts[0].time) < 1e-10);
  } finally {
    synth.stop();
  }
});

test("seeking past a recorded natural tail does not fabricate a new piano attack", async () => {
  const context = new SampleAudioContext();
  context.decodeAudioData = async () => ({
    duration: 0.2,
    length: 8820,
    numberOfChannels: 1,
  });
  const bank = new PianoSampleBank(context, async () => sampleResponse());
  await bank.prepare([60]);
  const synth = new ReviewSynth(context);
  try {
    synth.play([sounding(0, 2)], 1, 2, null, bank);
    assert.equal(context.sources.length, 0);
    assert.equal(synth.position().position, 1);
  } finally {
    synth.stop();
  }
});

test("sample load failure remains retryable rather than silently changing sound", async () => {
  const context = new SampleAudioContext();
  let available = false;
  const bank = new PianoSampleBank(context, async () =>
    available ? sampleResponse() : new Response("missing", { status: 404 }),
  );
  await assert.rejects(bank.prepare([60]), /Retry, or choose Simple tones/);
  assert.throws(() => bank.sample(60), /not ready/);
  assert.equal(context.oscillators.length, 0);
  available = true;
  await bank.prepare([60]);
  assert.equal(bank.sample(60).rootPitch, 60);
});

test("canceling a pending decode never publishes its late buffer", async () => {
  const context = new SampleAudioContext();
  let finish;
  context.decodeAudioData = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  const bank = new PianoSampleBank(context, async () => sampleResponse());
  const abort = new AbortController();
  const pending = bank.prepare([60], abort.signal);
  await new Promise((resolve) => setImmediate(resolve));
  abort.abort();
  finish({ duration: 8, length: 8 * 44100, numberOfChannels: 2 });
  await assert.rejects(pending, (error) => error.name === "AbortError");
  assert.equal(bank.decodedBytes, 0);
  assert.throws(() => bank.sample(60), /not ready/);
});

test("encoded and decoded sample budgets fail clearly before unlimited cache growth", async () => {
  const context = new SampleAudioContext();
  const oversized = new PianoSampleBank(
    context,
    async () => new Response(new Uint8Array(512 * 1024 + 1)),
  );
  await assert.rejects(oversized.prepare([60]), /download limit/);
  assert.equal(context.decoded, 0);
  context.decodeAudioData = async () => ({
    duration: 20,
    length: 30_000_000,
    numberOfChannels: 2,
  });
  const giant = new PianoSampleBank(context, async () => sampleResponse());
  await assert.rejects(giant.prepare([60]), /memory limit/);
  assert.equal(giant.decodedBytes, 0);
});

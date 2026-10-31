import type { CanonicalScore, NoteEvent } from "./api";
import type { PianoSampleBank } from "./pianoSamples";

export type PlaybackNote = {
  id: string;
  sourceIds: string[];
  pitch: number;
  start: number;
  end: number;
  velocity: number;
};
type Rational = { numerator: number; denominator: number };
type ScoreEvent = {
  id: string;
  kind: string;
  offset: Rational;
  duration: Rational;
  pitches: number[];
  source_note_ids: string[];
  tie: string | null;
  velocity?: number;
  pitch_velocities?: Record<string, number>;
};
type PlaybackScore = {
  tempo?: { bpm?: number };
  origin_seconds?: number;
  pedal_spans?: { part_id: string; start: Rational; end: Rational }[];
  parts?: {
    id: string;
    is_cue?: boolean;
    staves: {
      id: string;
      measures: {
        start: Rational;
        voices: { id: string; events: ScoreEvent[] }[];
      }[];
    }[];
  }[];
};
export type LoopRegion = { start: number; end: number };

export function rationalBeats(value: Rational): number {
  return value.numerator / value.denominator;
}

export function performancePlaybackNotes(notes: NoteEvent[]): PlaybackNote[] {
  return notes
    .filter(
      (note) =>
        [note.pitch, note.start, note.end].every(Number.isFinite) &&
        note.end > note.start &&
        note.pitch >= 0 &&
        note.pitch <= 127,
    )
    .map((note) => ({
      id: note.id,
      sourceIds: [note.id],
      pitch: note.pitch,
      start: note.start,
      end: note.end,
      velocity: note.velocity ?? 64,
    }))
    .sort((a, b) => a.start - b.start || a.pitch - b.pitch);
}

/** Canonical beats are mapped back onto the selected clip, never raw MIDI ticks. */
export function scorePlaybackNotes(
  score: CanonicalScore | null,
): PlaybackNote[] {
  if (!score) return [];
  const model = score as PlaybackScore;
  const bpm = model.tempo?.bpm;
  if (!bpm || !Number.isFinite(bpm) || bpm <= 0) return [];
  const secondsPerBeat = 60 / bpm;
  const origin = model.origin_seconds ?? 0;
  const notes = new Map<string, PlaybackNote>();
  const partForNote = new Map<PlaybackNote, string>();
  for (const part of model.parts ?? []) {
    if (part.is_cue) continue;
    for (const staff of part.staves) {
      for (const measure of staff.measures) {
        for (const voice of measure.voices) {
          for (const event of voice.events) {
            if (event.kind === "rest") continue;
            const startBeat =
              rationalBeats(measure.start) + rationalBeats(event.offset);
            const start = origin + startBeat * secondsPerBeat;
            const end = start + rationalBeats(event.duration) * secondsPerBeat;
            for (const pitch of event.pitches) {
              const key = [
                part.id,
                staff.id,
                voice.id,
                event.source_note_ids.join(","),
                pitch,
              ].join(":");
              const prior = notes.get(key);
              if (
                prior &&
                (event.tie === "continue" || event.tie === "stop") &&
                Math.abs(prior.end - start) < 1e-6
              ) {
                prior.end = end;
              } else {
                // A separate onset gets a separate key even if imported data
                // repeats its source mapping. Ties alone extend a sounding note.
                const value: PlaybackNote = {
                  id: event.id,
                  sourceIds: event.source_note_ids,
                  pitch,
                  start,
                  end,
                  velocity:
                    event.pitch_velocities?.[String(pitch)] ??
                    event.velocity ??
                    64,
                };
                notes.set(prior ? `${key}:${event.id}` : key, value);
                partForNote.set(value, part.id);
              }
            }
          }
        }
      }
    }
  }
  // Match per-part MIDI CC64 semantics after ties have been assembled. Pedal
  // sustains sounding notes, independently of physically held key durations.
  for (const note of notes.values()) {
    for (const span of model.pedal_spans ?? []) {
      if (partForNote.get(note) !== span.part_id) continue;
      const pedalStart = origin + rationalBeats(span.start) * secondsPerBeat;
      const pedalEnd = origin + rationalBeats(span.end) * secondsPerBeat;
      if (note.end >= pedalStart - 1e-9 && note.end < pedalEnd)
        note.end = pedalEnd;
    }
  }
  return [...notes.values()].sort(
    (a, b) => a.start - b.start || a.pitch - b.pitch,
  );
}

export function activeNoteIds(
  notes: PlaybackNote[],
  position: number,
): string[] {
  return [
    ...new Set(
      notes
        .filter((note) => note.start <= position && note.end > position)
        .flatMap((note) => note.sourceIds),
    ),
  ];
}

export function midiFrequency(pitch: number): number {
  return 440 * 2 ** ((pitch - 69) / 12);
}

export function validLoop(
  start: number,
  end: number,
  duration: number,
): boolean {
  return (
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    start >= 0 &&
    end <= duration + 0.001 &&
    end - start >= 0.05
  );
}

/** A single clock definition is shared by synthesis scheduling and its cursor. */
export function playbackPosition(
  initial: number,
  elapsed: number,
  duration: number,
  loop: LoopRegion | null,
): { position: number; cycle: number; finished: boolean } {
  const progress = Math.max(0, elapsed);
  if (!loop) {
    const position = Math.min(duration, initial + progress);
    return { position, cycle: 0, finished: position >= duration };
  }
  const start =
    initial >= loop.start && initial < loop.end ? initial : loop.start;
  const firstSpan = loop.end - start;
  if (progress < firstSpan - 1e-9)
    return { position: start + progress, cycle: 0, finished: false };
  const afterFirst = Math.max(0, progress - firstSpan);
  const length = loop.end - loop.start;
  const fullCycles = Math.floor((afterFirst + 1e-9) / length);
  const cycle = fullCycles + 1;
  return {
    position: loop.start + Math.max(0, afterFirst - fullCycles * length),
    cycle,
    finished: false,
  };
}

export function maximumPolyphony(notes: PlaybackNote[]): number {
  const edges = notes.flatMap((note) => [
    { at: note.start, change: 1 },
    { at: note.end, change: -1 },
  ]);
  edges.sort((a, b) => a.at - b.at || a.change - b.change);
  let active = 0;
  let peak = 0;
  for (const edge of edges) {
    active += edge.change;
    peak = Math.max(peak, active);
  }
  return peak;
}

/** Look-ahead synthesis schedules with the audio clock, not animation frames. */
export class ReviewSynth {
  readonly context: AudioContext;
  private voices = new Set<AudioScheduledSourceNode>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private epoch = 0;
  private initial = 0;
  private duration = 0;
  private loop: LoopRegion | null = null;
  private stopped = true;

  constructor(context: AudioContext) {
    this.context = context;
  }

  position() {
    return playbackPosition(
      this.initial,
      this.context.currentTime - this.epoch,
      this.duration,
      this.loop,
    );
  }

  play(
    notes: PlaybackNote[],
    initial: number,
    duration: number,
    loop: LoopRegion | null,
    samples: PianoSampleBank | null = null,
  ) {
    this.stop();
    const polyphony = maximumPolyphony(notes);
    if (polyphony > 64)
      throw new Error(
        "This transcription has more than 64 simultaneous notes. Reduce overlapping artifacts before synthesized playback; exports retain every saved note.",
      );
    this.initial =
      loop && (initial < loop.start || initial >= loop.end)
        ? loop.start
        : initial;
    this.duration = duration;
    this.loop = loop;
    this.epoch = this.context.currentTime + 0.025;
    this.stopped = false;
    let windowStart = this.epoch;
    let scheduledCycle = -1;
    const scheduled = new Set<string>();
    const level = 0.1 / Math.sqrt(Math.max(1, polyphony));
    const schedule = () => {
      if (this.stopped) return;
      const horizon = this.context.currentTime + 0.18;
      // On a suspended/backgrounded tab, catch up instead of scheduling a
      // burst of notes whose intended start and end have already passed.
      windowStart = Math.max(windowStart, this.context.currentTime);
      let segments = 0;
      while (windowStart < horizon && segments++ < 8) {
        const state = playbackPosition(
          this.initial,
          windowStart - this.epoch,
          this.duration,
          this.loop,
        );
        if (state.finished) break;
        if (state.cycle !== scheduledCycle) {
          scheduled.clear();
          scheduledCycle = state.cycle;
        }
        const boundary = this.loop?.end ?? this.duration;
        const segmentLength = Math.min(
          horizon - windowStart,
          boundary - state.position,
        );
        if (segmentLength < 1e-8) {
          windowStart += 1e-7;
          continue;
        }
        const segmentEnd = state.position + segmentLength;
        for (let index = 0; index < notes.length; index++) {
          const note = notes[index];
          if (note.start >= segmentEnd) break;
          if (note.end <= state.position || scheduled.has(String(index)))
            continue;
          scheduled.add(String(index));
          const start = windowStart + Math.max(0, note.start - state.position);
          const end =
            windowStart + Math.min(note.end, boundary) - state.position;
          if (end <= this.context.currentTime || end <= start) continue;
          const actualStart = Math.max(start, this.context.currentTime);
          const sample = samples?.sample(note.pitch);
          const sampleOffset = sample
            ? (Math.max(0, state.position - note.start) + actualStart - start) *
              sample.rate
            : 0;
          // Seeking into a naturally decayed sample never retriggers its attack.
          // Its logical held-note/cursor duration remains the saved duration.
          if (sample && sampleOffset >= sample.buffer.duration) continue;
          let voice: AudioBufferSourceNode | OscillatorNode;
          if (sample) {
            const source = this.context.createBufferSource();
            source.buffer = sample.buffer;
            source.playbackRate.value = sample.rate;
            voice = source;
          } else {
            const oscillator = this.context.createOscillator();
            oscillator.type = "triangle";
            oscillator.frequency.value = midiFrequency(note.pitch);
            voice = oscillator;
          }
          const gain = this.context.createGain();
          const attack = Math.min(0.008, (end - actualStart) / 3);
          const release = Math.min(0.02, (end - actualStart) / 3);
          gain.gain.setValueAtTime(0, actualStart);
          gain.gain.linearRampToValueAtTime(
            (level * Math.max(1, Math.min(127, note.velocity))) / 127,
            actualStart + attack,
          );
          gain.gain.setValueAtTime(
            (level * Math.max(1, Math.min(127, note.velocity))) / 127,
            Math.max(actualStart + attack, end - release),
          );
          gain.gain.linearRampToValueAtTime(0, end);
          voice.connect(gain);
          gain.connect(this.context.destination);
          this.voices.add(voice);
          voice.onended = () => {
            this.voices.delete(voice);
            voice.disconnect();
            gain.disconnect();
          };
          if (sample)
            (voice as AudioBufferSourceNode).start(actualStart, sampleOffset);
          else voice.start(actualStart);
          voice.stop(end);
        }
        windowStart += segmentLength;
      }
    };
    schedule();
    this.timer = setInterval(schedule, 25);
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    for (const voice of this.voices) {
      try {
        voice.stop();
      } catch {
        // An already-ended oscillator has no outstanding audio to stop.
      }
      voice.disconnect();
    }
    this.voices.clear();
  }
}

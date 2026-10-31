import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play, Repeat2, SkipBack } from "lucide-react";
import { request } from "./api";
import type { CanonicalScore, NoteEvent, ProjectDetail } from "./api";
import {
  activeNoteIds,
  performancePlaybackNotes,
  ReviewSynth,
  scorePlaybackNotes,
  validLoop,
} from "./playback";
import type { LoopRegion, PlaybackNote } from "./playback";
import { PianoSampleBank } from "./pianoSamples";

type Mode = "original" | "reference" | "edited" | "raw" | "score";
type SongReference = {
  available: boolean;
  reason?: string;
  title?: string;
  clip_start_seconds?: number;
  clip_duration_seconds?: number;
};
type Waveform = { duration: number; peaks: number[] };
const modeNames: Record<Mode, string> = {
  original: "Original recording",
  reference: "Full original song",
  edited: "Edited performance · unquantized",
  raw: "Raw performance · unquantized",
  score: "Quantized score",
};
const secondsLabel = (value: number) =>
  `${Math.floor(Math.max(0, value) / 60)}:${(Math.max(0, value) % 60).toFixed(2).padStart(5, "0")}`;

export interface ReviewPlaybackProps {
  project: ProjectDetail;
  notes: NoteEvent[];
  score: CanonicalScore | null;
  scoreStale?: boolean;
  selectedIds: string[];
  onSelectNote: (id: string) => void;
  onPositionChange?: (positionSeconds: number, activeIds: string[]) => void;
}

export default function ReviewPlayback({
  project,
  notes,
  score,
  scoreStale = false,
  selectedIds,
  onSelectNote,
  onPositionChange,
}: ReviewPlaybackProps) {
  const audio = useRef<HTMLAudioElement>(null);
  const context = useRef<AudioContext | null>(null);
  const synth = useRef<ReviewSynth | null>(null);
  const samples = useRef<PianoSampleBank | null>(null);
  const sampleLoad = useRef<AbortController | null>(null);
  const frame = useRef<number | null>(null);
  const generation = useRef(0);
  const running = useRef(false);
  const positionRef = useRef(0);
  const onPosition = useRef(onPositionChange);
  onPosition.current = onPositionChange;
  const [mode, setMode] = useState<Mode>("original");
  const [sound, setSound] = useState<"piano" | "tones">("piano");
  const [sampleProgress, setSampleProgress] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [starting, setStarting] = useState(false);
  const [position, setPosition] = useState(0);
  const [waveform, setWaveform] = useState<Waveform | null>(null);
  const [waveError, setWaveError] = useState("");
  const [reference, setReference] = useState<SongReference | null>(null);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState<NoteEvent[] | null>(null);
  const [rawError, setRawError] = useState("");
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [loopStart, setLoopStart] = useState("0");
  const [loopEnd, setLoopEnd] = useState("");
  const clipOffset = project.clip_start_seconds ?? 0;
  const originalLabel = project.arrangement_source_id
    ? "Instrumental source audio"
    : project.parent_project_id
      ? "Separated stem"
      : "Original recording";
  const songOffset = project.source_origin_seconds ?? 0;
  const audioMode = mode === "original" || mode === "reference";
  const referenceMode = mode === "reference";
  const hasLineage = Boolean(
    project.arrangement_source_id || project.parent_project_id,
  );
  const audioOffset = referenceMode
    ? (reference?.clip_start_seconds ?? 0)
    : clipOffset;
  const editedNotes = useMemo(
    () =>
      performancePlaybackNotes(
        project.output_mode === "accompaniment"
          ? notes.filter((note) => note.role !== "vocal_cue")
          : notes,
      ),
    [notes, project.output_mode],
  );
  const quantizedNotes = useMemo(() => scorePlaybackNotes(score), [score]);
  const rawNotes = useMemo(() => performancePlaybackNotes(raw ?? []), [raw]);
  const chosenNotes =
    mode === "score" ? quantizedNotes : mode === "raw" ? rawNotes : editedNotes;
  const allDuration =
    waveform?.duration ??
    project.clip_duration_seconds ??
    Math.max(0, (project.duration_seconds ?? 0) - clipOffset);
  const duration =
    mode === "score"
      ? Math.max(allDuration, ...quantizedNotes.map((note) => note.end), 0)
      : allDuration;
  const startValue = Number(loopStart);
  const endValue = loopEnd === "" ? duration : Number(loopEnd);
  const loopValid = validLoop(startValue, endValue, duration);
  const loop: LoopRegion | null =
    loopEnabled && loopValid ? { start: startValue, end: endValue } : null;
  const playbackState = useRef({
    mode,
    notes: chosenNotes,
    duration,
    loop,
    clipOffset: audioOffset,
  });
  playbackState.current = {
    mode,
    notes: chosenNotes,
    duration,
    loop,
    clipOffset: audioOffset,
  };

  const publish = useCallback((value: number, current: PlaybackNote[]) => {
    const time = Math.max(0, value);
    positionRef.current = time;
    setPosition(time);
    onPosition.current?.(time, activeNoteIds(current, time));
  }, []);

  const stop = useCallback(() => {
    ++generation.current;
    running.current = false;
    audio.current?.pause();
    synth.current?.stop();
    sampleLoad.current?.abort();
    sampleLoad.current = null;
    setSampleProgress(null);
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    setPlaying(false);
    setStarting(false);
    onPosition.current?.(positionRef.current, []);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setReference(null);
    if (!hasLineage) return () => controller.abort();
    void request<SongReference>(`/projects/${project.id}/playback/reference`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setReference(result);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setReference({
            available: false,
            reason: "Full original song reference is unavailable.",
          });
      });
    return () => controller.abort();
  }, [project.id, hasLineage]);

  useEffect(() => {
    const controller = new AbortController();
    setWaveform(null);
    setWaveError("");
    void request<Waveform>(
      `/projects/${project.id}/${referenceMode ? "playback/reference/waveform" : "waveform"}`,
      {
        signal: controller.signal,
      },
    )
      .then((result) => {
        if (!controller.signal.aborted) setWaveform(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setWaveError((reason as Error).message);
      });
    return () => controller.abort();
  }, [project.id, referenceMode]);

  useEffect(() => {
    if (mode !== "raw" || raw !== null || rawError) return;
    const controller = new AbortController();
    void request<{ notes: NoteEvent[] }>(
      `/projects/${project.id}/playback/raw`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) setRaw(result.notes);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setRawError((reason as Error).message);
      });
    return () => controller.abort();
  }, [mode, project.id, raw, rawError]);

  // Editing or switching time bases stops old scheduled tones immediately.
  // Playback then restarts deliberately from the current cursor on a gesture.
  useEffect(() => {
    stop();
  }, [
    mode,
    audioOffset,
    project.id,
    sound,
    notes,
    score,
    scoreStale,
    loopEnabled,
    loopStart,
    loopEnd,
    stop,
  ]);

  useEffect(
    () => () => {
      ++generation.current;
      running.current = false;
      audio.current?.pause();
      synth.current?.stop();
      sampleLoad.current?.abort();
      samples.current?.clear();
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      void context.current?.close();
    },
    [],
  );

  const seek = (value: number) => {
    stop();
    const time = Math.max(0, Math.min(duration, value));
    publish(time, chosenNotes);
    if (audio.current) audio.current.currentTime = time + audioOffset;
  };

  const tick = useCallback(() => {
    if (!running.current) return;
    const current = playbackState.current;
    if (current.mode === "original" || current.mode === "reference") {
      const player = audio.current;
      if (!player) return;
      let time = Math.max(0, player.currentTime - current.clipOffset);
      const boundary = current.loop?.end ?? current.duration;
      if (time >= boundary - 0.005) {
        if (current.loop) {
          time = current.loop.start;
          player.currentTime = time + current.clipOffset;
          if (player.paused) void player.play().catch(() => stop());
        } else {
          publish(current.duration, current.notes);
          stop();
          return;
        }
      }
      publish(time, current.notes);
    } else if (synth.current) {
      const state = synth.current.position();
      publish(state.position, current.notes);
      if (state.finished) {
        stop();
        return;
      }
    }
    frame.current = requestAnimationFrame(tick);
  }, [publish, stop]);

  const play = async () => {
    if (playing || starting) {
      stop();
      return;
    }
    if (loopEnabled && !loopValid) return;
    if (mode === "score" && (!score || scoreStale)) return;
    if (mode === "raw" && raw === null) return;
    if (referenceMode && !reference?.available) return;
    setError("");
    setStarting(true);
    const token = ++generation.current;
    let start = positionRef.current;
    if (start >= duration) start = loop?.start ?? 0;
    if (loop && (start < loop.start || start >= loop.end)) start = loop.start;
    try {
      if (audioMode) {
        if (!audio.current) return;
        audio.current.currentTime = start + audioOffset;
        await audio.current.play();
      } else {
        if (!window.AudioContext)
          throw new Error(
            "This browser does not support synthesized audio playback.",
          );
        context.current ??= new AudioContext();
        synth.current ??= new ReviewSynth(context.current);
        await context.current.resume();
        if (token !== generation.current) return;
        let bank: PianoSampleBank | null = null;
        if (sound === "piano") {
          samples.current ??= new PianoSampleBank(context.current);
          bank = samples.current;
          const controller = new AbortController();
          sampleLoad.current = controller;
          const required = chosenNotes.filter(
            (note) =>
              note.end > (loop?.start ?? start) &&
              note.start < (loop?.end ?? duration),
          );
          await bank.prepare(
            required.map((note) => note.pitch),
            controller.signal,
            (loaded, total) => {
              if (token === generation.current)
                setSampleProgress(
                  `Loading piano samples: ${loaded} of ${total}`,
                );
            },
          );
          if (token !== generation.current) return;
          sampleLoad.current = null;
          setSampleProgress(null);
        }
        synth.current.play(chosenNotes, start, duration, loop, bank);
      }
      // stop() already cancels the old media request. A late promise must not
      // stop a newer playback session that started after that cancellation.
      if (token !== generation.current) return;
      publish(start, chosenNotes);
      running.current = true;
      setPlaying(true);
      setStarting(false);
      frame.current = requestAnimationFrame(tick);
    } catch (reason) {
      if (token !== generation.current) return;
      stop();
      setError(
        audioMode
          ? "The browser could not play this original file. Check its audio format or try a WAV recording."
          : (reason as Error).message,
      );
    }
  };

  const selectedNotes = notes.filter((note) => selectedIds.includes(note.id));
  const displayedNotes = chosenNotes;
  const waveformWidth = 1000;
  const waveformDuration = Math.max(0.001, duration);
  const x = (seconds: number) =>
    Math.max(0, Math.min(1000, (seconds / waveformDuration) * 1000));
  const noteActions = useRef({ onSelectNote, seek });
  noteActions.current = { onSelectNote, seek };
  // Thousands of peak bars and note rectangles do not need reconciliation on
  // every cursor frame. Rebuild only when audio, edits, selection or sounding
  // note IDs change; the vertical cursor itself continues at animation speed.
  const waveformBars = useMemo(() => {
    const width = Math.min(
      1000,
      (allDuration / Math.max(0.001, duration)) * 1000,
    );
    return waveform?.peaks.map((peak, index) => (
      <line
        key={index}
        x1={(index / waveform.peaks.length) * width}
        x2={(index / waveform.peaks.length) * width}
        y1={45 - Math.min(1, Math.abs(peak)) * 37}
        y2={45 + Math.min(1, Math.abs(peak)) * 37}
        stroke="#286558"
        strokeWidth={Math.max(0.5, width / waveform.peaks.length)}
      />
    ));
  }, [waveform, allDuration, duration]);
  const activeKey = activeNoteIds(displayedNotes, position).join(" ");
  const noteMarkers = useMemo(() => {
    const lane = displayedNotes.slice(0, 10000),
      selected = new Set(selectedIds),
      active = new Set(activeKey.split(" "));
    const minPitch = Math.min(48, ...lane.map((n) => n.pitch)),
      maxPitch = Math.max(72, ...lane.map((n) => n.pitch));
    const place = (t: number) =>
      Math.max(0, Math.min(1000, (t / Math.max(0.001, duration)) * 1000));
    return lane.map((note, index) => (
      <rect
        key={`${note.id}:${index}`}
        x={place(note.start)}
        y={92 + ((maxPitch - note.pitch) / (maxPitch - minPitch)) * 44}
        width={Math.max(2, place(note.end) - place(note.start))}
        height={5}
        rx={1}
        fill={
          note.sourceIds.some((id) => selected.has(id))
            ? "#cb792d"
            : note.sourceIds.some((id) => active.has(id))
              ? "#2b6b59"
              : "#819972"
        }
        data-source-note-id={note.sourceIds[0]}
        onClick={(event) => {
          event.stopPropagation();
          if (note.sourceIds[0])
            noteActions.current.onSelectNote(note.sourceIds[0]);
          noteActions.current.seek(Math.max(0, note.start));
        }}
      >
        <title>{`MIDI ${note.pitch} · ${note.start.toFixed(2)}–${note.end.toFixed(2)} seconds`}</title>
      </rect>
    ));
  }, [displayedNotes, selectedIds, activeKey, duration]);
  const disabled =
    duration <= 0 ||
    (loopEnabled && !loopValid) ||
    (mode === "raw" && raw === null) ||
    (referenceMode && !reference?.available) ||
    (mode === "score" && (!score || scoreStale));

  return (
    <section
      className="card review-playback"
      aria-label="Audio review playback"
    >
      <div className="panel-heading">
        <h2>Listen and compare</h2>
        <span>{mode === "original" ? originalLabel : modeNames[mode]}</span>
      </div>
      <audio
        ref={audio}
        src={`/api/projects/${encodeURIComponent(project.id)}/${referenceMode ? "playback/reference/audio" : "audio"}`}
        preload="metadata"
        onError={() => {
          if (
            playbackState.current.mode === "original" ||
            playbackState.current.mode === "reference"
          ) {
            stop();
            setError(
              "The browser cannot decode this original audio file. Synthesized playback is still available.",
            );
          }
        }}
        onEnded={() => {
          if (!playbackState.current.loop) stop();
        }}
      />
      <div
        className="review-transport"
        style={{
          display: "flex",
          gap: 12,
          alignItems: "end",
          flexWrap: "wrap",
        }}
      >
        <label>
          Playback source
          <select
            aria-label="Playback source"
            value={mode}
            onChange={(event) => {
              stop();
              setError("");
              setMode(event.target.value as Mode);
            }}
          >
            {(Object.entries(modeNames) as [Mode, string][]).map(
              ([value, label]) => (
                <option
                  key={value}
                  value={value}
                  disabled={value === "reference" && !reference?.available}
                  hidden={value === "reference" && !hasLineage}
                >
                  {value === "original" ? originalLabel : label}
                </option>
              ),
            )}
          </select>
        </label>
        {!audioMode && (
          <label>
            Review sound
            <select
              aria-label="Review sound"
              value={sound}
              onChange={(event) => {
                stop();
                setError("");
                setSound(event.target.value as "piano" | "tones");
              }}
            >
              <option value="piano">Sampled piano</option>
              <option value="tones">Simple tones</option>
            </select>
          </label>
        )}
        <button
          className="button primary small"
          onClick={() => void play()}
          disabled={disabled}
          aria-label={
            playing || starting
              ? "Pause review playback"
              : "Play review playback"
          }
        >
          {playing || starting ? <Pause size={16} /> : <Play size={16} />}
          {starting ? "Starting…" : playing ? "Pause" : "Play"}
        </button>
        <button
          className="button secondary small"
          onClick={() => seek(loop?.start ?? 0)}
          aria-label="Rewind review playback"
        >
          <SkipBack size={16} /> Start
        </button>
        <output
          aria-label="Playback position"
          data-testid="playback-position"
          data-seconds={position.toFixed(4)}
        >
          {secondsLabel(position)} / {secondsLabel(duration)}
        </output>
      </div>
      {!audioMode && (
        <p className="input-help">
          {sound === "piano"
            ? "Recorded piano timbre with natural decay; velocity controls volume. "
            : "Simple synthesized tones for pitch and rhythm review. "}
          {mode === "score"
            ? "Uses the quantized score shown below, aligned to the clip through its downbeat/pickup mapping."
            : mode === "raw"
              ? "Uses preserved raw detections, before cleanup or editing."
              : "Uses the current edited note times, including unsaved edits; no score quantization."}
        </p>
      )}
      {!audioMode && sound === "piano" && (
        <p className="input-help">
          Samples load locally when you press Play. Pitches, timing and velocity
          are retained; no random timing or accompaniment is added. One recorded
          velocity layer is used.{" "}
          <a
            href="/samples/salamander/ATTRIBUTION.txt"
            target="_blank"
            rel="noreferrer"
          >
            Salamander piano by Alexander Holm · CC BY 3.0
          </a>
        </p>
      )}
      {hasLineage && !reference && (
        <p className="input-help" role="status">
          Finding the full original song for comparison…
        </p>
      )}
      {hasLineage && reference && !reference.available && (
        <p className="input-help" role="status">
          {reference.reason}
        </p>
      )}
      {referenceMode && reference?.available && (
        <p className="input-help">
          Full mix: {reference.title}. Switching sources keeps the same clip
          position and loop; press Play to compare.
        </p>
      )}
      {sampleProgress && <p role="status">{sampleProgress}</p>}
      <p className="input-help">
        Times are relative to the selected clip.{" "}
        {project.parent_project_id ? "Original-song" : "Original-recording"}{" "}
        time is {secondsLabel(position + clipOffset + songOffset)}. Click the
        waveform to seek; click a note marker to select it.
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
          {!audioMode && sound === "piano" && (
            <>
              {" "}
              <button
                className="button secondary small"
                disabled={starting}
                onClick={() => void play()}
              >
                Retry sampled piano
              </button>
            </>
          )}
        </p>
      )}
      {mode === "raw" && raw === null && !rawError && (
        <p role="status">Loading preserved raw events…</p>
      )}
      {mode === "raw" && rawError && (
        <p className="notice" role="status">
          Raw playback unavailable: {rawError}
        </p>
      )}
      {mode === "score" && (!score || scoreStale) && (
        <p className="notice" role="status">
          Preview the current edits to update quantized-score playback.
        </p>
      )}
      <div
        className="review-waveform"
        style={{ position: "relative", marginTop: 16 }}
      >
        <svg
          viewBox={`0 0 ${waveformWidth} 150`}
          style={{
            width: "100%",
            height: 150,
            display: "block",
            background: "#f4f7f0",
            borderRadius: 8,
            cursor: "crosshair",
          }}
          role="img"
          aria-label="Decoded audio waveform, note markers, and playback cursor"
          data-testid="review-waveform"
          onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            seek(((event.clientX - rect.left) / rect.width) * duration);
          }}
        >
          {loop && (
            <rect
              x={x(loop.start)}
              y={0}
              width={Math.max(0, x(loop.end) - x(loop.start))}
              height={150}
              fill="#c8d9b7"
              opacity={0.5}
            />
          )}
          <line x1={0} x2={1000} y1={45} y2={45} stroke="#c9d2c4" />
          {waveformBars}
          {noteMarkers}
          <line
            x1={x(position)}
            x2={x(position)}
            y1={0}
            y2={150}
            stroke="#d76b32"
            strokeWidth={2}
            data-testid="playback-cursor"
          />
        </svg>
        {!waveform && (
          <p className="input-help" role="status">
            {waveError
              ? `Waveform unavailable: ${waveError}`
              : "Decoding the waveform…"}
          </p>
        )}
      </div>
      <input
        aria-label="Review playback seek"
        type="range"
        min={0}
        max={Math.max(0.01, duration)}
        step="0.01"
        value={Math.min(position, duration)}
        onChange={(event) => seek(Number(event.target.value))}
        style={{ width: "100%" }}
      />
      <div
        className="review-loop-controls"
        style={{
          display: "flex",
          gap: 12,
          alignItems: "end",
          flexWrap: "wrap",
          marginTop: 12,
        }}
      >
        <label
          style={{
            display: "flex",
            gap: 7,
            alignItems: "center",
            minHeight: 36,
          }}
        >
          <input
            type="checkbox"
            checked={loopEnabled}
            onChange={(event) => setLoopEnabled(event.target.checked)}
          />
          <Repeat2 size={15} /> Loop region
        </label>
        <label>
          Loop start (s)
          <input
            aria-label="Loop start seconds"
            type="number"
            min={0}
            max={duration}
            step="0.01"
            value={loopStart}
            onChange={(event) => setLoopStart(event.target.value)}
            style={{ width: 110 }}
          />
        </label>
        <label>
          Loop end (s)
          <input
            aria-label="Loop end seconds"
            type="number"
            min={0}
            max={duration}
            step="0.01"
            value={loopEnd}
            placeholder={duration.toFixed(2)}
            onChange={(event) => setLoopEnd(event.target.value)}
            style={{ width: 110 }}
          />
        </label>
        <button
          className="button secondary small"
          disabled={!selectedNotes.length}
          onClick={() => {
            const from = Math.max(
              0,
              Math.min(...selectedNotes.map((note) => note.start)),
            );
            const to = Math.min(
              duration,
              Math.max(...selectedNotes.map((note) => note.end)),
            );
            setLoopStart(String(from));
            setLoopEnd(String(to));
            setLoopEnabled(true);
            seek(from);
          }}
        >
          Loop selected notes
        </button>
        <button
          className="button secondary small"
          disabled={!selectedNotes.length}
          onClick={() =>
            seek(Math.min(...selectedNotes.map((note) => note.start)))
          }
        >
          Seek to selected note
        </button>
      </div>
      {loopEnabled && !loopValid && (
        <p className="field-error" role="alert">
          Choose a loop within the playback range, at least 0.05 seconds long.
        </p>
      )}
    </section>
  );
}

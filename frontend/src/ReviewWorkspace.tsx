import { useCallback, useMemo, useRef, useState } from "react";
import type { CanonicalScore, Health, NoteEvent, ProjectDetail } from "./api";
import { noteEditValues } from "./api";
import NoteEditor, { invalidNote } from "./NoteEditor";
import ScoreEditor from "./ScoreEditor";
import type { ScoreEditorHandle } from "./ScoreEditor";
import ReviewPlayback from "./ReviewPlayback";

const values = noteEditValues;
export default function ReviewWorkspace({
  project,
  onSaved,
  health,
}: {
  project: ProjectDetail;
  health?: Health | null;
  onSaved: (project: ProjectDetail) => void;
}) {
  const [history, setHistory] = useState<{
    past: NoteEvent[][];
    present: NoteEvent[];
    future: NoteEvent[][];
  }>(() => ({ past: [], present: project.notes ?? [], future: [] }));
  const [savedValues, setSavedValues] = useState(() =>
    values(project.notes ?? []),
  );
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [activeIds, setActiveIds] = useState<string[]>([]);
  const position = useRef(0);
  const [score, setScore] = useState<CanonicalScore | null>(null);
  const [scoreStale, setScoreStale] = useState(true);
  const [busy, setBusy] = useState(false);
  const scoreEditor = useRef<ScoreEditorHandle>(null);
  const notes = history.present;
  const noteValues = useMemo(() => values(notes), [notes]);
  const dirty = noteValues !== savedValues;
  const invalid = useMemo(
    () =>
      invalidNote(
        notes,
        project.clip_duration_seconds ?? project.duration_seconds,
      ) >= 0,
    [notes, project.clip_duration_seconds, project.duration_seconds],
  );
  const change = useCallback((next: NoteEvent[]) => {
    setHistory((current) => ({
      past: [...current.past, current.present].slice(-100),
      present: next,
      future: [],
    }));
    setScoreStale(true);
  }, []);
  const undo = () => {
    setHistory((current) =>
      current.past.length
        ? {
            past: current.past.slice(0, -1),
            present: current.past[current.past.length - 1],
            future: [current.present, ...current.future],
          }
        : current,
    );
    setScoreStale(true);
  };
  const redo = () => {
    setHistory((current) =>
      current.future.length
        ? {
            past: [...current.past, current.present].slice(-100),
            present: current.future[0],
            future: current.future.slice(1),
          }
        : current,
    );
    setScoreStale(true);
  };
  const saved = useCallback(
    (value: ProjectDetail) => {
      const next = value.notes ?? [];
      setHistory((current) => ({ ...current, present: next }));
      setSavedValues(values(next));
      onSaved(value);
    },
    [onSaved],
  );
  const scoreChanged = useCallback(
    (next: CanonicalScore | null, stale: boolean) => {
      setScore(next);
      setScoreStale(stale);
    },
    [],
  );
  const selected = useCallback((ids: string[]) => setSelectedIds(ids), []);
  const sourceToCurrent = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const note of notes)
      for (const id of [note.id, ...note.source_note_ids]) {
        const current = map.get(id) ?? [];
        if (!current.includes(note.id)) current.push(note.id);
        map.set(id, current);
      }
    return map;
  }, [notes]);
  const sourceMapping = useRef(sourceToCurrent);
  sourceMapping.current = sourceToCurrent;
  const selectFromPlayback = useCallback(
    (id: string) => setSelectedIds(sourceMapping.current.get(id) ?? []),
    [],
  );
  const positionChanged = useCallback((seconds: number, ids: string[]) => {
    position.current = seconds;
    const mapped = [
      ...new Set(ids.flatMap((id) => sourceMapping.current.get(id) ?? [id])),
    ];
    setActiveIds((previous) =>
      previous.join(" ") === mapped.join(" ") ? previous : mapped,
    );
  }, []);
  return (
    <div
      className="review-workspace"
      onKeyDown={(event) => {
        if (
          (event.metaKey || event.ctrlKey) &&
          event.key.toLowerCase() === "z" &&
          !busy
        ) {
          event.preventDefault();
          if (event.shiftKey) redo();
          else undo();
        }
        if (
          (event.metaKey || event.ctrlKey) &&
          event.key.toLowerCase() === "s"
        ) {
          event.preventDefault();
          scoreEditor.current?.save();
        }
      }}
    >
      {notes.length > 10000 && (
        <p className="input-error" role="status">
          The complete transcription contains {notes.length.toLocaleString()}{" "}
          notes, above this editor's 10,000-note limit. All detected notes and
          the full raw-performance MIDI are retained. The visual note lane shows
          the first 10,000 markers; audio playback covers the full clip. Export
          the MIDI for editing in notation software, or transcribe a shorter
          selected clip for in-app score editing.
        </p>
      )}
      <ScoreEditor
        ref={scoreEditor}
        project={project}
        health={health}
        review={{
          notes,
          dirty,
          invalid,
          selectedIds,
          activeIds,
          onSelect: selected,
          onSaved: saved,
          onScore: scoreChanged,
          onBusy: setBusy,
        }}
      />
      <div className="review-workspace-heading">
        <div>
          <span className="eyebrow">REVIEW WORKSPACE</span>
          <h2>Listen, compare, and correct</h2>
        </div>
        <p>
          Stable note identities connect the waveform, notation, and editor.
          Save review publishes the notes and score together.
        </p>
      </div>
      <ReviewPlayback
        project={project}
        notes={notes}
        score={score}
        scoreStale={scoreStale || invalid}
        selectedIds={selectedIds}
        onSelectNote={selectFromPlayback}
        onPositionChange={positionChanged}
      />
      <div className="review-layout">
        <NoteEditor
          project={project}
          notes={notes}
          selectedIds={selectedIds}
          onSelect={selected}
          onChange={change}
          onSave={() => scoreEditor.current?.save()}
          onUndo={undo}
          onRedo={redo}
          canUndo={!!history.past.length}
          canRedo={!!history.future.length}
          dirty={dirty}
          busy={busy}
          position={position}
        />
      </div>
    </div>
  );
}

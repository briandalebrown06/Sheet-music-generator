import { useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Plus,
  Save,
  Trash2,
  Undo2,
  Redo2,
  Scissors,
  Combine,
} from "lucide-react";
import { pitchName } from "./api";
import type { NoteEvent, ProjectDetail } from "./api";

export function invalidNote(notes: NoteEvent[], duration: number | null) {
  return notes.findIndex(
    (n) =>
      !Number.isInteger(n.pitch) ||
      n.pitch < 0 ||
      n.pitch > 127 ||
      !Number.isFinite(n.start) ||
      n.start < 0 ||
      !Number.isFinite(n.end) ||
      n.end <= n.start ||
      (duration != null && n.end > duration + 0.001) ||
      (n.velocity != null &&
        (!Number.isInteger(n.velocity) ||
          n.velocity < 1 ||
          n.velocity > 127)) ||
      !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(n.part_id) ||
      (n.pedal_sustained &&
        (n.key_release_seconds == null ||
          !Number.isFinite(n.key_release_seconds) ||
          n.key_release_seconds <= n.start ||
          n.key_release_seconds > n.end)) ||
      (n.fingering != null &&
        (!Number.isInteger(n.fingering) ||
          n.fingering < 1 ||
          n.fingering > 5)) ||
      (n.voice_id != null && !/^(?:[1-9]|[12][0-9]|3[0-2])$/.test(n.voice_id)),
  );
}
const finite = (value: number) => (Number.isFinite(value) ? value : "");
const pageSize = 30;
export default function NoteEditor({
  project,
  notes,
  selectedIds,
  onSelect,
  onChange,
  onSave,
  onUndo,
  onRedo,
  canUndo,
  canRedo,
  dirty,
  busy,
  position,
}: {
  project: ProjectDetail;
  notes: NoteEvent[];
  selectedIds: string[];
  onSelect: (ids: string[]) => void;
  onChange: (notes: NoteEvent[]) => void;
  onSave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  dirty: boolean;
  busy: boolean;
  position: RefObject<number>;
}) {
  const [page, setPage] = useState(0);
  const [splitPoint, setSplitPoint] = useState(0);
  const duration = project.clip_duration_seconds ?? project.duration_seconds;
  const selected = useMemo(
    () =>
      notes
        .filter((n) => selectedIds.includes(n.id))
        .sort((a, b) => a.start - b.start || a.id.localeCompare(b.id)),
    [notes, selectedIds],
  );
  const single = selected.length === 1 ? selected[0] : null;
  useEffect(() => {
    if (single) setSplitPoint((single.start + single.end) / 2);
  }, [single?.id, single?.start, single?.end]);
  useEffect(() => {
    if (selectedIds.length === 1) {
      const index = notes.findIndex((n) => n.id === selectedIds[0]);
      if (index >= 0) setPage(Math.floor(index / pageSize));
    }
  }, [selectedIds, notes]);
  const pageCount = Math.max(1, Math.ceil(notes.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const invalid = useMemo(
    () => invalidNote(notes, duration),
    [notes, duration],
  );
  const validDurations = useRef(new Map<string, number>());
  useEffect(() => {
    for (const n of notes) {
      const length = n.end - n.start;
      if (Number.isFinite(length) && length > 0)
        validDurations.current.set(n.id, length);
    }
  }, [notes]);
  const canSplit =
    !!single &&
    Number.isFinite(splitPoint) &&
    splitPoint > single.start + 0.01 &&
    splitPoint < single.end - 0.01 &&
    notes.length < 10000;
  const canMerge =
    selected.length >= 2 &&
    selected.every(
      (n) =>
        n.pitch === selected[0].pitch &&
        n.part_id === selected[0].part_id &&
        (n.voice_id ?? null) === (selected[0].voice_id ?? null) &&
        (n.hand ?? null) === (selected[0].hand ?? null) &&
        (n.role ?? null) === (selected[0].role ?? null) &&
        (n.pedal_sustained ?? false) === (selected[0].pedal_sustained ?? false),
    ) &&
    selected.every(
      (n, i) =>
        i === 0 ||
        n.start <= Math.max(...selected.slice(0, i).map((x) => x.end)) + 0.05,
    );
  const change = (id: string, values: Partial<NoteEvent>) =>
    onChange(
      notes.map((n) => {
        if (n.id !== id) return n;
        const next = { ...n, locked: true, ...values };
        if (
          n.key_release_seconds != null &&
          values.key_release_seconds === undefined &&
          (values.start !== undefined || values.end !== undefined)
        ) {
          next.key_release_seconds = Math.min(
            next.end,
            n.key_release_seconds + (next.start - n.start),
          );
        }
        return next;
      }),
    );
  const lineage = (items: NoteEvent[]) => [
    ...new Set(
      items.flatMap((n) => (n.new_note ? (n.derived_from_ids ?? []) : [n.id])),
    ),
  ];
  const add = () => {
    const id = crypto.randomUUID();
    const start = Math.max(
      0,
      Math.min(position.current, (duration ?? position.current + 0.5) - 0.1),
    );
    onChange([
      ...notes,
      {
        id,
        new_note: true,
        pitch: single?.pitch ?? 60,
        start,
        end: Math.min(start + 0.5, duration ?? start + 0.5),
        velocity: 80,
        part_id: single?.part_id ?? "part-1",
        voice_id: null,
        provenance: "manually_added",
        model_score: null,
        source_note_ids: [],
        original_timing: null,
        score_timing: null,
        locked: true,
        hand: single?.hand ?? null,
      },
    ]);
    onSelect([id]);
  };
  const split = () => {
    if (!single || !canSplit) return;
    const id = crypto.randomUUID();
    const second: NoteEvent = {
      ...single,
      id,
      new_note: true,
      start: splitPoint,
      key_release_seconds: single.pedal_sustained
        ? Math.max(splitPoint + 0.001, single.key_release_seconds ?? single.end)
        : null,
      locked: true,
      derived_from_ids: lineage([single]),
    };
    const result = notes.flatMap((n) =>
      n.id === single.id
        ? [
            {
              ...n,
              end: splitPoint,
              locked: true,
              key_release_seconds: n.pedal_sustained
                ? Math.min(splitPoint, n.key_release_seconds ?? n.end)
                : null,
            },
            second,
          ]
        : [n],
    );
    onChange(result);
    onSelect([single.id, id]);
  };
  const merge = () => {
    if (!canMerge) return;
    const first = selected[0];
    const joined = {
      ...first,
      end: Math.max(...selected.map((n) => n.end)),
      key_release_seconds: first.pedal_sustained
        ? Math.max(...selected.map((n) => n.key_release_seconds ?? n.end))
        : null,
      locked: true,
      derived_from_ids: lineage(selected),
    };
    onChange(
      notes
        .filter((n) => !selectedIds.includes(n.id) || n.id === first.id)
        .map((n) => (n.id === first.id ? joined : n)),
    );
    onSelect([first.id]);
  };
  return (
    <section className="card review-note-card" aria-label="Note event editor">
      <div className="panel-heading">
        <h2>Note event editor</h2>
        <span>
          {notes.length} notes · revision {project.notes_revision}
        </span>
      </div>
      <p className="input-help">
        Select a note here, on the waveform, or in the score. Pitch and timing
        edits affect synthesized performance immediately. Preview or save to
        update notation. Times are seconds within the selected audio clip.
      </p>
      <div className="editor-toolbar review-editor-toolbar">
        <button
          className="button secondary small"
          onClick={add}
          disabled={busy || notes.length >= 10000}
        >
          <Plus size={14} />
          Add note
        </button>
        <button
          className="button secondary small"
          onClick={onUndo}
          disabled={busy || !canUndo}
          aria-label="Undo edit"
        >
          <Undo2 size={14} />
          Undo
        </button>
        <button
          className="button secondary small"
          onClick={onRedo}
          disabled={busy || !canRedo}
          aria-label="Redo edit"
        >
          <Redo2 size={14} />
          Redo
        </button>
        <span className="review-dirty">
          {dirty ? "Unsaved changes" : "Saved performance"}
        </span>
        <button
          className="button primary small"
          onClick={onSave}
          disabled={busy || invalid >= 0}
        >
          <Save size={14} />
          Save review
        </button>
      </div>
      <div className="selection-tools">
        <strong>
          {selected.length
            ? `${selected.length} selected`
            : "Select notes to split or merge"}
        </strong>
        {single && (
          <label>
            Split at (seconds)
            <input
              aria-label="Split at seconds"
              type="number"
              step="0.01"
              min={
                Number.isFinite(single.start) ? single.start + 0.01 : undefined
              }
              max={Number.isFinite(single.end) ? single.end - 0.01 : undefined}
              value={finite(splitPoint)}
              onChange={(e) => setSplitPoint(e.target.valueAsNumber)}
            />
          </label>
        )}
        <button
          className="button secondary small"
          onClick={split}
          disabled={busy || !canSplit}
        >
          <Scissors size={14} />
          Split note
        </button>
        <button
          className="button secondary small"
          onClick={merge}
          disabled={busy || !canMerge}
        >
          <Combine size={14} />
          Merge notes
        </button>
        <button
          className="button secondary small"
          disabled={busy || !selected.length}
          onClick={() => {
            onChange(notes.filter((n) => !selectedIds.includes(n.id)));
            onSelect([]);
          }}
        >
          <Trash2 size={14} />
          Delete selected
        </button>
        {selected.length >= 2 && !canMerge && (
          <p className="input-help">
            Merge requires the same pitch, part, and voice, with overlap or gaps
            no greater than 0.05 s.
          </p>
        )}
      </div>
      <div id="piano-note-details" className="piano-note-details">
        {single ? (
          <fieldset disabled={busy}>
            <legend>Selected note · piano performance</legend>
            <p className="input-help">
              Hand, staff and voice are separate assignments. Your changes are
              locked for future arrangement passes until you unlock the note.
            </p>
            <div className="piano-note-fields">
              <label>
                Playing hand
                <select
                  aria-label="Selected note playing hand"
                  value={single.hand ?? ""}
                  onChange={(event) =>
                    change(single.id, {
                      hand: (event.target.value || null) as NoteEvent["hand"],
                    })
                  }
                >
                  <option value="">Unassigned</option>
                  <option value="left">Left hand</option>
                  <option value="right">Right hand</option>
                </select>
              </label>
              <label>
                Musical role
                <select
                  aria-label="Selected note musical role"
                  value={single.role ?? ""}
                  onChange={(event) =>
                    change(single.id, {
                      role: (event.target.value || null) as NoteEvent["role"],
                    })
                  }
                >
                  <option value="">Unspecified</option>
                  <option value="bass">Essential bass</option>
                  <option value="harmony">Harmony</option>
                  <option value="riff">Characteristic riff</option>
                  <option value="countermelody">Countermelody</option>
                  <option value="melody">Lead melody</option>
                  <option value="texture">Secondary texture</option>
                  <option value="vocal_cue">Vocal cue (separate staff)</option>
                </select>
              </label>
              <label>
                Manual finger number
                <select
                  aria-label="Selected note manual finger number"
                  value={single.fingering ?? ""}
                  onChange={(event) =>
                    change(single.id, {
                      fingering: event.target.value
                        ? Number(event.target.value)
                        : null,
                    })
                  }
                >
                  <option value="">None · not guessed</option>
                  {[1, 2, 3, 4, 5].map((finger) => (
                    <option key={finger} value={finger}>
                      {finger}
                    </option>
                  ))}
                </select>
              </label>
              <label className="piano-note-checkbox">
                <input
                  aria-label="Lock selected note for arrangement"
                  type="checkbox"
                  checked={single.locked ?? single.provenance !== "detected"}
                  onChange={(event) =>
                    change(single.id, { locked: event.target.checked })
                  }
                />{" "}
                Lock for arrangement
              </label>
              <label className="piano-note-checkbox">
                <input
                  aria-label="Selected note sustained by pedal"
                  type="checkbox"
                  checked={single.pedal_sustained ?? false}
                  onChange={(event) =>
                    change(single.id, {
                      pedal_sustained: event.target.checked,
                      key_release_seconds: event.target.checked
                        ? (single.key_release_seconds ?? single.end)
                        : null,
                    })
                  }
                />{" "}
                Sound continues under pedal (manual)
              </label>
              {single.pedal_sustained && (
                <label>
                  Release key at (seconds)
                  <input
                    aria-label="Selected note physical key release seconds"
                    type="number"
                    step="0.01"
                    min={single.start + 0.001}
                    max={single.end}
                    value={
                      single.key_release_seconds == null
                        ? ""
                        : finite(single.key_release_seconds)
                    }
                    onChange={(event) =>
                      change(single.id, {
                        key_release_seconds: event.target.valueAsNumber,
                      })
                    }
                  />
                </label>
              )}
            </div>
            {single.pedal_sustained && (
              <p className="input-help">
                The key is held until the release time; sound continues until
                the note's end. Pedal affects all notes in the same piano part
                and never makes an impossible simultaneous chord playable.
              </p>
            )}
          </fieldset>
        ) : (
          <p className="input-help">
            Select one note to assign its playing hand, preserve a riff or bass
            line, lock it against arrangement changes, or enter pedal and finger
            markings manually.
          </p>
        )}
      </div>
      {invalid >= 0 && (
        <p className="field-error" role="alert">
          Check note {invalid + 1}: pitch 0–127, positive duration inside the
          clip, velocity 1–127 or blank, and a valid part/voice assignment are
          required. Pedal key release must lie inside the note, and manual
          finger numbers must be 1–5 or blank.
        </p>
      )}
      <div className="notes-table-wrap">
        <table className="notes-table editable-notes">
          <thead>
            <tr>
              <th>Select</th>
              <th>Pitch</th>
              <th>Start (s)</th>
              <th>Duration (s)</th>
              <th>Velocity</th>
              <th>Part</th>
              <th>Voice</th>
              <th>Source</th>
              <th>
                <span className="visually-hidden">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {notes
              .slice(currentPage * pageSize, (currentPage + 1) * pageSize)
              .map((n, i) => {
                const index = currentPage * pageSize + i + 1;
                const chosen = selectedIds.includes(n.id);
                return (
                  <tr
                    key={n.id}
                    data-note-id={n.id}
                    className={chosen ? "is-selected" : ""}
                  >
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select note ${index}`}
                        checked={chosen}
                        onChange={(e) =>
                          onSelect(
                            e.target.checked
                              ? [...selectedIds, n.id]
                              : selectedIds.filter((id) => id !== n.id),
                          )
                        }
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Note ${index} MIDI pitch`}
                        type="number"
                        min={0}
                        max={127}
                        step={1}
                        value={finite(n.pitch)}
                        disabled={busy}
                        onFocus={() => onSelect([n.id])}
                        onChange={(e) =>
                          change(n.id, { pitch: e.target.valueAsNumber })
                        }
                      />
                      <small>
                        {Number.isInteger(n.pitch) &&
                        n.pitch >= 0 &&
                        n.pitch <= 127
                          ? pitchName(n.pitch)
                          : "—"}
                      </small>
                    </td>
                    <td>
                      <input
                        aria-label={`Note ${index} start seconds`}
                        type="number"
                        min={0}
                        step="0.01"
                        value={finite(n.start)}
                        disabled={busy}
                        onFocus={() => onSelect([n.id])}
                        onChange={(e) => {
                          const start = e.target.valueAsNumber;
                          change(n.id, {
                            start,
                            end: Number.isFinite(start)
                              ? start +
                                (Number.isFinite(n.end - n.start)
                                  ? n.end - n.start
                                  : (validDurations.current.get(n.id) ?? 0.5))
                              : n.end,
                          });
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Note ${index} duration seconds`}
                        type="number"
                        min="0.01"
                        step="0.01"
                        value={finite(
                          Math.round((n.end - n.start) * 1000000) / 1000000,
                        )}
                        disabled={busy}
                        onFocus={() => onSelect([n.id])}
                        onChange={(e) =>
                          change(n.id, {
                            end: n.start + e.target.valueAsNumber,
                          })
                        }
                      />
                      <small>
                        End {Number.isFinite(n.end) ? n.end.toFixed(3) : "—"} s
                      </small>
                    </td>
                    <td>
                      <input
                        aria-label={`Note ${index} velocity`}
                        type="number"
                        min={1}
                        max={127}
                        value={n.velocity == null ? "" : finite(n.velocity)}
                        disabled={busy}
                        onChange={(e) =>
                          change(n.id, {
                            velocity:
                              e.target.value === ""
                                ? null
                                : e.target.valueAsNumber,
                          })
                        }
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Note ${index} part`}
                        value={n.part_id}
                        maxLength={64}
                        disabled={busy}
                        onChange={(e) =>
                          change(n.id, { part_id: e.target.value })
                        }
                      />
                    </td>
                    <td>
                      <select
                        aria-label={`Note ${index} voice`}
                        value={n.voice_id ?? "auto"}
                        disabled={busy}
                        onChange={(e) =>
                          change(n.id, {
                            voice_id:
                              e.target.value === "auto" ? null : e.target.value,
                          })
                        }
                      >
                        <option value="auto">Auto</option>
                        {Array.from({ length: 32 }, (_, j) => (
                          <option key={j + 1} value={String(j + 1)}>
                            {j + 1}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <small>{n.provenance.replaceAll("_", " ")}</small>
                      {n.original_timing && (
                        <details className="note-origin">
                          <summary>Original timing</summary>
                          <span>
                            {n.original_timing.start.toFixed(3)}–
                            {n.original_timing.end.toFixed(3)} s · MIDI{" "}
                            {n.original_timing.pitch}
                          </span>
                        </details>
                      )}
                    </td>
                    <td>
                      <button
                        className="icon-button"
                        aria-label={`Remove note ${index}`}
                        disabled={busy}
                        onClick={() => {
                          onChange(notes.filter((item) => item.id !== n.id));
                          onSelect(selectedIds.filter((id) => id !== n.id));
                        }}
                      >
                        <Trash2 size={15} />
                      </button>
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
        {!notes.length && (
          <p className="editor-empty">
            No notes. Add a note manually after listening to the recording.
          </p>
        )}
      </div>
      <div className="editor-pagination">
        <span>
          {notes.length
            ? `${currentPage * pageSize + 1}–${Math.min((currentPage + 1) * pageSize, notes.length)} of ${notes.length}`
            : "0 notes"}
        </span>
        <button
          className="button secondary small"
          aria-label="Previous notes page"
          disabled={currentPage === 0}
          onClick={() => setPage(currentPage - 1)}
        >
          <ChevronLeft size={14} />
          Previous
        </button>
        <button
          className="button secondary small"
          aria-label="Next notes page"
          disabled={currentPage + 1 >= pageCount}
          onClick={() => setPage(currentPage + 1)}
        >
          <ChevronRight size={14} />
          Next
        </button>
      </div>
    </section>
  );
}

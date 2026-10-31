import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Ref } from "react";
import SheetMusic from "./SheetMusic";
import { Eye, FileMusic, LoaderCircle, RefreshCw, Save } from "lucide-react";
import { apiFetch, ApiError, pitchName, request, noteEditValues } from "./api";
import PianoPanel from "./PianoPanel";
import PianoEditions from "./PianoEditions";
import NotationChecks from "./NotationChecks";
import type {
  CanonicalScore,
  Health,
  ProjectDetail,
  NoteEvent,
  ScorePreview,
  ScoreSettings,
  ScoreState,
} from "./api";

const keyNames = [
  "7 flats · C♭ major / A♭ minor",
  "6 flats · G♭ major / E♭ minor",
  "5 flats · D♭ major / B♭ minor",
  "4 flats · A♭ major / F minor",
  "3 flats · E♭ major / C minor",
  "2 flats · B♭ major / G minor",
  "1 flat · F major / D minor",
  "No sharps or flats · C major / A minor",
  "1 sharp · G major / E minor",
  "2 sharps · D major / B minor",
  "3 sharps · A major / F♯ minor",
  "4 sharps · E major / C♯ minor",
  "5 sharps · B major / G♯ minor",
  "6 sharps · F♯ major / D♯ minor",
  "7 sharps · C♯ major / A♯ minor",
];

async function loadMusicXML(path: string, signal?: AbortSignal) {
  const response = await apiFetch(path, { signal });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      typeof body?.detail === "string"
        ? body.detail
        : "The saved score could not be loaded.",
      response.status,
    );
  }
  return response.text();
}

export interface ScoreEditorHandle {
  save: () => void;
}
interface ReviewProps {
  notes: NoteEvent[];
  dirty: boolean;
  invalid: boolean;
  selectedIds: string[];
  activeIds: string[];
  onSelect: (ids: string[]) => void;
  onSaved: (project: ProjectDetail) => void;
  onScore: (score: CanonicalScore | null, stale: boolean) => void;
  onBusy: (busy: boolean) => void;
}
export default function ScoreEditor({
  project,
  review,
  ref,
  health,
}: {
  project: ProjectDetail;
  health?: Health | null;
  review?: ReviewProps;
  ref?: Ref<ScoreEditorHandle>;
}) {
  const [stored, setStored] = useState<ScoreState | null>(null);
  const [settings, setSettings] = useState<ScoreSettings | null>(null);
  const [rendered, setRendered] = useState<CanonicalScore | null>(null);
  const [musicxml, setMusicxml] = useState("");
  const [renderedSettings, setRenderedSettings] = useState("");
  const [renderedNoteValues, setRenderedNoteValues] = useState("");
  const [renderedNotesRevision, setRenderedNotesRevision] = useState<
    number | null
  >(null);
  const [mode, setMode] = useState<"saved" | "preview">("saved");
  const [zoom, setZoom] = useState(1);
  const [busy, setBusy] = useState<"loading" | "preview" | "save" | null>(
    "loading",
  );
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);
  const [staffPage, setStaffPage] = useState(0);
  const [settingsExpanded, setSettingsExpanded] = useState(!review);
  const initialized = useRef(false);
  const requestGeneration = useRef(0);
  const resetRequested = useRef(false);
  const latestNotesRevision = useRef(project.notes_revision);
  const form = useRef<HTMLFormElement>(null);
  latestNotesRevision.current = project.notes_revision;
  const base = `/projects/${encodeURIComponent(project.id)}/score`;
  const notes = review?.notes ?? project.notes ?? [];
  const noteValues = useMemo(() => noteEditValues(notes), [notes]);

  useEffect(() => {
    const controller = new AbortController();
    const generation = ++requestGeneration.current;
    const shouldReset = !initialized.current || resetRequested.current;
    resetRequested.current = false;
    if (shouldReset) setBusy("loading");
    const load = async () => {
      try {
        let value = await request<ScoreState>(base, {
          signal: controller.signal,
        });
        // Older completed projects may predate canonical notation. Prepare an
        // initial score from their saved notes/defaults without changing the
        // performance revision or replacing an existing score/settings.
        if (shouldReset && !value.score && project.notes !== null) {
          try {
            value = await request<ScoreState>(base, {
              method: "PUT",
              signal: controller.signal,
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                notes_revision: project.notes_revision,
                revision: value.revision,
                settings: value.defaults,
              }),
            });
          } catch (reason) {
            if (!(reason instanceof ApiError) || reason.status !== 409)
              throw reason;
            // A second tab (or an interrupted initial load) may have saved
            // first. Use its accepted score; never overwrite it with defaults.
            value = await request<ScoreState>(base, {
              signal: controller.signal,
            });
            if (!value.score || value.stale) throw reason;
          }
        }
        const xml =
          shouldReset && value.score && !value.stale
            ? (value.musicxml ??
              (await loadMusicXML(
                `${base}/exports/musicxml?revision=${value.revision}`,
                controller.signal,
              )))
            : "";
        if (
          controller.signal.aborted ||
          generation !== requestGeneration.current
        )
          return;
        setStored(value);
        if (shouldReset) {
          const next = value.score?.settings ?? value.defaults;
          setSettings(next);
          setRendered(value.stale ? null : value.score);
          setMusicxml(xml);
          setRenderedSettings(
            value.score && !value.stale ? JSON.stringify(next) : "",
          );
          setRenderedNoteValues(noteEditValues(project.notes ?? []));
          setRenderedNotesRevision(
            value.stale ? null : value.source_notes_revision,
          );
          setMode("saved");
          setMessage("");
          setError("");
          initialized.current = true;
        }
      } catch (e) {
        if (
          !controller.signal.aborted &&
          generation === requestGeneration.current
        )
          setError((e as Error).message);
      } finally {
        if (
          !controller.signal.aborted &&
          generation === requestGeneration.current
        )
          setBusy(null);
      }
    };
    void load();
    return () => controller.abort();
  }, [base, project.notes_revision, reload]);

  const update = useCallback(
    <K extends keyof ScoreSettings>(field: K, value: ScoreSettings[K]) => {
      setSettings((previous) =>
        previous
          ? {
              ...previous,
              [field]: value,
              ...(field === "quantization" && String(value).includes("triplet")
                ? { melody_quantization: "same" as const }
                : {}),
              estimated_fields: previous.estimated_fields?.filter(
                (item) => item !== field,
              ),
              confirmed_fields:
                field === "staff_overrides"
                  ? previous.confirmed_fields
                  : [...new Set([...previous.confirmed_fields, field])],
            }
          : previous,
      );
      setMessage("");
    },
    [],
  );
  const generate = async (save: boolean) => {
    if (!settings || busy || review?.invalid) return;
    if (!form.current?.reportValidity()) return;
    const generation = ++requestGeneration.current;
    const sourceRevision = project.notes_revision;
    const submittedSettings = structuredClone(settings);
    const submittedNotes = noteValues;
    setBusy(save ? "save" : "preview");
    setError("");
    setMessage("");
    try {
      const response = await request<
        ScorePreview &
          Partial<ScoreState> & {
            project?: ProjectDetail;
            score_state?: ScoreState;
          }
      >(
        review
          ? `/projects/${project.id}/review${save ? "" : "/preview"}`
          : save
            ? base
            : `${base}/preview`,
        {
          method: save ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            notes_revision: sourceRevision,
            ...(review
              ? {
                  score_revision: stored?.revision ?? 0,
                  notes: notes.map((n) => ({
                    id: n.id,
                    pitch: n.pitch,
                    start: n.start,
                    end: n.end,
                    velocity: n.velocity,
                    part_id: n.part_id,
                    voice_id: n.voice_id ?? null,
                    hand: n.hand ?? null,
                    locked: n.locked,
                    key_release_seconds: n.key_release_seconds ?? null,
                    pedal_sustained: n.pedal_sustained ?? false,
                    role: n.role ?? null,
                    fingering: n.fingering ?? null,
                    new_note: n.new_note ?? false,
                    derived_from_ids: n.derived_from_ids ?? [],
                  })),
                }
              : save
                ? { revision: stored?.revision ?? 0 }
                : {}),
            settings: submittedSettings,
          }),
        },
      );
      if (generation !== requestGeneration.current) return;
      const result = response.score_state
        ? {
            ...response.score_state,
            musicxml: response.score_state.musicxml ?? "",
          }
        : response;
      if (latestNotesRevision.current !== sourceRevision)
        throw new Error(
          "Performance notes changed while this score was generated. Preview again using the latest saved notes.",
        );
      setRendered(result.score ?? null);
      setMusicxml(result.musicxml ?? "");
      setRenderedNotesRevision(result.source_notes_revision ?? sourceRevision);
      setRenderedNoteValues(submittedNotes);
      setRenderedSettings(JSON.stringify(submittedSettings));
      setMode(save ? "saved" : "preview");
      if (save) {
        setStored(result as ScoreState);
        if (response.project) {
          setRenderedNoteValues(noteEditValues(response.project.notes ?? []));
          review?.onSaved(response.project);
        }
        if (result.score) {
          setSettings(result.score.settings);
          setRenderedSettings(JSON.stringify(result.score.settings));
        }
        setMessage(
          review
            ? "Review saved. Notes, notation, playback and exports now share this saved revision. Raw transcription is unchanged."
            : "Score saved. MusicXML and score MIDI now use this score. Raw transcription is unchanged.",
        );
      } else
        setMessage(
          "Preview only. Save this score to update notation exports. Raw transcription is unchanged.",
        );
    } catch (e) {
      if (generation === requestGeneration.current)
        setError((e as Error).message);
    } finally {
      if (generation === requestGeneration.current) setBusy(null);
    }
  };
  const formChanged =
    !!settings && JSON.stringify(settings) !== renderedSettings;
  const stale =
    stored?.stale ||
    (stored?.score && stored.source_notes_revision !== project.notes_revision);
  const previewStale =
    rendered !== null &&
    (renderedNotesRevision !== project.notes_revision ||
      renderedNoteValues !== noteValues);
  useImperativeHandle(ref, () => ({
    save: () => {
      void generate(true);
    },
  }));
  const notifyScore = review?.onScore,
    notifyBusy = review?.onBusy;
  useEffect(() => {
    notifyScore?.(rendered, !!previewStale || !!formChanged);
  }, [rendered, previewStale, formChanged, notifyScore]);
  useEffect(() => {
    notifyBusy?.(busy !== null);
  }, [busy, notifyBusy]);
  const staffPageSize = 30;
  const staffPageCount = Math.max(1, Math.ceil(notes.length / staffPageSize));
  const visibleStaffPage = Math.min(staffPage, staffPageCount - 1);

  const unsaved =
    !!review?.dirty ||
    (!!stored?.score &&
      !!settings &&
      JSON.stringify(settings) !== JSON.stringify(stored.score.settings));
  const displayStatus = previewStale
    ? "This display uses earlier notes. Preview or save to update it."
    : formChanged
      ? "Settings changed. Preview or save to update the displayed score."
      : mode === "preview"
        ? "Preview · not yet saved"
        : stored?.score
          ? `Saved ${project.arrangement?.edition_mode === "solo_piano" ? "solo piano" : project.output_mode === "accompaniment" ? "accompaniment" : "score"} · revision ${stored.revision}`
          : busy
            ? "Loading your sheet music…"
            : "Sheet music is not available yet.";

  return (
    <>
      <SheetMusic
        projectId={project.id}
        stored={stored}
        stale={!!stale}
        unsaved={unsaved}
        busy={busy !== null}
        invalid={!!review?.invalid}
        health={health}
        musicxml={musicxml}
        rendered={rendered}
        displayStatus={displayStatus}
        zoom={zoom}
        onZoom={setZoom}
        onSave={() => void generate(true)}
        canSave={settings !== null}
        error={error}
        onRetry={() => setReload((value) => value + 1)}
        selectedIds={review?.selectedIds}
        activeIds={review?.activeIds}
        onSelect={review?.onSelect}
      />
      {settings &&
        !project.arrangement_source_id &&
        project.transcription_profile !== "vocals" && (
          <PianoEditions
            project={project}
            settings={settings}
            revision={stored?.revision ?? 0}
            disabled={unsaved || busy !== null}
          />
        )}
      <PianoPanel
        project={project}
        savedRevision={stored?.revision ?? 0}
        unsaved={unsaved}
        busy={busy !== null}
        onSelect={review?.onSelect}
      />
      <section className="card notation-card" aria-label="Musical score">
        <div className="panel-heading">
          <h2>
            <FileMusic size={19} /> Musical score
          </h2>
          <span>
            {mode === "preview"
              ? "UNSAVED PREVIEW"
              : stored?.score && !stale
                ? `SAVED · REVISION ${stored.revision}`
                : "SET UP NOTATION"}
          </span>
        </div>
        <p className="notation-intro">
          {project.output_mode === "accompaniment"
            ? "Review the arranged piano part and preview its musical settings. The source transcription remains in its separate project."
            : "Shape the detected performance into a readable score. Preview different rhythms and musical settings; your original note times and raw MIDI stay preserved."}
        </p>
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
        {stale && (
          <div className="notice" role="status">
            The performance notes have changed since this score was saved.
            Preview and save again before downloading notation exports.
          </div>
        )}
        {!settings && busy === "loading" && (
          <p className="score-render-status" role="status">
            <LoaderCircle size={16} className="spin" /> Loading score settings…
          </p>
        )}
        {!settings && !busy && (
          <button
            className="button secondary small"
            onClick={() => setReload((value) => value + 1)}
          >
            <RefreshCw size={14} /> Retry loading score
          </button>
        )}
        {settings && (
          <>
            <form
              ref={form}
              className="notation-settings"
              onSubmit={(event) => {
                event.preventDefault();
                void generate(false);
              }}
            >
              <details
                className="review-musical-settings"
                open={settingsExpanded}
                onToggle={(event) =>
                  setSettingsExpanded(event.currentTarget.open)
                }
              >
                <summary>Musical settings and piano staves</summary>
                <fieldset disabled={busy !== null || review?.invalid}>
                  <legend>Musical settings</legend>
                  <p className="notation-assumption-help">
                    Settings not supplied by you are provisional. The preview
                    lists its assumptions and any tempo estimate for review.
                  </p>
                  <div className="notation-fields">
                    <label>
                      Tempo mode
                      <select
                        aria-label="Tempo mode"
                        value={settings.tempo_bpm === null ? "auto" : "manual"}
                        onChange={(e) =>
                          update(
                            "tempo_bpm",
                            e.target.value === "auto"
                              ? null
                              : (rendered?.tempo?.bpm ?? 120),
                          )
                        }
                      >
                        <option value="auto">Estimate from note onsets</option>
                        <option value="manual">Set manually</option>
                      </select>
                    </label>
                    <label>
                      Tempo (BPM)
                      <input
                        aria-label="Tempo (BPM)"
                        type="number"
                        min={30}
                        max={300}
                        step="0.1"
                        required
                        disabled={settings.tempo_bpm === null}
                        value={
                          settings.tempo_bpm !== null &&
                          Number.isFinite(settings.tempo_bpm)
                            ? settings.tempo_bpm
                            : ""
                        }
                        placeholder="Automatic"
                        onChange={(e) =>
                          update("tempo_bpm", e.target.valueAsNumber)
                        }
                      />
                    </label>
                    <label>
                      Time signature
                      <select
                        aria-label="Time signature"
                        value={settings.time_signature}
                        onChange={(e) =>
                          update("time_signature", e.target.value)
                        }
                      >
                        {[
                          "2/4",
                          "3/4",
                          "4/4",
                          "5/4",
                          "6/8",
                          "7/8",
                          "9/8",
                          "12/8",
                        ].map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Key signature
                      <select
                        aria-label="Key signature"
                        value={settings.key_signature}
                        onChange={(e) =>
                          update("key_signature", Number(e.target.value))
                        }
                      >
                        {keyNames.map((label, index) => (
                          <option key={index} value={index - 7}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      First downbeat (seconds)
                      <input
                        aria-label="First downbeat (seconds)"
                        type="number"
                        min={0}
                        max={
                          project.clip_duration_seconds ??
                          project.duration_seconds ??
                          3600
                        }
                        step="0.001"
                        required
                        value={
                          Number.isFinite(settings.first_downbeat_seconds)
                            ? settings.first_downbeat_seconds
                            : ""
                        }
                        onChange={(e) =>
                          update(
                            "first_downbeat_seconds",
                            e.target.valueAsNumber,
                          )
                        }
                      />
                    </label>
                    <label>
                      Pickup length (beats)
                      <input
                        aria-label="Pickup length (beats)"
                        type="text"
                        inputMode="text"
                        value={settings.pickup_beats}
                        required
                        placeholder="0, 1, or 1/2"
                        onChange={(e) => update("pickup_beats", e.target.value)}
                      />
                    </label>
                    <label>
                      Quantization
                      <select
                        aria-label="Quantization"
                        value={settings.quantization}
                        onChange={(e) =>
                          update(
                            "quantization",
                            e.target.value as ScoreSettings["quantization"],
                          )
                        }
                      >
                        <option value="quarter">
                          Straight · quarter notes
                        </option>
                        <option value="eighth">Straight · eighth notes</option>
                        <option value="sixteenth">
                          Straight · sixteenth notes
                        </option>
                        <option value="eighth_triplet">
                          Triplets · eighth notes
                        </option>
                        <option value="sixteenth_triplet">
                          Triplets · sixteenth notes
                        </option>
                      </select>
                    </label>
                    <label>
                      Melody rhythm
                      <select
                        aria-label="Melody rhythm"
                        value={settings.melody_quantization ?? "same"}
                        disabled={settings.quantization.includes("triplet")}
                        onChange={(event) =>
                          update(
                            "melody_quantization",
                            event.target.value as "same" | "sixteenth",
                          )
                        }
                      >
                        <option value="same">Same grid as accompaniment</option>
                        <option value="sixteenth">
                          Sixteenth notes for melody and vocal cue
                        </option>
                      </select>
                    </label>
                    <label>
                      Vocal cue clef
                      <select
                        aria-label="Vocal cue clef"
                        value={settings.vocal_clef ?? "auto"}
                        onChange={(event) =>
                          update(
                            "vocal_clef",
                            event.target.value as "auto" | "treble" | "bass",
                          )
                        }
                      >
                        <option value="auto">
                          Choose from the vocal register
                        </option>
                        <option value="treble">Treble</option>
                        <option value="bass">Bass</option>
                      </select>
                    </label>
                    <label>
                      Clef
                      <select
                        aria-label="Clef"
                        disabled={settings.piano_split}
                        value={settings.clef}
                        onChange={(e) =>
                          update(
                            "clef",
                            e.target.value as ScoreSettings["clef"],
                          )
                        }
                      >
                        <option value="auto">Choose from pitch range</option>
                        <option value="treble">Treble</option>
                        <option value="bass">Bass</option>
                        <option value="alto">Alto</option>
                        <option value="tenor">Tenor</option>
                      </select>
                    </label>
                    <label>
                      Transposition (semitones)
                      <input
                        aria-label="Transposition (semitones)"
                        type="number"
                        min={-24}
                        max={24}
                        step={1}
                        required
                        value={
                          Number.isFinite(settings.transpose_semitones)
                            ? settings.transpose_semitones
                            : ""
                        }
                        onChange={(e) =>
                          update("transpose_semitones", e.target.valueAsNumber)
                        }
                      />
                    </label>
                  </div>
                  <p className="input-help">
                    Beats here are quarter-note beats. The downbeat is the start
                    of the first full bar, measured from the selected clip. A
                    pickup comes before it; use 0 for no pickup. Key signature
                    describes the written score and does not change
                    automatically when you transpose pitches.
                  </p>
                  <details className="staff-options">
                    <summary>Piano staves and note placement</summary>
                    <label className="notation-checkbox">
                      <input
                        type="checkbox"
                        aria-label="Piano staff split"
                        checked={settings.piano_split}
                        onChange={(e) =>
                          update("piano_split", e.target.checked)
                        }
                      />{" "}
                      Split this part into treble and bass staves
                    </label>
                    <p className="input-help">
                      {settings.readability_mode === "accompaniment"
                        ? "Assigned playing hands choose the default staves in this arrangement. Staff overrides below change engraving only. Change a playing hand in the note editor; the pitch threshold is a fallback for unassigned notes."
                        : "This is a pitch-based layout heuristic. It does not identify the performer's actual hands. Correct individual staff placements below."}
                    </p>
                    {settings.piano_split && (
                      <>
                        <label className="split-threshold">
                          Treble starts at MIDI pitch
                          <input
                            aria-label="Treble starts at MIDI pitch"
                            type="number"
                            min={0}
                            max={127}
                            step={1}
                            required
                            value={
                              Number.isFinite(settings.split_pitch)
                                ? settings.split_pitch
                                : ""
                            }
                            onChange={(e) =>
                              update("split_pitch", e.target.valueAsNumber)
                            }
                          />
                        </label>
                        <div className="staff-note-list">
                          {notes
                            .slice(
                              visibleStaffPage * staffPageSize,
                              (visibleStaffPage + 1) * staffPageSize,
                            )
                            .map((note) => (
                              <label key={note.id}>
                                <span>
                                  {pitchName(note.pitch)}{" "}
                                  <small>
                                    {note.start.toFixed(2)}–
                                    {note.end.toFixed(2)} s
                                  </small>
                                </span>
                                <select
                                  aria-label={`Staff for ${pitchName(note.pitch)} at ${note.start.toFixed(2)}s`}
                                  value={
                                    settings.staff_overrides[note.id] ?? "auto"
                                  }
                                  onChange={(e) => {
                                    const overrides = {
                                      ...settings.staff_overrides,
                                    };
                                    if (e.target.value === "auto")
                                      delete overrides[note.id];
                                    else
                                      overrides[note.id] = e.target.value as
                                        | "treble"
                                        | "bass";
                                    update("staff_overrides", overrides);
                                  }}
                                >
                                  <option value="auto">Automatic</option>
                                  <option value="treble">Treble</option>
                                  <option value="bass">Bass</option>
                                </select>
                              </label>
                            ))}
                        </div>
                        {staffPageCount > 1 && (
                          <div className="staff-pagination">
                            <button
                              type="button"
                              className="button secondary small"
                              disabled={visibleStaffPage === 0}
                              onClick={() => setStaffPage(visibleStaffPage - 1)}
                            >
                              Previous notes
                            </button>
                            <span>
                              Page {visibleStaffPage + 1} of {staffPageCount}
                            </span>
                            <button
                              type="button"
                              className="button secondary small"
                              disabled={visibleStaffPage + 1 >= staffPageCount}
                              onClick={() => setStaffPage(visibleStaffPage + 1)}
                            >
                              Next notes
                            </button>
                          </div>
                        )}
                      </>
                    )}
                  </details>
                </fieldset>
              </details>
              <div className="notation-actions">
                <button
                  type="submit"
                  className="button secondary small"
                  disabled={busy !== null || review?.invalid}
                >
                  <Eye size={15} />
                  {busy === "preview" ? "Preparing preview…" : "Preview score"}
                </button>
                <button
                  type="button"
                  className="button primary small"
                  disabled={busy !== null || review?.invalid}
                  onClick={() => void generate(true)}
                >
                  <Save size={15} />
                  {busy === "save"
                    ? "Saving score…"
                    : review
                      ? "Save review"
                      : "Save score"}
                </button>
                <button
                  type="button"
                  className="button secondary small"
                  disabled={busy !== null || review?.invalid}
                  onClick={() => {
                    resetRequested.current = true;
                    setReload((value) => value + 1);
                  }}
                >
                  <RefreshCw size={14} /> Reload saved score
                </button>
              </div>
            </form>
            {message && (
              <p className="notation-message" role="status">
                {message}
              </p>
            )}
            {rendered && (
              <div className="score-settings-summary">
                {typeof rendered.tempo?.bpm === "number" && (
                  <p>
                    <strong>
                      {rendered.tempo.bpm.toFixed(1).replace(/\.0$/, "")} BPM
                    </strong>
                    {rendered.tempo.source ? ` · ${rendered.tempo.source}` : ""}
                    {rendered.tempo.method ? ` · ${rendered.tempo.method}` : ""}
                  </p>
                )}
                {rendered.assumptions?.length > 0 && (
                  <details open={!review}>
                    <summary>Assumed musical settings · review these</summary>
                    <ul>
                      {rendered.assumptions.map((value, index) => (
                        <li key={index}>{value}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {rendered.warnings?.length > 0 && (
                  <details open={!review}>
                    <summary>Notation review notes</summary>
                    <ul>
                      {rendered.warnings.map((value, index) => (
                        <li key={index}>{value}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {rendered.readability && (
                  <NotationChecks
                    key={JSON.stringify(rendered.readability)}
                    report={rendered.readability}
                    onSelect={review?.onSelect}
                  />
                )}
              </div>
            )}
            {stored?.score && !stale && !unsaved && (
              <a
                className="button secondary small"
                href={`/api${base}/exports/json?revision=${stored.revision}`}
                download
              >
                Canonical score data (JSON)
              </a>
            )}
          </>
        )}
      </section>
    </>
  );
}

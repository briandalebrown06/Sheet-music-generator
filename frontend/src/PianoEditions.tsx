import { useEffect, useRef, useState } from "react";
import { FileMusic, LoaderCircle } from "lucide-react";
import { request } from "./api";
import type { ProjectDetail, ScoreSettings, SeparationState } from "./api";

type Edition = "faithful" | "accompaniment" | "solo_piano";
const editions: { mode: Edition; title: string; detail: string }[] = [
  {
    mode: "faithful",
    title: "Faithful instrumental transcription",
    detail:
      "Keep every detected instrumental pitch and performance time. Correct the notation grid; no promise of two-hand playability.",
  },
  {
    mode: "accompaniment",
    title: "Singer’s piano accompaniment",
    detail:
      "Reduce the instrumental texture for two hands. Show the actual vocal transcription on a separate cue staff when available.",
  },
  {
    mode: "solo_piano",
    title: "Solo piano with melody",
    detail:
      "Let the transcribed vocal melody lead the right hand, with a reduced instrumental accompaniment. This is an arrangement. Identified vocals use a sixteenth-note grid so short melody notes remain distinct.",
  },
];
interface Analysis {
  status: string;
  suggested_settings: Partial<ScoreSettings>;
  warnings: string[];
  tempo_candidates: { bpm: number; median_grid_error_ms: number }[];
  key_candidates: { tonic: string; mode: string; key_signature: number }[];
  notes_revision: number;
}
interface SavedEdition {
  id: string;
  title: string;
  edition_mode: Edition;
}

export default function PianoEditions({
  project,
  settings,
  revision,
  disabled,
}: {
  project: ProjectDetail;
  settings: ScoreSettings;
  revision: number;
  disabled: boolean;
}) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [useEstimates, setUseEstimates] = useState(false);
  const [melodyOctave, setMelodyOctave] = useState<0 | 12 | -12>(12);
  const [keyTarget, setKeyTarget] = useState(3);
  const [melody, setMelody] = useState<ProjectDetail | null>(null);
  const [saved, setSaved] = useState<SavedEdition[]>([]);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);
  const generation = useRef(0);
  const base = `/projects/${project.id}`;
  useEffect(() => {
    const abort = new AbortController();
    const id = ++generation.current;
    setAnalysis(null);
    setMelody(null);
    setSaved([]);
    setUseEstimates(false);
    setError("");
    setMessage("");
    setPending(null);
    const load = async () => {
      const work = [
        request<{ editions: SavedEdition[] }>(`${base}/piano-editions`, {
          signal: abort.signal,
        }).then((value) => {
          if (!abort.signal.aborted) setSaved(value.editions);
        }),
        project.parent_project_id
          ? request<SeparationState>(
              `/projects/${project.parent_project_id}/stems`,
              { signal: abort.signal },
            ).then(async (value) => {
              const vocal = value.children.find(
                (child) => child.stem_category === "vocals",
              );
              if (vocal) {
                const detail = await request<ProjectDetail>(
                  `/projects/${vocal.id}`,
                  { signal: abort.signal },
                );
                if (!abort.signal.aborted) {
                  setMelody(detail);
                  const pitches = (detail.notes ?? [])
                    .map((note) => note.pitch)
                    .sort((a, b) => a - b);
                  const protectedVocal = (detail.notes ?? []).some(
                    (note) => note.locked ?? note.provenance !== "detected",
                  );
                  setMelodyOctave(
                    !protectedVocal &&
                      pitches.length > 0 &&
                      pitches[Math.floor(pitches.length / 2)] < 60
                      ? 12
                      : 0,
                  );
                }
              }
            })
          : Promise.resolve(),
      ];
      const results = await Promise.allSettled(work);
      if (id !== generation.current || abort.signal.aborted) return;
      const failed = results.find((value) => value.status === "rejected");
      if (failed?.status === "rejected")
        setError(String(failed.reason.message));
    };
    void load();
    return () => {
      abort.abort();
      generation.current++;
    };
  }, [base, project.parent_project_id, project.notes_revision, reload]);

  const run = async (action: "analyze" | "vocals" | Edition | "all") => {
    if (pending || disabled) return;
    const id = generation.current;
    setPending(action);
    setError("");
    setMessage("");
    try {
      if (action === "analyze") {
        const value = await request<Analysis>(`${base}/musical-analysis`);
        if (id === generation.current) setAnalysis(value);
      } else if (action === "vocals") {
        const value = await request<ProjectDetail>(
          `/projects/${project.parent_project_id}/stems/vocals/transcribe`,
          { method: "POST" },
        );
        if (id === generation.current) {
          setMelody(value);
          setMessage(
            "Vocal transcription is running in the background. Open its project to review the melody, then refresh available editions here.",
          );
        }
      } else {
        const modes: Edition[] =
          action === "all"
            ? ["faithful", "accompaniment", "solo_piano"]
            : [action];
        const musical =
          useEstimates && analysis
            ? {
                ...settings,
                ...analysis.suggested_settings,
                estimated_fields: [
                  ...new Set([
                    ...(settings.estimated_fields ?? []).filter(
                      (field) =>
                        !Object.keys(analysis.suggested_settings).includes(
                          field,
                        ),
                    ),
                    ...(analysis.suggested_settings.estimated_fields ?? []),
                  ]),
                ],
                confirmed_fields: settings.confirmed_fields.filter(
                  (field) =>
                    !Object.keys(analysis.suggested_settings).includes(field),
                ),
              }
            : settings;
        for (const mode of modes) {
          const useVocal =
            mode !== "faithful" && melody?.status === "completed";
          const value = await request<ProjectDetail>(`${base}/piano-editions`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              notes_revision: project.notes_revision,
              score_revision: revision,
              edition_mode: mode,
              melody_octave_shift: mode === "solo_piano" ? melodyOctave : 0,
              settings: musical,
              piano_settings: {
                include_vocal_cue: mode === "accompaniment" && useVocal,
                target_keys_per_hand: keyTarget,
              },
              ...(useVocal
                ? {
                    melody_project_id: melody.id,
                    melody_notes_revision: melody.notes_revision,
                  }
                : {}),
            }),
          });
          if (id !== generation.current) return;
          setSaved((previous) => [
            { id: value.id, title: value.title, edition_mode: mode },
            ...previous.filter((item) => item.id !== value.id),
          ]);
        }
        if (id === generation.current)
          setMessage(
            "Saved separate editions. Open a version below to review, play, correct and download it.",
          );
      }
    } catch (reason) {
      if (id === generation.current) setError((reason as Error).message);
    } finally {
      if (id === generation.current) setPending(null);
    }
  };
  const vocalReady = melody?.status === "completed";
  return (
    <section className="card piano-editions" aria-label="Three piano editions">
      <div className="panel-heading">
        <h2>
          <FileMusic size={20} /> Choose the kind of score
        </h2>
        <span>SEPARATE SAVED EDITIONS</span>
      </div>
      <p>
        Transcription preserves detected notes. Arrangement makes editorial
        choices for a pianist. Each version keeps its source and labels
        estimated musical settings.
      </p>
      <label className="piano-texture-choice">
        Arrangement texture · held keys per hand
        <select
          aria-label="New arrangement held-key target"
          value={keyTarget}
          disabled={!!pending || disabled}
          onChange={(event) => setKeyTarget(Number(event.target.value))}
        >
          <option value={2}>2 · sparse</option>
          <option value={3}>3 · lighter piano texture</option>
          <option value={4}>4 · fuller texture</option>
          <option value={5}>5 · retain more inner notes</option>
        </select>
      </label>
      <p className="input-help">
        Arrangement keeps melody, bass and marked figures, then reduces optional
        inner notes. Protected conflicts stay visible. The faithful
        transcription retains every note. Accuracy still depends on reviewing
        detected pitches and rhythms against the recording, whether
        human-performed or AI-generated.
      </p>
      <div className="piano-actions">
        <button
          className="button secondary"
          disabled={disabled || !!pending}
          onClick={() => void run("analyze")}
        >
          Check tempo and key evidence
        </button>
        {project.parent_project_id && !vocalReady && (
          <button
            className="button secondary"
            disabled={
              disabled ||
              !!pending ||
              melody?.status === "processing" ||
              melody?.status === "queued"
            }
            onClick={() => void run("vocals")}
          >
            Transcribe vocal melody
          </button>
        )}
        {melody && (
          <a className="button secondary" href={`#/projects/${melody.id}`}>
            Review vocal melody · {melody.status}
          </a>
        )}
        {melody && !vocalReady && (
          <button
            className="button secondary"
            disabled={!!pending}
            onClick={() => setReload((value) => value + 1)}
          >
            Refresh available editions
          </button>
        )}
      </div>
      {analysis && (
        <div className="notice">
          <p>
            <strong>Estimated musical settings:</strong>{" "}
            {analysis.suggested_settings.tempo_bpm
              ? `${analysis.suggested_settings.tempo_bpm} BPM`
              : "Tempo unresolved"}
            ;{" "}
            {analysis.suggested_settings.key_signature !== undefined &&
            analysis.key_candidates[0]
              ? `${analysis.key_candidates[0].tonic} ${analysis.key_candidates[0].mode}`
              : "key unresolved"}
            . The timing phase is provisional; meter and the first bar still
            need musical review.
          </p>
          <label>
            <input
              type="checkbox"
              checked={useEstimates}
              disabled={
                !!pending || analysis.status === "insufficient_evidence"
              }
              onChange={(event) => setUseEstimates(event.target.checked)}
            />{" "}
            Use these estimates for new editions only
          </label>
          <p className="input-help">
            {analysis.warnings?.slice(0, 3).join(" ")}
          </p>
        </div>
      )}
      <label className="piano-edition-register">
        Solo melody register
        <select
          aria-label="Solo melody register"
          value={melodyOctave}
          disabled={disabled || !!pending}
          onChange={(event) =>
            setMelodyOctave(Number(event.target.value) as 0 | 12 | -12)
          }
        >
          <option value={12}>One octave higher for piano</option>
          <option value={0}>Original vocal register</option>
          <option value={-12}>One octave lower for piano</option>
        </select>
      </label>
      <p className="input-help">
        This is an explicit piano arrangement choice. It changes the solo
        melody’s octave while retaining its rhythm and pitch classes; the source
        vocal notes and singer’s cue remain unchanged. Locked vocal corrections
        require the original register.
      </p>
      <div className="edition-options">
        {editions.map((edition) => (
          <article key={edition.mode}>
            <h3>{edition.title}</h3>
            <p>{edition.detail}</p>
            <button
              className="button secondary"
              disabled={
                disabled ||
                !!pending ||
                (edition.mode === "solo_piano" && !vocalReady)
              }
              onClick={() => void run(edition.mode)}
            >
              Create{" "}
              {edition.mode === "faithful"
                ? "transcription"
                : edition.mode === "accompaniment"
                  ? "accompaniment"
                  : "solo piano"}
            </button>
            {edition.mode === "solo_piano" && !vocalReady && (
              <p className="input-help">
                Requires a completed vocal transcription from the same mixed
                recording.
              </p>
            )}
          </article>
        ))}
      </div>
      <button
        className="button primary"
        disabled={disabled || !!pending || !vocalReady}
        onClick={() => void run("all")}
      >
        {pending && <LoaderCircle size={17} className="spin" />}{" "}
        {pending ? "Preparing…" : "Create all three editions"}
      </button>
      {disabled && (
        <p className="input-help">
          Save the current notes and notation settings before deriving editions.
        </p>
      )}
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      {saved.length > 0 && (
        <ul className="edition-links">
          {saved.map((value) => (
            <li key={value.id}>
              <a href={`#/projects/${value.id}`}>{value.title}</a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

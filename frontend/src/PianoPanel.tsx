import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Hand,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { request } from "./api";
import type { PianoSettings, PlayabilityState, ProjectDetail } from "./api";

const defaults: PianoSettings = {
  profile: "comfortable_accompaniment",
  left_reach: 9,
  right_reach: 9,
  max_keys_per_hand: 5,
  target_keys_per_hand: 3,
  max_jump_semitones: 12,
  max_jump_semitones_per_second: 28,
  attack_tolerance_seconds: 0.035,
  include_vocal_cue: false,
};

export default function PianoPanel({
  project,
  savedRevision,
  unsaved,
  busy,
  onSelect,
}: {
  project: ProjectDetail;
  savedRevision: number;
  unsaved: boolean;
  busy: boolean;
  onSelect?: (ids: string[]) => void;
}) {
  const [settings, setSettings] = useState<PianoSettings>(() => ({
    ...defaults,
    ...project.arrangement?.settings,
  }));
  const [state, setState] = useState<PlayabilityState | null>(null);
  const [action, setAction] = useState<
    "loading" | "checking" | "creating" | null
  >("loading");
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [issuePage, setIssuePage] = useState(0);
  const requestId = useRef(0);
  const initialized = useRef(false);
  const accompaniment = project.output_mode === "accompaniment";
  const base = `/projects/${encodeURIComponent(project.id)}`;
  useEffect(() => {
    const controller = new AbortController();
    const generation = ++requestId.current;
    setAction("loading");
    setError("");
    void request<PlayabilityState>(`${base}/playability`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted || generation !== requestId.current)
          return;
        setState(result);
        setIssuePage(0);
        if (!initialized.current) {
          setSettings({ ...defaults, ...result.settings });
          initialized.current = true;
        }
      })
      .catch((reason: Error) => {
        if (!controller.signal.aborted && generation === requestId.current)
          setError(reason.message);
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === requestId.current)
          setAction(null);
      });
    return () => controller.abort();
  }, [base, project.notes_revision, savedRevision, reload]);

  const run = async (create: boolean) => {
    if (action || busy || unsaved || !state) return;
    const generation = ++requestId.current;
    setAction(create ? "creating" : "checking");
    setError("");
    try {
      const result = await request<PlayabilityState | ProjectDetail>(
        `${base}/${create ? "accompaniment" : "playability/preview"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            notes_revision: project.notes_revision,
            score_revision: savedRevision,
            piano_settings: settings,
          }),
        },
      );
      if (generation !== requestId.current) return;
      if (create && "id" in result) {
        window.location.hash = `#/projects/${result.id}`;
      } else if ("report" in result) {
        setState(result);
        setIssuePage(0);
      }
    } catch (reason) {
      if (generation === requestId.current) setError((reason as Error).message);
    } finally {
      if (generation === requestId.current) setAction(null);
    }
  };
  const report = state?.report;
  const issues = report?.issues ?? [];
  const page = Math.min(
    issuePage,
    Math.max(0, Math.ceil(issues.length / 8) - 1),
  );
  const settingsChanged =
    state && JSON.stringify(settings) !== JSON.stringify(state.settings);
  const blocked = !!action || busy || unsaved || !state;
  return (
    <section
      className="card piano-panel"
      aria-label="Piano accompaniment and playability"
    >
      <div className="panel-heading">
        <h2>
          <Hand size={20} />{" "}
          {accompaniment
            ? project.arrangement?.edition_mode === "solo_piano"
              ? "Solo piano review"
              : "Accompaniment review"
            : "Make a piano accompaniment"}
        </h2>
        <span>{accompaniment ? "DERIVED ARRANGEMENT" : "TWO-HAND REVIEW"}</span>
      </div>
      <p className="notation-intro">
        {accompaniment
          ? "This is a separate arrangement derived from the saved transcription. "
          : "Create a separate piano arrangement from these notes. "}
        Choose a lighter texture with compact voicings and smoother movement.
        Melody, bass, marked riffs and locked corrections stay protected. Each
        reduction is an editorial arrangement with the original notes retained.
      </p>
      {!!project.arrangement?.melody_octave_shift && (
        <p className="notice">
          The solo melody is placed{" "}
          {project.arrangement.melody_octave_shift > 0
            ? "one octave higher"
            : "one octave lower"}{" "}
          for piano. This editorial register choice preserves the original vocal
          transcription separately.
        </p>
      )}
      <fieldset className="piano-settings" disabled={!!action || busy}>
        <legend>Comfortable accompaniment</legend>
        <label>
          Held-key target per hand
          <select
            aria-label="Held-key target per hand"
            value={settings.target_keys_per_hand}
            onChange={(event) =>
              setSettings((current) => ({
                ...current,
                target_keys_per_hand: Number(event.target.value),
              }))
            }
          >
            {[1, 2, 3, 4, 5].map((count) => (
              <option key={count} value={count}>
                {count} {count === 1 ? "key" : "keys"}
                {count === 3 ? " · lighter default" : ""}
              </option>
            ))}
          </select>
        </label>
        {(["left", "right"] as const).map((hand) => (
          <label key={hand}>
            {hand === "left" ? "Left-hand reach" : "Right-hand reach"}
            <select
              aria-label={
                hand === "left" ? "Left-hand reach" : "Right-hand reach"
              }
              value={settings[`${hand}_reach`]}
              onChange={(event) =>
                setSettings((current) => ({
                  ...current,
                  [`${hand}_reach`]: Number(event.target.value),
                }))
              }
            >
              {[5, 6, 7, 8, 9, 10, 11, 12].map((reach) => (
                <option key={reach} value={reach}>
                  {reach} semitones{reach === 9 ? " · default" : ""}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label className="piano-cue-choice">
          <input
            type="checkbox"
            checked={settings.include_vocal_cue}
            onChange={(event) =>
              setSettings((current) => ({
                ...current,
                include_vocal_cue: event.target.checked,
              }))
            }
          />{" "}
          Include marked vocal notes on a separate cue staff
        </label>
      </fieldset>
      <p className="input-help">
        The target includes keys already being held. Protected notes can exceed
        it and remain flagged for review. Five fingers alone do not establish
        comfort: checks also consider reach, keyboard shape and available time
        between attacks. Repeated figures present in the source are preserved.
      </p>
      <div className="piano-actions">
        <button
          type="button"
          className="button primary"
          disabled={blocked}
          onClick={() => void run(true)}
        >
          {action === "creating" ? (
            <LoaderCircle size={17} className="spin" />
          ) : (
            <ArrowRight size={17} />
          )}
          {action === "creating"
            ? "Preparing separate arrangement…"
            : accompaniment
              ? "Simplify and smooth movement"
              : "Create playable accompaniment"}
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={blocked}
          onClick={() => void run(false)}
        >
          <RefreshCw
            size={16}
            className={action === "checking" ? "spin" : ""}
          />{" "}
          Check saved score
        </button>
        {project.arrangement_source_id && (
          <a
            className="button secondary"
            href={`#/projects/${project.arrangement_source_id}`}
          >
            Open source version
          </a>
        )}
      </div>
      <p className="input-help">
        Each revoice creates another saved project. The source and locked notes
        are preserved. Exact transposition is checked again after saving;
        revoicing is a separate action. No finger numbers are guessed.
      </p>
      {unsaved && (
        <p className="notice" role="status">
          Save your note and musical-setting changes before checking or creating
          an arrangement. Checks below describe the saved score.
        </p>
      )}
      {error && (
        <div className="notice error" role="alert">
          <p>{error}</p>
          <button
            type="button"
            className="button secondary small"
            disabled={!!action}
            onClick={() => setReload((n) => n + 1)}
          >
            Retry playability checks
          </button>
        </div>
      )}
      {action === "loading" && (
        <p className="input-help" role="status">
          Checking saved-score reach, held notes and movement…
        </p>
      )}
      {report && (
        <div className="piano-report" aria-live="polite">
          <strong>
            {report.summary.passed_automated_constraints &&
            report.density?.target_met !== false ? (
              <>
                <CheckCircle2 size={17} /> Selected automated constraints
                satisfied
              </>
            ) : report.summary.error_count === 0 &&
              report.density?.target_met === false ? (
              "Texture exceeds the selected held-key target"
            ) : report.summary.error_count === 0 ? (
              "Configured hard constraints met; review warnings below"
            ) : (
              `${report.summary.error_count} constraint issue${report.summary.error_count === 1 ? "" : "s"} need review`
            )}
          </strong>
          <p>
            {report.summary.warning_count} heuristic warning
            {report.summary.warning_count === 1 ? "" : "s"}. This is an
            automated assessment, not pianist validation.
          </p>
          {settingsChanged && (
            <p className="input-help">
              Piano settings changed. Use Check saved score to refresh this
              assessment.
            </p>
          )}
          {report.density && (
            <div className="piano-density-summary">
              <p>
                Held keys at score attacks: left maximum{" "}
                {report.density.max_left_keys}, right maximum{" "}
                {report.density.max_right_keys}. Target:{" "}
                {report.density.target_keys_per_hand} per hand.
              </p>
              <p>
                {report.density.target_met
                  ? "The saved score meets this density target."
                  : `${report.density.over_target_frames} hand positions exceed the target. Simplification removes optional inner notes where the protected lines allow it.`}
              </p>
              {report.density.protected_exceptions.length > 0 && (
                <p className="input-help">
                  Some dense positions contain protected notes. Review their
                  roles or locks before allowing further reduction; they have
                  not been silently removed.
                </p>
              )}
            </div>
          )}
          {issues.length > 0 && (
            <ol className="piano-issues" start={page * 8 + 1}>
              {issues.slice(page * 8, page * 8 + 8).map((issue, index) => (
                <li
                  key={`${issue.code}:${issue.measure}:${issue.hand}:${index}`}
                >
                  <div>
                    <strong>
                      Measure {issue.measure === 0 ? "pickup" : issue.measure} ·{" "}
                      {issue.hand === "both"
                        ? "both hands"
                        : issue.hand === "unassigned"
                          ? "unassigned hand"
                          : `${issue.hand} hand`}
                    </strong>
                    <span className="piano-issue-severity">
                      {issue.severity}
                    </span>
                  </div>
                  <p>{issue.detail}</p>
                  {issue.note_ids.length > 0 && onSelect && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        onSelect(issue.note_ids);
                        document
                          .getElementById("piano-note-details")
                          ?.scrollIntoView({
                            behavior: "smooth",
                            block: "center",
                          });
                      }}
                    >
                      Select {issue.note_ids.length} affected note
                      {issue.note_ids.length === 1 ? "" : "s"}
                    </button>
                  )}
                </li>
              ))}
            </ol>
          )}
          {issues.length > 8 && (
            <div className="piano-issue-pages">
              <span>
                {page * 8 + 1}–{Math.min(issues.length, page * 8 + 8)} of{" "}
                {issues.length} issues
              </span>
              <button
                type="button"
                className="button secondary small"
                disabled={!page}
                onClick={() => setIssuePage(page - 1)}
              >
                Previous issues
              </button>
              <button
                type="button"
                className="button secondary small"
                disabled={(page + 1) * 8 >= issues.length}
                onClick={() => setIssuePage(page + 1)}
              >
                Next issues
              </button>
            </div>
          )}
          <details>
            <summary>What these checks can and cannot establish</summary>
            <ul>
              {report.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </section>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  AudioLines,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  FileMusic,
  FolderOpen,
  Headphones,
  LoaderCircle,
  LogOut,
  Mic,
  Music2,
  Plus,
  Radio,
  RefreshCw,
  ShieldCheck,
  Square,
} from "lucide-react";
import AudioInput from "./AudioInput";
import AuthBoundary from "./AuthBoundary";
import DeleteProject from "./DeleteProject";
import ReviewWorkspace from "./ReviewWorkspace";
import MixedWorkspace from "./MixedWorkspace";
import { formatDate, formatDuration, request } from "./api";
import type {
  AuthSession,
  Health,
  Project,
  ProjectDetail,
  Status,
} from "./api";

const active = (status: Status) =>
  status === "queued" || status === "processing";
const statusLabel: Record<Status, string> = {
  queued: "In queue",
  processing: "Processing",
  completed: "Ready to review",
  failed: "Needs attention",
  canceled: "Canceled",
};
function Badge({ status, mixed = false }: { status: Status; mixed?: boolean }) {
  return (
    <span className={`badge ${status}`}>
      {active(status) && <LoaderCircle size={12} className="spin" />}
      {mixed && status === "completed" ? "Choose a part" : statusLabel[status]}
    </span>
  );
}
function ErrorNotice({ children }: { children: React.ReactNode }) {
  return (
    <div className="notice error" role="alert">
      {children}
    </div>
  );
}
function useRoute() {
  const [route, setRoute] = useState(location.hash.slice(1) || "/");
  useEffect(() => {
    const update = () => setRoute(location.hash.slice(1) || "/");
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

export default function App() {
  return (
    <AuthBoundary>
      {(session, logout) => (
        <Workspace
          key={session.user?.id ?? "local"}
          session={session}
          logout={logout}
        />
      )}
    </AuthBoundary>
  );
}

function Workspace({
  session,
  logout,
}: {
  session: AuthSession;
  logout: () => Promise<void>;
}) {
  const route = useRoute();
  const [health, setHealth] = useState<Health | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    try {
      setProjects(await request<Project[]>("/projects"));
      setLoadError("");
    } catch (e) {
      setLoadError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    let live = true;
    const check = () =>
      request<Health>("/health")
        .then((value) => {
          if (live) setHealth(value);
        })
        .catch(() => {
          if (live) setHealth(null);
        });
    void check();
    const timer = setInterval(() => void check(), 10000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);
  const projectId = route.startsWith("/projects/")
    ? route.slice("/projects/".length)
    : null;
  const ready =
    !!health?.worker.online &&
    health.capabilities.ffmpeg &&
    health.capabilities.transcription;
  return (
    <div className="app">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to main content
      </a>
      <aside className="sidebar">
        <a className="brand" href="#/" aria-label="Scorecraft home">
          <span className="brand-mark">
            <AudioLines size={24} />
          </span>
          scorecraft<span className="brand-dot">.</span>
        </a>
        <div className="workspace-label">YOUR WORKSPACE</div>
        <nav aria-label="Main navigation">
          <a
            href="#/"
            className={route === "/" ? "nav-link selected" : "nav-link"}
          >
            <Plus size={19} />
            New transcription
          </a>
          <a
            href="#/library"
            className={
              route === "/library" || projectId
                ? "nav-link selected"
                : "nav-link"
            }
          >
            <FolderOpen size={19} />
            My projects<span className="nav-count">{projects.length}</span>
          </a>
        </nav>
        <div className="sidebar-bottom">
          <div className="local-card">
            <span className="local-icon">
              <ShieldCheck size={18} />
            </span>
            <div>
              <strong>A studio of your own</strong>
              <p>
                {session.authentication_required
                  ? "Private recordings and scores, protected by your account."
                  : "Your recordings stay on this computer."}
              </p>
            </div>
          </div>
          <a
            className={route === "/guide" ? "nav-link selected" : "nav-link"}
            href="#/guide"
          >
            <CircleHelp size={18} />
            Getting started
            <ArrowRight size={15} />
          </a>
          <div className="profile">
            <span className="avatar">
              {session.user?.username.slice(0, 1).toUpperCase() ?? "L"}
            </span>
            <div>
              <strong>{session.user?.username ?? "Local workspace"}</strong>
              <small>
                {session.authentication_required
                  ? "Private account"
                  : "Single-user edition"}
              </small>
            </div>
            <span className="tiny-dot" />
          </div>
        </div>
      </aside>
      <div className="main-wrap">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <ChevronRight size={14} />
            <span>
              {projectId
                ? "Project"
                : route === "/library"
                  ? "My projects"
                  : route === "/guide"
                    ? "Getting started"
                    : "New transcription"}
            </span>
          </div>
          <div className="topbar-actions">
            <span
              className={`connection ${ready ? "online" : ""}`}
              title={
                health
                  ? "API connected. Analysis requires FFmpeg, Basic Pitch and a running worker."
                  : "Connect to the studio API."
              }
            >
              <span />
              {ready
                ? "Studio ready"
                : health
                  ? "API connected"
                  : "Connecting to API"}
            </span>
            {session.authentication_required && (
              <button
                className="button secondary sign-out"
                onClick={() => void logout()}
              >
                <LogOut size={15} /> Sign out
              </button>
            )}
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          {projectId ? (
            <Detail
              key={projectId}
              id={projectId}
              health={health}
              onChange={refresh}
            />
          ) : route === "/guide" ? (
            <Guide />
          ) : route === "/library" ? (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">YOUR COLLECTION</span>
                  <h1>My projects</h1>
                  <p>Every recording. Every musical idea. All in one place.</p>
                </div>
                <a className="button primary" href="#/">
                  <Plus size={17} />
                  New transcription
                </a>
              </div>
              <ProjectList
                projects={projects}
                loading={loading}
                error={loadError}
                refresh={refresh}
              />
            </>
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">FROM SOUND TO SCORE</span>
                  <h1>Your music, on paper.</h1>
                  <p>Capture a melody. Find the notes. Make it yours.</p>
                </div>
                <span className="edition-tag">
                  <span />
                  LOCAL STUDIO
                </span>
              </div>
              <div className="create-grid">
                <AudioInput health={health} onCreated={refresh} />
                <aside className="tips-panel">
                  <span className="tips-icon">
                    <Headphones size={24} strokeWidth={1.5} />
                  </span>
                  <h2>
                    A little clarity goes
                    <br />a long way.
                  </h2>
                  <p>
                    Start with one voice or one instrument for the clearest
                    first draft.
                  </p>
                  <div className="tip">
                    <span>01</span>
                    <div>
                      <strong>Keep it focused</strong>
                      <p>
                        Choose Solo for one performance, or Full song to
                        separate a mix and choose a part.
                      </p>
                    </div>
                  </div>
                  <div className="tip">
                    <span>02</span>
                    <div>
                      <strong>Find a quiet space</strong>
                      <p>
                        Less background noise means more of your music comes
                        through.
                      </p>
                    </div>
                  </div>
                  <div className="tip">
                    <span>03</span>
                    <div>
                      <strong>Make room for review</strong>
                      <p>
                        Transcription is a starting point. Timing and pitches
                        may need correction.
                      </p>
                    </div>
                  </div>
                  <a href="#/guide" className="text-link">
                    A guide to your first score
                    <ArrowRight size={15} />
                  </a>
                </aside>
              </div>
              <div className="flow-strip">
                <div>
                  <span className="flow-number">1</span>
                  <strong>Bring your audio</strong>
                  <span>Upload or record</span>
                </div>
                <ChevronRight size={16} />
                <div>
                  <span className="flow-number">2</span>
                  <strong>Find the notes</strong>
                  <span>Analyze in the private worker</span>
                </div>
                <ChevronRight size={16} />
                <div>
                  <span className="flow-number">3</span>
                  <strong>Review the draft</strong>
                  <span>Listen and inspect</span>
                </div>
              </div>
              <section className="recent">
                <div className="section-heading">
                  <h2>
                    Recent projects<span>{projects.length}</span>
                  </h2>
                  <a href="#/library" className="text-link">
                    View all
                    <ArrowRight size={15} />
                  </a>
                </div>
                <ProjectList
                  projects={projects.slice(0, 4)}
                  loading={loading}
                  error={loadError}
                  refresh={refresh}
                  compact
                />
              </section>
            </>
          )}
          <footer>
            <span>
              <ShieldCheck size={14} />
              Private recordings. Review before exporting.
            </span>
            <span>Scorecraft · Foundation preview</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

function ProjectList({
  projects,
  loading,
  error,
  refresh,
  compact = false,
}: {
  projects: Project[];
  loading: boolean;
  error: string;
  refresh: () => Promise<void>;
  compact?: boolean;
}) {
  if (error)
    return (
      <div className="empty-projects">
        <span className="empty-icon">
          <Radio size={22} />
        </span>
        <div>
          <h3>Connect your local studio</h3>
          <p>{error}</p>
        </div>
        <button
          className="button secondary small"
          onClick={() => void refresh()}
        >
          <RefreshCw size={14} />
          Reconnect
        </button>
      </div>
    );
  if (loading)
    return (
      <div className="empty-projects" role="status">
        <LoaderCircle size={22} className="spin" />
        <p>Loading your projects…</p>
      </div>
    );
  if (!projects.length)
    return (
      <div className="empty-projects">
        <span className="empty-icon">
          <FileMusic size={24} strokeWidth={1.4} />
        </span>
        <div>
          <h3>Your next idea starts here</h3>
          <p>
            {compact
              ? "Create your first transcription and it will appear here."
              : "Upload a recording or capture a melody to start your collection."}
          </p>
        </div>
        {!compact && (
          <a href="#/" className="button secondary small">
            Create a project
            <ArrowRight size={14} />
          </a>
        )}
      </div>
    );
  return (
    <div className="project-list">
      {projects.map((project) => (
        <a
          key={project.id}
          className="project-row"
          href={`#/projects/${project.id}`}
        >
          <span className="project-icon">
            {project.source === "microphone" ? (
              <Mic size={21} />
            ) : (
              <FileMusic size={21} />
            )}
          </span>
          <div className="project-name">
            <strong>{project.title}</strong>
            <span>
              {project.parent_project_id
                ? `${project.stem_category} part`
                : project.input_mode === "mixed"
                  ? "Mixed recording"
                  : project.source === "microphone"
                    ? "Microphone"
                    : "Audio upload"}{" "}
              · {formatDate(project.created_at)}
            </span>
          </div>
          <span className="project-duration">
            {formatDuration(project.duration_seconds)}
          </span>
          <Badge
            status={project.status}
            mixed={project.input_mode === "mixed"}
          />
          <ChevronRight size={17} />
        </a>
      ))}
    </div>
  );
}

function Detail({
  id,
  health,
  onChange,
}: {
  id: string;
  health: Health | null;
  onChange: () => Promise<void>;
}) {
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState("");
  const [jobAction, setJobAction] = useState<"retry" | "cancel" | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [originalPlaybackError, setOriginalPlaybackError] = useState(false);
  const retryRequest = useRef<{ attempt: string; key: string } | null>(null);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const value = await request<ProjectDetail>(
          `/projects/${encodeURIComponent(id)}`,
        );
        if (live) {
          setProject(value);
          setError("");
          if (active(value.status)) timer = setTimeout(() => void load(), 2000);
        }
      } catch (e) {
        if (live) {
          setError((e as Error).message);
          timer = setTimeout(() => void load(), 5000);
        }
      }
    };
    void load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [id, reloadTick]);
  const updateJob = async (action: "retry" | "cancel") => {
    if (jobAction) return;
    setJobAction(action);
    setError("");
    const attempt = project?.jobs[0]?.id ?? id;
    if (action === "retry" && retryRequest.current?.attempt !== attempt)
      retryRequest.current = { attempt, key: crypto.randomUUID() };
    try {
      setProject(
        await request<ProjectDetail>(`/projects/${id}/${action}`, {
          method: "POST",
          headers:
            action === "retry" && retryRequest.current
              ? { "Idempotency-Key": retryRequest.current.key }
              : undefined,
        }),
      );
      setReloadTick((value) => value + 1);
      await onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setJobAction(null);
    }
  };
  if (!project)
    return (
      <>
        <a href="#/library" className="back-link">
          <ArrowLeft size={16} />
          All projects
        </a>
        {error ? (
          <ErrorNotice>{error}</ErrorNotice>
        ) : (
          <div className="loading-detail">
            <LoaderCircle className="spin" />
            Opening your project…
          </div>
        )}
      </>
    );
  const latest = project.jobs[0];
  const mixed = project.input_mode === "mixed";
  const sourceLabel = project.parent_project_id
    ? `Separated ${project.stem_category} stem`
    : "Original recording";
  return (
    <>
      <a
        href={
          project.arrangement_source_id
            ? `#/projects/${project.arrangement_source_id}`
            : project.parent_project_id
              ? `#/projects/${project.parent_project_id}`
              : "#/library"
        }
        className="back-link"
      >
        <ArrowLeft size={16} />
        {project.arrangement_source_id
          ? "Back to source version"
          : project.parent_project_id
            ? "Back to mixed recording"
            : "All projects"}
      </a>
      <div
        className={`page-heading detail-heading ${project.status === "completed" ? "completed-detail-heading" : ""}`}
      >
        <div>
          <span className="eyebrow">
            {project.arrangement?.edition_mode === "solo_piano"
              ? "SOLO PIANO · DERIVED ARRANGEMENT"
              : project.arrangement?.edition_mode === "faithful"
                ? "FAITHFUL INSTRUMENTAL TRANSCRIPTION"
                : project.output_mode === "accompaniment"
                  ? "PIANO ACCOMPANIMENT · DERIVED ARRANGEMENT"
                  : mixed
                    ? "MIXED RECORDING"
                    : project.parent_project_id
                      ? "PART TRANSCRIPTION"
                      : "TRANSCRIPTION PROJECT"}
          </span>
          <h1>{project.title}</h1>
          <p>
            {project.original_filename} <span className="dot-separator">·</span>{" "}
            Created {formatDate(project.created_at)}
          </p>
        </div>
        <div className="detail-actions">
          <Badge status={project.status} mixed={mixed} />
          <DeleteProject project={project} onDeleted={onChange} />
        </div>
      </div>
      {project.status === "completed" && project.notes !== null && (
        <ReviewWorkspace
          key={project.id}
          project={project}
          health={health}
          onSaved={(value) => {
            setProject(value);
            void onChange();
          }}
        />
      )}
      {project.parent_project_id && (
        <div className="notice part-provenance">
          This score estimates the <strong>{project.stem_category}</strong> part
          from a separated stem.
          {project.stem_category === "vocals"
            ? " Vocal transcription follows one predominant melody, including vibrato cleanup; it does not recover every harmony."
            : project.stem_category === "bass"
              ? " Bass transcription follows one estimated line."
              : " Other pitched material is experimental and may contain several instruments."}{" "}
          Part time 0.00 s maps to{" "}
          {(project.source_origin_seconds ?? 0).toFixed(2)} s in the original
          song. Review leakage, pitch and rhythm before exporting. No
          accompaniment is generated.
        </div>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
      <details
        className="review-source-details"
        open={project.status !== "completed"}
      >
        <summary>Source recording and processing status</summary>
        <div className="detail-grid">
          <section className="card audio-card">
            <div className="panel-heading">
              <h2>
                <AudioLines size={19} />
                {sourceLabel}
              </h2>
              <span>
                {project.parent_project_id
                  ? "SEPARATED PART"
                  : project.source === "microphone"
                    ? "MICROPHONE"
                    : "UPLOAD"}
              </span>
            </div>
            <audio
              controls
              preload="metadata"
              src={`/api/projects/${id}/audio`}
              aria-label={sourceLabel}
              onError={() => setOriginalPlaybackError(true)}
            />
            {originalPlaybackError && (
              <div className="notice" role="status">
                This browser cannot play the original file. Use the normalized
                selected clip below once decoding finishes; your original
                recording is still preserved.
              </div>
            )}
            <div className="audio-metadata">
              <div>
                <span>
                  {project.parent_project_id
                    ? "Stem duration"
                    : "Original duration"}
                </span>
                <strong>
                  {formatDuration(
                    project.original_duration_seconds ??
                      project.duration_seconds,
                  )}
                </strong>
              </div>
              <div>
                <span>Saved notes</span>
                <strong>{project.note_count ?? "—"}</strong>
              </div>
              <div>
                <span>Engine</span>
                <strong>
                  {mixed
                    ? "Source separation"
                    : project.transcription_profile === "vocals" ||
                        project.transcription_profile === "bass"
                      ? "pYIN melody / line"
                      : "Basic Pitch"}
                </strong>
              </div>
            </div>
            <div className="saved-clip">
              <h3>Selected clip</h3>
              <p>
                {Number(project.clip_start_seconds ?? 0).toFixed(2)} s to{" "}
                {project.clip_end_seconds == null
                  ? "the end of the original recording"
                  : `${project.clip_end_seconds.toFixed(2)} s`}
                .
                {project.clip_duration_seconds != null &&
                  ` ${project.clip_duration_seconds.toFixed(2)} seconds selected.`}{" "}
                {project.parent_project_id
                  ? "The separated source audio is preserved."
                  : "The original file is preserved."}
              </p>
              {project.analysis_available ? (
                <>
                  <audio
                    controls
                    preload="metadata"
                    src={`/api/projects/${id}/analysis-audio`}
                    aria-label="Selected analysis clip"
                  />
                  <p>
                    Listen to the exact normalized clip used for analysis. Note
                    times below start at zero within this clip.
                  </p>
                </>
              ) : (
                <p>
                  The selected playback copy becomes available after the server
                  validates and decodes your recording.
                </p>
              )}
              <div className="saved-hints" aria-label="Saved musical hints">
                {project.instrument_hint && (
                  <span>Instrument: {project.instrument_hint}</span>
                )}
                <span>
                  Tempo: {project.tempo_hint ?? 120} BPM{" "}
                  {project.tempo_hint ? "hint" : "default"}
                </span>
                <span>
                  Meter: {project.time_signature_hint ?? "4/4"}{" "}
                  {project.time_signature_hint ? "hint" : "default"}
                </span>
              </div>
            </div>
          </section>
          <section className="card status-card">
            <h2>Processing status</h2>
            <div className={`job-state ${project.status}`}>
              <span>
                {project.status === "completed" ? (
                  <Check size={23} />
                ) : project.status === "canceled" ? (
                  <Square size={20} />
                ) : project.status === "failed" ? (
                  <CircleHelp size={23} />
                ) : (
                  <LoaderCircle size={23} className="spin" />
                )}
              </span>
              <div>
                <strong>
                  {mixed && project.status === "completed"
                    ? "Stems ready to audition"
                    : statusLabel[project.status]}
                </strong>
                <p>
                  {project.status === "queued"
                    ? health?.worker.online
                      ? "Your recording is waiting for analysis."
                      : "Waiting for an analysis worker to start."
                    : project.status === "completed"
                      ? mixed
                        ? "Listen to the separated stems below, then choose a pitched part to transcribe."
                        : "Your draft is ready for a careful listen."
                      : project.status === "processing"
                        ? (
                            latest?.stage || "Analyzing your recording"
                          ).replaceAll("_", " ")
                        : project.status === "canceled"
                          ? "Analysis was canceled. The original recording remains saved."
                          : "The recording is saved. Resolve the issue below, then retry."}
                </p>
              </div>
            </div>
            {active(project.status) && health && !health.worker.online && (
              <div className="notice" role="status">
                The analysis worker is offline. Start it in a separate terminal
                to continue. Interrupted jobs become retryable when the worker
                restarts.
              </div>
            )}
            {latest?.error && <ErrorNotice>{latest.error}</ErrorNotice>}
            {active(project.status) && (
              <button
                className="button secondary small"
                disabled={jobAction !== null}
                onClick={() => void updateJob("cancel")}
              >
                <Square size={14} />
                {jobAction === "cancel"
                  ? "Canceling…"
                  : mixed
                    ? "Cancel separation"
                    : "Cancel transcription"}
              </button>
            )}
            {(project.status === "failed" || project.status === "canceled") && (
              <button
                className="button secondary small"
                disabled={jobAction !== null || project.retries_remaining === 0}
                onClick={() => void updateJob("retry")}
              >
                <RefreshCw
                  size={15}
                  className={jobAction === "retry" ? "spin" : ""}
                />
                {jobAction === "retry"
                  ? "Retrying…"
                  : mixed
                    ? "Retry separation"
                    : "Retry transcription"}
              </button>
            )}
            <p className="retry-limit">
              Attempt {project.jobs.length} of {project.max_attempts ?? 3}.{" "}
              {project.retries_remaining === 0
                ? "The attempt limit has been reached."
                : `${project.retries_remaining ?? Math.max(0, 3 - project.jobs.length)} retries remaining.`}
            </p>
          </section>
        </div>
      </details>
      {mixed && project.status === "completed" && (
        <MixedWorkspace project={project} onChange={onChange} />
      )}

      {!mixed && (
        <section className="card score-card">
          <div className="panel-heading">
            <h2>
              <FileMusic size={19} />
              Preserved performance
            </h2>
            <span>
              {project.status === "completed"
                ? `${project.note_count ?? 0} PERFORMANCE NOTES`
                : "AWAITING ANALYSIS"}
            </span>
          </div>
          {project.status === "completed" && project.notes !== null ? (
            <p className="input-help">
              Your preserved performance files remain separate from the current
              score. Use the review workspace above to edit, play, and save
              notation.
            </p>
          ) : (
            <div className="score-empty">
              <span className="empty-score-icon">
                <Music2 size={37} strokeWidth={1.2} />
              </span>
              <h3>
                {project.status === "canceled"
                  ? "Transcription canceled"
                  : "A little music, waiting to take shape."}
              </h3>
              <p>
                {project.status === "canceled"
                  ? "Your original recording is saved. Retry when you are ready."
                  : "Detected notes will appear here after successful analysis.\nYour original recording is always available above."}
              </p>
            </div>
          )}
          {project.status === "completed" &&
            !!project.notes?.length &&
            project.exports.length === 0 && (
              <div className="notice">
                Draft exports are unavailable for this run. Check that music21
                is installed and inspect the worker log. Your detected notes are
                saved.
              </div>
            )}
          {project.exports.length > 0 && (
            <div className="export-row">
              <div>
                <strong>Performance exports</strong>
                <p>
                  Raw performance MIDI preserves the original detected events;
                  edited performance MIDI uses saved note times. Use the musical
                  score above for quantized MusicXML and score MIDI.
                </p>
              </div>
              {project.exports
                .filter((format) => ["raw-midi", "midi"].includes(format))
                .map((format) => (
                  <a
                    className="button secondary small"
                    key={format}
                    href={`/api/projects/${id}/exports/${format}`}
                    download
                  >
                    <ArrowDownToLine size={15} />
                    {format === "raw-midi"
                      ? "Raw performance MIDI"
                      : "Edited performance MIDI"}
                  </a>
                ))}
            </div>
          )}
          {(project.artifacts?.length ?? 0) > 0 && (
            <details className="artifact-files">
              <summary>Preserved analysis files</summary>
              <p>
                Raw activations and detected events are preserved separately
                from editable notes. Model activations are not calibrated
                accuracy probabilities.
              </p>
              <div>
                {(project.artifacts ?? [])
                  .filter((key) =>
                    [
                      "raw-model-output",
                      "raw-note-events",
                      "raw-performance",
                      "normalized-notes",
                      "manifest",
                    ].includes(key),
                  )
                  .map((key) => (
                    <a
                      className="button secondary small"
                      key={key}
                      href={`/api/projects/${id}/artifacts/${key}`}
                      download
                    >
                      <ArrowDownToLine size={14} />
                      {
                        {
                          "raw-model-output": "Raw model arrays (.npz)",
                          "raw-note-events": "Raw note events (.json)",
                          "raw-performance": "Raw performance MIDI",
                          "normalized-notes": "Normalized notes (.json)",
                          manifest: "Analysis manifest (.json)",
                        }[key]
                      }
                    </a>
                  ))}
              </div>
            </details>
          )}
          {project.cleanup && (
            <p className="input-help cleanup-summary">
              {project.transcription_profile === "vocals" ||
              project.transcription_profile === "bass" ? (
                <>
                  Cleanup: minimum note duration{" "}
                  {project.cleanup.minimum_note_seconds} s, with{" "}
                  {project.transcription_profile === "vocals"
                    ? "vocal"
                    : "bass"}{" "}
                  pitch-stability and fragment cleanup. Raw pitch tracks and
                  note events are retained. See the analysis manifest for the
                  actual processing settings.
                </>
              ) : (
                <>
                  Cleanup: minimum {project.cleanup.minimum_note_seconds} s;
                  fragment merge gap up to {project.cleanup.merge_gap_seconds}{" "}
                  s. Re-articulated notes stay separate. Original detections
                  remain available in the raw files.
                </>
              )}
            </p>
          )}
        </section>
      )}
      <details className="history">
        <summary>
          <Clock3 size={16} />
          Processing history · {project.jobs.length}{" "}
          {project.jobs.length === 1 ? "attempt" : "attempts"}
        </summary>
        {project.jobs.map((job) => (
          <div key={job.id}>
            <Badge status={job.status} />
            <span>{job.stage.replaceAll("_", " ")}</span>
            <time>{new Date(job.created_at).toLocaleString()}</time>
            {job.error && <p>{job.error}</p>}
          </div>
        ))}
      </details>
    </>
  );
}

function Guide() {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">A GOOD PLACE TO START</span>
          <h1>From a melody to a first draft.</h1>
          <p>
            A small, private workspace for listening more closely to your music.
          </p>
        </div>
      </div>
      <div className="guide-grid">
        <section className="card guide-card">
          <h2>Make your first project</h2>
          <ol>
            <li>
              <strong>Choose the recording mode.</strong>
              <p>
                Upload an audio file or use Record audio to capture your
                microphone. Stop recording, listen back, choose the clip start
                and end, and optionally add musical hints before creating your
                project. Choose Solo for one voice or instrument, or Full song /
                mixed recording to separate a finished mix. Nothing is
                transcribed while you record.
              </p>
            </li>
            <li>
              <strong>Let the analysis worker analyze it.</strong>
              <p>
                FFmpeg decodes the recording. Solo input goes directly to note
                detection. A mixed recording is separated into the model's
                actual stem categories; audition them and choose a vocal melody,
                bass line, or experimental pitched part. Drum notation is
                unsupported. The project page shows real stages and setup
                errors.
              </p>
            </li>
            <li>
              <strong>Listen and review.</strong>
              <p>
                Compare original audio with synthesized performance and score
                playback. Select notes to correct pitch, timing, part, or voice;
                split, merge, undo, and redo. Save the review to publish
                matching MusicXML, score MIDI, and paginated PDF exports. Raw
                performance MIDI stays separate. Automatic transcription can
                miss notes or add extras, especially with layered instruments.
              </p>
            </li>
          </ol>
          <a className="button primary" href="#/">
            Start a transcription
            <ArrowRight size={16} />
          </a>
        </section>
        <section className="card guide-card">
          <h2>Run your local studio</h2>
          <p>
            Follow the repository README to install the API, worker, and
            frontend dependencies.
          </p>
          <p>
            Start these in three terminals from the repository root after
            activating your Python environment:
          </p>
          <label>API</label>
          <code>
            python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
          </code>
          <label>Analysis worker</label>
          <code>python -m backend.app.worker</code>
          <label>Frontend</label>
          <code>cd frontend &amp;&amp; pnpm dev</code>
          <p className="guide-note">
            Analysis also needs FFmpeg and the optional Basic Pitch
            dependencies; mixed recordings also need the separation engine and
            its model weights. Local development binds to localhost. Hosted
            installations require configured HTTPS, accounts, private storage
            and the deployment checks documented in the repository.
          </p>
        </section>
      </div>
      <section className="card roadmap">
        <h2>A foundation, with room to grow.</h2>
        <div>
          <p>
            <span>01 · FOUNDATION</span>
            <strong>Capture & analyze</strong>Local projects, recordings, job
            history, and real note detection.
          </p>
          <p>
            <span>02 · AVAILABLE</span>
            <strong>Review & notate</strong>Performance editing, beat-based
            notation, adjustable rhythm settings, and a rendered score.
          </p>
          <p>
            <span>03 · AVAILABLE</span>
            <strong>Score exports</strong>MusicXML, score MIDI, and paginated
            PDF use the saved score revision. Compare synthesized playback with
            the original recording while reviewing.
          </p>
          <p>
            <span>04 · AVAILABLE</span>
            <strong>Explore full songs</strong>Separate sources, audition the
            actual stems, and choose one pitched part. “Other” can contain
            several instruments. Transcription estimates performed notes;
            arrangement creates new parts, and is not performed here.
          </p>
        </div>
      </section>
    </>
  );
}

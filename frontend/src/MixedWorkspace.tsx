import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  AudioLines,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { formatDuration, request } from "./api";
import type { ProjectDetail, SeparatedStem, SeparationState } from "./api";

function stemDescription(stem: SeparatedStem) {
  if (stem.category === "vocals")
    return "Estimate one predominant vocal melody. Harmonies, vibrato and breathy sounds can still cause missing or fragmented notes.";
  if (stem.category === "bass")
    return "Estimate one bass line. Check low pitches, repeated notes and leakage from kick drums by listening back.";
  if (stem.category === "drums")
    return "Listen to the percussion stem. Drum notation is unsupported; this audio is never sent to the pitched-note model.";
  if (stem.category === "other")
    return "Experimental pitched transcription. This stem can contain several instruments; it is not an isolated guitar or piano.";
  return (
    stem.detail ||
    "This is the category returned by the separation model. Listen before choosing a supported transcription profile."
  );
}

export default function MixedWorkspace({
  project,
  onChange,
}: {
  project: ProjectDetail;
  onChange: () => Promise<void>;
}) {
  const [result, setResult] = useState<SeparationState | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [pending, setPending] = useState<string | null>(null);
  const [playbackErrors, setPlaybackErrors] = useState<string[]>([]);
  const players = useRef(new Map<string, HTMLAudioElement>());
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void request<SeparationState>(`/projects/${project.id}/stems`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setResult(value);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError((reason as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [project.id, reload]);

  const transcribe = async (stem: SeparatedStem) => {
    if (pending || !stem.transcription_supported || stem.category === "drums")
      return;
    setPending(stem.category);
    setError("");
    players.current.forEach((player) => player.pause());
    try {
      const part = await request<ProjectDetail>(
        `/projects/${project.id}/stems/${encodeURIComponent(stem.category)}/transcribe`,
        { method: "POST" },
      );
      await onChange();
      if (mounted.current) location.hash = `/projects/${part.id}`;
    } catch (reason) {
      if (mounted.current) setError((reason as Error).message);
    } finally {
      if (mounted.current) setPending(null);
    }
  };

  return (
    <section className="card mixed-workspace" aria-label="Separated parts">
      <div className="panel-heading">
        <h2>
          <AudioLines size={19} /> Listen to the separated parts
        </h2>
        <span>CHOOSE A PART</span>
      </div>
      <p className="input-help">
        Each stem is an estimate and may contain sounds from other parts.
        Audition the actual model categories below, then choose a pitched part
        to transcribe into an editable score.
      </p>
      <div className="notice stem-alignment">
        All stems share the selected clip's timeline. Stem time 0.00 s
        corresponds to {project.clip_start_seconds.toFixed(2)} s in the original
        song.
        {project.clip_duration_seconds !== null &&
          ` The selected clip is ${project.clip_duration_seconds.toFixed(2)} seconds long.`}
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {loading && (
        <p role="status">
          <LoaderCircle size={16} className="spin" /> Loading separated parts…
        </p>
      )}
      {result && !result.stems.length && (
        <p className="notice">
          No published stems are available for this attempt. Check the
          processing history.
        </p>
      )}
      <div className="stem-grid">
        {result?.stems.map((stem) => {
          const supported =
            stem.transcription_supported && stem.category !== "drums";
          const child = result.children.find(
            (value) => value.id === stem.child_project_id,
          );
          const audioURL = `/api/projects/${project.id}/stems/${encodeURIComponent(stem.category)}/audio`;
          return (
            <article
              className="stem-card"
              key={stem.id}
              data-stem-category={stem.category}
            >
              <div className="stem-card-heading">
                <h3>{stem.label || stem.category}</h3>
                <span>
                  {stem.category === "other"
                    ? "EXPERIMENTAL"
                    : !supported
                      ? "AUDITION ONLY"
                      : "PITCHED PART"}
                </span>
              </div>
              <p className="stem-category">
                Model category: <strong>{stem.category}</strong> ·{" "}
                {formatDuration(stem.duration_seconds)}
              </p>
              <p className="stem-description">{stemDescription(stem)}</p>
              <audio
                controls
                preload="metadata"
                src={audioURL}
                aria-label={`Audition ${stem.category} stem`}
                ref={(node) => {
                  if (node) players.current.set(stem.category, node);
                  else {
                    players.current.get(stem.category)?.pause();
                    players.current.delete(stem.category);
                  }
                }}
                onPlay={(event) =>
                  players.current.forEach((player) => {
                    if (player !== event.currentTarget) player.pause();
                  })
                }
                onError={() =>
                  setPlaybackErrors((current) =>
                    current.includes(stem.category)
                      ? current
                      : [...current, stem.category],
                  )
                }
              />
              {playbackErrors.includes(stem.category) && (
                <p className="field-error" role="status">
                  The browser could not play this stem. Check the server or
                  download its WAV to listen.
                </p>
              )}
              <div className="stem-actions">
                {child ? (
                  <a
                    className="button primary small"
                    href={`#/projects/${child.id}`}
                    aria-label={`Open ${stem.category} part project`}
                  >
                    Open part project <ArrowRight size={15} />
                  </a>
                ) : (
                  <button
                    className="button primary small"
                    disabled={!supported || pending !== null}
                    onClick={() => void transcribe(stem)}
                    aria-label={`Transcribe ${stem.category} stem`}
                  >
                    {pending === stem.category ? (
                      <>
                        <LoaderCircle size={15} className="spin" /> Creating
                        part…
                      </>
                    ) : supported ? (
                      <>
                        Transcribe this part <ArrowRight size={15} />
                      </>
                    ) : stem.category === "drums" ? (
                      "Drum notation unsupported"
                    ) : (
                      "Transcription unsupported"
                    )}
                  </button>
                )}
                <a
                  className="button secondary small"
                  href={audioURL}
                  download
                  aria-label={`Download ${stem.category} stem WAV`}
                >
                  <ArrowDownToLine size={14} /> WAV
                </a>
              </div>
              {child && (
                <p className="input-help">
                  Saved part project: {child.status}. Its edits and exports
                  remain separate from these preserved stems.
                </p>
              )}
            </article>
          );
        })}
      </div>
      <p className="input-help transcription-scope">
        Transcription estimates notes performed in the recording. Arrangement
        creates new accompaniment or simplified parts. This workflow performs
        transcription; it does not generate accompaniment.
      </p>
      <div className="stem-footer">
        <button
          className="button secondary small"
          disabled={loading || pending !== null}
          onClick={() => setReload((value) => value + 1)}
        >
          <RefreshCw size={14} /> Refresh parts
        </button>
        {result?.manifest && (
          <a
            className="button secondary small"
            href={`/api/projects/${project.id}/separation-manifest`}
            download
          >
            <ArrowDownToLine size={14} /> Separation manifest
          </a>
        )}
      </div>
      {result?.manifest && (
        <details className="separation-manifest">
          <summary>Engine, model and processing settings</summary>
          <p>
            Saved with this separation for reproducibility. Timing offsets are
            measured in seconds from the original recording.
          </p>
          <pre>{JSON.stringify(result.manifest, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}

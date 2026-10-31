import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  Eye,
  FileMusic,
  LoaderCircle,
  Maximize2,
  Save,
  X,
} from "lucide-react";
import { apiFetch } from "./api";
import type { CanonicalScore, Health, ScoreState } from "./api";
import ScoreRenderer from "./ScoreRenderer";

const formats = [
  {
    id: "pdf",
    label: "Download PDF",
    name: "PDF",
    description: "Printable sheet music",
    extension: "pdf",
  },
  {
    id: "midi",
    label: "Download MIDI",
    name: "MIDI",
    description: "Current edited score · quantized timing",
    extension: "mid",
  },
  {
    id: "musicxml",
    label: "Download MusicXML (editable sheet music)",
    name: "MusicXML",
    description: "Editable sheet music",
    extension: "musicxml",
  },
] as const;
type Format = (typeof formats)[number]["id"];
type Download = {
  status: "generating" | "ready" | "error";
  url?: string;
  filename?: string;
  error?: string;
};

function downloadName(header: string | null, fallback: string) {
  const encoded = header?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded).replace(/[\\/\r\n]/g, "-");
    } catch {
      /* Use the ASCII fallback. */
    }
  }
  return (
    header?.match(/filename="([^"]+)"/i)?.[1].replace(/[\\/\r\n]/g, "-") ||
    fallback
  );
}

function DownloadButtons({
  projectId,
  stored,
  blocked,
  health,
}: {
  projectId: string;
  stored: ScoreState | null;
  blocked: string;
  health?: Health | null;
}) {
  const [downloads, setDownloads] = useState<Partial<Record<Format, Download>>>(
    {},
  );
  const requests = useRef(new Map<Format, AbortController>());
  const urls = useRef(new Map<Format, string>());
  // The parent keys this component by revision and edit state, so a save, edit,
  // logout or navigation aborts old requests and releases all private blobs.
  useEffect(
    () => () => {
      requests.current.forEach((controller) => controller.abort());
      urls.current.forEach((url) => URL.revokeObjectURL(url));
    },
    [],
  );

  const start = async (format: (typeof formats)[number]) => {
    if (blocked || !stored?.score || requests.current.has(format.id)) return;
    const controller = new AbortController();
    requests.current.set(format.id, controller);
    setDownloads((current) => ({
      ...current,
      [format.id]: { status: "generating" },
    }));
    try {
      const response = await apiFetch(
        `/projects/${encodeURIComponent(projectId)}/score/exports/${format.id}?revision=${stored.revision}`,
        { signal: controller.signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(
          typeof body?.detail === "string"
            ? body.detail
            : `Download unavailable (${response.status}). Try again.`,
        );
      }
      if (response.headers.get("X-Score-Revision") !== String(stored.revision))
        throw new Error(
          "The saved score changed. Reload the saved score before downloading.",
        );
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const previous = urls.current.get(format.id);
      if (previous) URL.revokeObjectURL(previous);
      const url = URL.createObjectURL(blob);
      urls.current.set(format.id, url);
      const filename = downloadName(
        response.headers.get("Content-Disposition"),
        `${stored.score.title}-r${stored.revision}.${format.extension}`,
      );
      setDownloads((current) => ({
        ...current,
        [format.id]: { status: "ready", url, filename },
      }));
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
    } catch (error) {
      if (!controller.signal.aborted)
        setDownloads((current) => ({
          ...current,
          [format.id]: {
            status: "error",
            error:
              error instanceof TypeError
                ? "The connection was interrupted. Retry this download."
                : (error as Error).message,
          },
        }));
    } finally {
      requests.current.delete(format.id);
    }
  };

  return (
    <div className="sheet-downloads">
      {formats.map((format) => {
        const result = downloads[format.id];
        const unavailable =
          format.id === "pdf" && health?.capabilities.pdf === false
            ? health.pdf_export?.detail ||
              "PDF engraving is unavailable on this server. MusicXML can be printed from notation software."
            : blocked;
        const pending = result?.status === "generating";
        return (
          <div className="sheet-download" key={format.id}>
            <button
              className="button primary"
              disabled={!!unavailable || pending}
              aria-describedby={`export-${format.id}-help`}
              onClick={() => void start(format)}
            >
              {pending ? (
                <LoaderCircle size={18} className="spin" />
              ) : (
                <ArrowDownToLine size={18} />
              )}
              {format.label}
            </button>
            <p id={`export-${format.id}-help`}>
              {format.description}
              {unavailable ? ` · ${unavailable}` : ""}
            </p>
            <div
              aria-live="polite"
              aria-atomic="true"
              className="sheet-download-status"
            >
              {pending && (
                <p role="status">
                  Preparing {format.name}… Your download will start when ready.
                </p>
              )}
              {result?.status === "error" && (
                <>
                  <p className="export-error" role="alert">
                    {result.error}
                  </p>
                  <button
                    className="text-button"
                    onClick={() => void start(format)}
                    disabled={!!unavailable}
                  >
                    Retry {format.name} download
                  </button>
                </>
              )}
              {result?.status === "ready" && !unavailable && (
                <>
                  <p>
                    Ready · revision {stored?.revision}. If the download did not
                    start:
                  </p>
                  <a
                    href={`/api/projects/${encodeURIComponent(projectId)}/score/exports/${format.id}?revision=${stored?.revision}`}
                    download={result.filename}
                    onClick={(event) => {
                      event.preventDefault();
                      // Recheck the server revision: a different tab may have
                      // saved new settings since this download was prepared.
                      void start(format);
                    }}
                  >
                    Download {format.name} again
                  </a>
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function SheetMusic({
  projectId,
  stored,
  stale,
  unsaved,
  busy,
  invalid,
  health,
  musicxml,
  rendered,
  displayStatus,
  zoom,
  onZoom,
  onSave,
  canSave,
  error,
  onRetry,
  selectedIds,
  activeIds,
  onSelect,
}: {
  projectId: string;
  stored: ScoreState | null;
  stale: boolean;
  unsaved: boolean;
  busy: boolean;
  invalid: boolean;
  health?: Health | null;
  musicxml: string;
  rendered: CanonicalScore | null;
  displayStatus: string;
  zoom: number;
  onZoom: (zoom: number) => void;
  onSave: () => void;
  canSave: boolean;
  error: string;
  onRetry: () => void;
  selectedIds?: string[];
  activeIds?: string[];
  onSelect?: (ids: string[]) => void;
}) {
  const preview = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    if (!fullscreen) return;
    const node = dialog.current;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    node?.showModal();
    return () => {
      node?.close();
      document.body.style.overflow = overflow;
    };
  }, [fullscreen]);
  const blocked = busy
    ? "Loading or saving the score…"
    : !canSave
      ? "The sheet music could not be loaded. Retry below."
      : invalid
        ? "Correct the invalid notes, then save before downloading."
        : unsaved
          ? "Save your changes before downloading."
          : stale
            ? "Regenerate and save the score to include the latest notes."
            : !stored?.score
              ? "Create and save sheet music to enable this download."
              : "";
  const render = () => (
    <ScoreRenderer
      musicxml={musicxml}
      zoom={zoom}
      score={rendered}
      selectedIds={selectedIds}
      activeIds={activeIds}
      onSelect={onSelect}
    />
  );

  return (
    <section className="card sheet-music" aria-label="Your sheet music">
      <div className="sheet-music-heading">
        <div>
          <span className="eyebrow">READY TO PLAY & SHARE</span>
          <h2>
            <FileMusic size={25} /> Your sheet music
          </h2>
        </div>
        <button
          className="button secondary"
          disabled={!musicxml}
          onClick={() => {
            preview.current?.scrollIntoView({
              behavior: window.matchMedia("(prefers-reduced-motion: reduce)")
                .matches
                ? "instant"
                : "smooth",
              block: "start",
            });
            preview.current?.focus({ preventScroll: true });
          }}
        >
          <Eye size={18} /> View sheet music
        </button>
      </div>
      <p className="sheet-revision">
        {stored?.score && !stale
          ? `Saved score · revision ${stored.revision}. Downloads include saved corrections and transposition.`
          : "Your detected performance, prepared as readable sheet music."}
      </p>
      <DownloadButtons
        key={`${projectId}:${stored?.revision ?? 0}:${stale}:${unsaved}:${busy}:${invalid}`}
        projectId={projectId}
        stored={stored}
        blocked={blocked}
        health={health}
      />
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {!canSave && !busy && (
        <button className="button secondary" onClick={onRetry}>
          Retry loading sheet music
        </button>
      )}
      {canSave && (unsaved || stale || (!stored?.score && !busy)) && (
        <div className="sheet-save-notice" role="status">
          <p>
            {unsaved
              ? "You have unsaved changes. Save them to update your sheet music and all three downloads."
              : "Sheet music needs to be generated from your saved notes. Musical settings below remain adjustable."}
          </p>
          <button
            className="button secondary"
            disabled={busy || invalid}
            onClick={onSave}
          >
            <Save size={16} />
            {unsaved
              ? "Save changes"
              : stale
                ? "Regenerate sheet music"
                : "Create sheet music"}
          </button>
        </div>
      )}
      <div
        ref={preview}
        className="sheet-inline-preview"
        tabIndex={-1}
        aria-label="Sheet music preview"
      >
        <div className="score-toolbar">
          <p>{displayStatus}</p>
          <div className="sheet-view-controls">
            <label>
              Zoom{" "}
              <input
                aria-label="Score zoom"
                type="range"
                min={0.5}
                max={1.5}
                step={0.1}
                value={zoom}
                onChange={(event) => onZoom(Number(event.target.value))}
              />
              <output>{Math.round(zoom * 100)}%</output>
            </label>
            <button
              ref={trigger}
              className="button secondary"
              disabled={!musicxml}
              onClick={() => setFullscreen(true)}
            >
              <Maximize2 size={16} /> Full-screen score
            </button>
          </div>
        </div>
        {musicxml ? (
          !fullscreen ? (
            render()
          ) : (
            <p className="score-render-status">
              The score is open in full-screen view.
            </p>
          )
        ) : (
          <p className="score-first-preview">
            {busy
              ? "Loading your sheet music…"
              : "Your score preview will appear here once sheet music is available."}
          </p>
        )}
      </div>
      <dialog
        ref={dialog}
        className="sheet-fullscreen"
        aria-label="Full-screen sheet music"
        onCancel={() => setFullscreen(false)}
        onClose={() => {
          setFullscreen(false);
          trigger.current?.focus();
        }}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const controls = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              'button:not([disabled]), input:not([disabled]), [tabindex="0"]',
            ),
          ).filter((node) => node.getClientRects().length > 0);
          const first = controls[0],
            last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        {fullscreen && (
          <>
            <div className="sheet-fullscreen-heading">
              <div>
                <h2>Your sheet music</h2>
                <p>{displayStatus}</p>
              </div>
              <button
                autoFocus
                className="button secondary"
                onClick={() => setFullscreen(false)}
              >
                <X size={18} /> Close full-screen
              </button>
            </div>
            <label className="sheet-fullscreen-zoom">
              Zoom{" "}
              <input
                aria-label="Full-screen score zoom"
                type="range"
                min={0.5}
                max={1.5}
                step={0.1}
                value={zoom}
                onChange={(event) => onZoom(Number(event.target.value))}
              />
              <output>{Math.round(zoom * 100)}%</output>
            </label>
            {render()}
          </>
        )}
      </dialog>
    </section>
  );
}

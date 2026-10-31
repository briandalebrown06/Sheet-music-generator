import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Trash2 } from "lucide-react";
import { request } from "./api";
import type { Project } from "./api";

export default function DeleteProject({
  project,
  onDeleted,
}: {
  project: Project;
  onDeleted: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => () => dialog.current?.close(), []);
  const remove = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await request(`/projects/${project.id}`, { method: "DELETE" });
      dialog.current?.close();
      location.hash = "/library";
      await onDeleted();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        ref={trigger}
        className="button secondary danger-text"
        onClick={() => {
          setError("");
          dialog.current?.showModal();
        }}
      >
        <Trash2 size={15} /> Delete project
      </button>
      <dialog
        ref={dialog}
        className="delete-dialog"
        aria-labelledby="delete-title"
        aria-describedby="delete-description"
        onCancel={(event) => {
          if (busy) event.preventDefault();
        }}
        onClose={() => trigger.current?.focus()}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const controls = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>(
              "button:not([disabled])",
            ),
          );
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
        <form onSubmit={remove}>
          <span className="eyebrow">PERMANENT DELETION</span>
          <h2 id="delete-title">Delete this project?</h2>
          <p className="delete-project-name">{project.title}</p>
          <p id="delete-description">
            This cancels processing and removes the recording, analysis files,
            edits, scores and exports.
            {project.input_mode === "mixed"
              ? " All selected-part projects and separated stems will also be deleted."
              : project.parent_project_id
                ? " The parent mixed recording and other parts remain."
                : ""}{" "}
            Any piano editions derived from this project, including editions
            using its vocal melody, are also deleted.{" "}
            {project.arrangement_source_id && "Its source version remains. "}
            Access stops immediately; file cleanup finishes in the background.
            This cannot be undone.
          </p>
          <p className="input-help">
            Copies you downloaded and administrator backups have separate
            retention.
          </p>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <button
              type="button"
              className="button secondary"
              autoFocus
              disabled={busy}
              onClick={() => dialog.current?.close()}
            >
              Keep project
            </button>
            <button type="submit" className="button danger" disabled={busy}>
              {busy ? (
                <LoaderCircle className="spin" size={15} />
              ) : (
                <Trash2 size={15} />
              )}{" "}
              {busy ? "Deleting…" : "Delete permanently"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}

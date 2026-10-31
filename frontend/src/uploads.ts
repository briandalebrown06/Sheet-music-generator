import { ApiError, apiAccountId, apiFetch, apiRequestHeaders } from "./api";
import type { InputMode, ProjectDetail, Source } from "./api";

export const RESUMABLE_THRESHOLD = 8 * 1024 * 1024;
export interface UploadMetadata {
  filename: string;
  size_bytes: number;
  title: string;
  source: Source;
  input_mode: InputMode;
  clip_start_seconds: number;
  clip_end_seconds: number | null;
  instrument_hint: string | null;
  tempo_hint: number | null;
  time_signature_hint: string | null;
  minimum_note_seconds: number;
  merge_gap_seconds: number;
}
export interface UploadSession {
  id: string;
  status: "uploading" | "completed";
  offset: number;
  size_bytes: number;
  chunk_size: number;
  expires_at: number;
  project_id?: string;
  preview_url?: string;
  chunks: { offset: number; size_bytes: number; sha256: string }[];
}
export interface UploadProgress {
  stage: "checking" | "uploading" | "retrying" | "completing" | "paused";
  sentBytes: number;
  confirmedBytes: number;
  totalBytes: number;
  message: string;
}
type StoredUpload = {
  id?: string;
  key: string;
  metadata: UploadMetadata;
  updated: number;
  expiresAt?: number;
};
const PREFIX = "scorecraft-upload-v1:";
const memory = new Map<string, StoredUpload>();
const abortError = () => new DOMException("Upload paused.", "AbortError");
const checkAbort = (signal: AbortSignal) => {
  if (signal.aborted) throw abortError();
};
const hex = (bytes: ArrayBuffer) =>
  [...new Uint8Array(bytes)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
async function hash(blob: Blob, signal: AbortSignal) {
  checkAbort(signal);
  const buffer = await blob.arrayBuffer();
  checkAbort(signal);
  return hex(await crypto.subtle.digest("SHA-256", buffer));
}
async function identity(file: File, signal: AbortSignal) {
  // Small identity samples only locate a resumable session. Every already
  // accepted chunk is rehashed below before any resume is trusted.
  const edge = 65536;
  const [first, last] = await Promise.all([
    hash(file.slice(0, edge), signal),
    hash(file.slice(Math.max(0, file.size - edge)), signal),
  ]);
  return hex(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(
        JSON.stringify([
          location.origin,
          apiAccountId(),
          file.name,
          file.size,
          file.lastModified,
          first,
          last,
        ]),
      ),
    ),
  );
}
function readStored(key: string): StoredUpload | null {
  let parsed: StoredUpload | undefined = memory.get(key);
  try {
    const value = sessionStorage.getItem(PREFIX + key);
    if (value) parsed = JSON.parse(value) as StoredUpload;
  } catch {
    /* Private browsing/storage denial falls back to this open page. */
  }
  if (
    parsed &&
    typeof parsed.key === "string" &&
    parsed.metadata &&
    Number.isFinite(parsed.updated) &&
    Date.now() - parsed.updated < 24 * 60 * 60 * 1000 &&
    (!parsed.expiresAt || parsed.expiresAt * 1000 > Date.now())
  )
    return parsed;
  forget(key);
  return null;
}
function writeStored(key: string, value: StoredUpload) {
  memory.set(key, value);
  while (memory.size > 10) memory.delete(memory.keys().next().value!);
  try {
    const entries = Object.keys(sessionStorage).filter((item) =>
      item.startsWith(PREFIX),
    );
    for (const item of entries) {
      try {
        const saved = JSON.parse(
          sessionStorage.getItem(item) || "null",
        ) as StoredUpload | null;
        if (
          !saved ||
          Date.now() - saved.updated > 24 * 60 * 60 * 1000 ||
          (saved.expiresAt && saved.expiresAt * 1000 <= Date.now())
        )
          sessionStorage.removeItem(item);
      } catch {
        sessionStorage.removeItem(item);
      }
    }
    while (entries.length >= 10) sessionStorage.removeItem(entries.shift()!);
    sessionStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* The server session still supports same-page retry. */
  }
}
function forget(key: string) {
  memory.delete(key);
  try {
    sessionStorage.removeItem(PREFIX + key);
  } catch {
    /* Optional storage. */
  }
}
export function forgetUpload(id: string) {
  for (const [key, value] of memory) if (value.id === id) forget(key);
  try {
    for (const key of Object.keys(sessionStorage).filter((item) =>
      item.startsWith(PREFIX),
    )) {
      try {
        if (JSON.parse(sessionStorage.getItem(key) || "null")?.id === id)
          sessionStorage.removeItem(key);
      } catch {
        sessionStorage.removeItem(key);
      }
    }
  } catch {
    /* Optional storage. */
  }
}
async function json<T>(
  path: string,
  signal: AbortSignal,
  init: RequestInit = {},
): Promise<T> {
  checkAbort(signal);
  const response = await apiFetch(path, { ...init, signal });
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new ApiError(
      typeof body?.detail === "string"
        ? body.detail
        : `Upload request failed (${response.status}).`,
      response.status,
    );
  return body as T;
}
export function transfer<T>(
  path: string,
  method: string,
  body: Blob | FormData,
  signal: AbortSignal,
  onProgress: (sent: number, total: number) => void,
  headers: Record<string, string> = {},
): Promise<T> {
  return new Promise((resolve, reject) => {
    checkAbort(signal);
    const xhr = new XMLHttpRequest();
    xhr.open(method, `/api${path}`);
    xhr.withCredentials = true;
    xhr.timeout = 180000;
    apiRequestHeaders(method, headers).forEach((value, name) =>
      xhr.setRequestHeader(name, value),
    );
    const abort = () => xhr.abort();
    signal.addEventListener("abort", abort, { once: true });
    const cleanup = () => signal.removeEventListener("abort", abort);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    };
    xhr.onload = () => {
      cleanup();
      let result: unknown;
      try {
        result = JSON.parse(xhr.responseText);
      } catch {
        result = null;
      }
      if (xhr.status === 401)
        window.dispatchEvent(new Event("scorecraft-session-expired"));
      if (xhr.status < 200 || xhr.status >= 300) {
        const detail = (result as { detail?: unknown } | null)?.detail;
        reject(
          new ApiError(
            typeof detail === "string"
              ? detail
              : `Upload request failed (${xhr.status}).`,
            xhr.status,
          ),
        );
      } else resolve(result as T);
    };
    xhr.onerror = () => {
      cleanup();
      reject(new TypeError("The upload connection was interrupted."));
    };
    xhr.ontimeout = () => {
      cleanup();
      reject(new TypeError("The upload connection timed out."));
    };
    xhr.onabort = () => {
      cleanup();
      reject(abortError());
    };
    xhr.send(body);
  });
}
async function delay(signal: AbortSignal, milliseconds: number) {
  await new Promise<void>((resolve, reject) => {
    checkAbort(signal);
    const stop = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stop);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", stop, { once: true });
  });
}
export async function uploadFile(
  file: File,
  metadata: UploadMetadata,
  signal: AbortSignal,
  onProgress: (progress: UploadProgress) => void,
  onSession: (session: UploadSession) => void,
): Promise<UploadSession> {
  const report = (
    stage: UploadProgress["stage"],
    confirmed: number,
    sent: number,
    message: string,
  ) =>
    onProgress({
      stage,
      confirmedBytes: confirmed,
      sentBytes: sent,
      totalBytes: file.size,
      message,
    });
  report("checking", 0, 0, "Checking the file for a resumable upload…");
  const key = await identity(file, signal);
  let saved = readStored(key) ?? {
    key: crypto.randomUUID(),
    metadata,
    updated: Date.now(),
  };
  writeStored(key, saved);
  let session: UploadSession | null = null;
  if (saved.id) {
    try {
      session = await json<UploadSession>(`/uploads/${saved.id}`, signal);
    } catch (reason) {
      if (!(reason instanceof ApiError) || ![404, 410].includes(reason.status))
        throw reason;
      saved = { key: crypto.randomUUID(), metadata, updated: Date.now() };
      writeStored(key, saved);
    }
  }
  if (!session)
    session = await json<UploadSession>("/uploads", signal, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": saved.key,
      },
      body: JSON.stringify(saved.metadata),
    });
  saved.id = session.id;
  saved.updated = Date.now();
  saved.expiresAt = session.expires_at;
  writeStored(key, saved);
  onSession(session);
  if (
    session.size_bytes !== file.size ||
    !Number.isSafeInteger(session.offset) ||
    session.offset < 0 ||
    session.offset > file.size ||
    !Number.isSafeInteger(session.chunk_size) ||
    session.chunk_size < 1 ||
    session.chunk_size > RESUMABLE_THRESHOLD
  )
    throw new Error(
      "The saved upload has inconsistent size information. Discard it and select the file again.",
    );
  let verified = 0;
  for (const chunk of session.chunks) {
    if (
      chunk.offset !== verified ||
      chunk.size_bytes < 1 ||
      chunk.size_bytes > session.chunk_size ||
      chunk.offset + chunk.size_bytes > session.offset
    )
      throw new Error(
        "The saved upload has inconsistent chunk information. Discard it and select the file again.",
      );
    report(
      "checking",
      session.offset,
      session.offset,
      `Verifying previously uploaded bytes (${verified} of ${session.offset})…`,
    );
    if (
      (await hash(
        file.slice(chunk.offset, chunk.offset + chunk.size_bytes),
        signal,
      )) !== chunk.sha256
    )
      throw new Error(
        "This file differs from the earlier upload. Discard the upload before submitting the changed file.",
      );
    verified += chunk.size_bytes;
  }
  if (verified !== session.offset)
    throw new Error(
      "The server could not verify all uploaded chunks. Discard this upload and retry.",
    );
  while (session.offset < file.size) {
    checkAbort(signal);
    const offset: number = session.offset;
    const chunk = file.slice(
      offset,
      Math.min(file.size, offset + session.chunk_size),
    );
    const digest = await hash(chunk, signal);
    let next: UploadSession | null = null;
    for (let attempt = 0; attempt < 3 && !next; attempt++) {
      try {
        report(
          "uploading",
          offset,
          offset,
          "Uploading audio. Analysis has not started.",
        );
        next = await transfer<UploadSession>(
          `/uploads/${session.id}/chunks/${offset}`,
          "PUT",
          chunk,
          signal,
          (sent) =>
            report(
              "uploading",
              offset,
              offset + Math.min(sent, chunk.size),
              "Uploading audio. Analysis has not started.",
            ),
          {
            "Content-Type": "application/octet-stream",
            "X-Chunk-SHA256": digest,
          },
        );
      } catch (reason) {
        checkAbort(signal);
        if (
          reason instanceof ApiError &&
          ![408, 409, 429, 500, 502, 503, 504].includes(reason.status)
        )
          throw reason;
        report(
          "retrying",
          offset,
          offset,
          `Connection interrupted. Checking saved progress before retry ${attempt + 1} of 3…`,
        );
        try {
          const current: UploadSession = await json<UploadSession>(
            `/uploads/${session.id}`,
            signal,
          );
          const accepted = current.chunks.find(
            (value) => value.offset === offset,
          );
          if (
            current.offset === offset + chunk.size &&
            accepted?.sha256 === digest &&
            accepted.size_bytes === chunk.size
          )
            next = current;
          else if (current.offset !== offset)
            throw new Error(
              "This upload changed in another tab. Pause and reselect the same file to verify it.",
            );
        } catch (check) {
          if (!(check instanceof TypeError)) throw check;
        }
        if (!next && attempt === 2)
          throw new Error(
            "Upload paused after three connection attempts. Retry to continue from the server’s saved bytes.",
          );
        if (!next) await delay(signal, 500 * (attempt + 1));
      }
    }
    if (!next || next.offset !== offset + chunk.size)
      throw new Error(
        "The server did not confirm this audio chunk. Retry to check saved progress.",
      );
    session = next;
    saved.updated = Date.now();
    saved.expiresAt = session.expires_at;
    writeStored(key, saved);
    onSession(session);
    report(
      "uploading",
      session.offset,
      session.offset,
      "Audio bytes saved. Continuing upload…",
    );
  }
  report(
    "completing",
    file.size,
    file.size,
    "Audio transfer complete. Ready to validate and queue analysis.",
  );
  return session;
}
export async function completeUpload(
  session: UploadSession,
  metadata: UploadMetadata,
  signal: AbortSignal,
) {
  const { filename: _filename, size_bytes: _size, ...options } = metadata;
  const project = await json<ProjectDetail>(
    `/uploads/${session.id}/complete`,
    signal,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    },
  );
  forgetUpload(session.id);
  return project;
}
export async function discardUpload(id: string) {
  const response = await apiFetch(`/uploads/${id}`, { method: "DELETE" });
  if (!response.ok && response.status !== 404 && response.status !== 410)
    throw new Error(
      "The partial upload could not be discarded. Retry; abandoned uploads also expire automatically.",
    );
  forgetUpload(id);
}

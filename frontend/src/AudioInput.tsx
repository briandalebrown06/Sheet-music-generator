import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  AudioLines,
  Check,
  FileAudio,
  LoaderCircle,
  Mic,
  Music2,
  Pause,
  Play,
  Plus,
  ShieldCheck,
  Square,
  Upload,
  X,
} from "lucide-react";
import { apiFetch, formatDuration } from "./api";
import {
  completeUpload,
  discardUpload,
  RESUMABLE_THRESHOLD,
  transfer,
  uploadFile,
} from "./uploads";
import type { UploadMetadata, UploadProgress, UploadSession } from "./uploads";
import type { Health, InputMode, ProjectDetail, Source } from "./api";

type Phase = "idle" | "requesting" | "recording" | "stopping";
type Candidate = { file: File; measuredDuration?: number };
const recordingTypes = [
  "audio/webm;codecs=opus",
  "audio/mp4;codecs=mp4a.40.2",
  "audio/ogg;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg",
  "audio/wav",
];
const extensions: Record<string, string> = {
  "audio/webm": "webm",
  "audio/mp4": "m4a",
  "audio/ogg": "ogg",
  "audio/wav": "wav",
  "audio/x-wav": "wav",
};
const preciseTime = (value: number) => String(Math.round(value * 1000) / 1000);

function microphoneError(error: unknown): string {
  switch ((error as DOMException).name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
      return "Microphone access was denied. Allow microphone access in your browser settings, then try again, or upload a file. Recording requires HTTPS or localhost.";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "No microphone was found. Connect a microphone, check your system input settings, and try again, or upload a file.";
    case "NotReadableError":
    case "TrackStartError":
      return "The microphone could not be opened. Another application or your operating system may be blocking it. Check your input settings and try again.";
    case "NotSupportedError":
      return "This browser could not record a supported audio format. Try a current browser or upload an audio file.";
    default:
      return "Recording could not start. Check your microphone and browser permissions, then try again or upload an audio file.";
  }
}

export default function AudioInput({
  health,
  onCreated,
}: {
  health: Health | null;
  onCreated: () => Promise<void>;
}) {
  const [mode, setMode] = useState<Source>("upload");
  const [inputMode, setInputMode] = useState<InputMode>("solo");
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [title, setTitle] = useState("");
  const [preview, setPreview] = useState("");
  const [preparedPreview, setPreparedPreview] = useState("");
  const [duration, setDuration] = useState<number | null>(null);
  const [durationEstimated, setDurationEstimated] = useState(false);
  const [playable, setPlayable] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(
    null,
  );
  const [pausedUpload, setPausedUpload] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [recordedType, setRecordedType] = useState("");
  const [clipStart, setClipStart] = useState("0");
  const [clipEnd, setClipEnd] = useState("");
  const [endEdited, setEndEdited] = useState(false);
  const [clipPlaying, setClipPlaying] = useState(false);
  const [instrument, setInstrument] = useState("");
  const [tempo, setTempo] = useState("");
  const [meter, setMeter] = useState("");
  const [minimumNote, setMinimumNote] = useState("0.08");
  const [mergeGap, setMergeGap] = useState("0.03");
  const submission = useRef<{
    file: File;
    options: string;
    key: string;
  } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const mounted = useRef(false);
  const recordingGeneration = useRef(0);
  const sourceGeneration = useRef(0);
  const phaseRef = useRef<Phase>("idle");
  const startedAt = useRef(0);
  const stoppedAt = useRef<number | null>(null);
  const prepareController = useRef<AbortController | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const uploadSession = useRef<UploadSession | null>(null);
  const maxBytes = health?.limits.max_upload_bytes ?? 500_000_000;
  const maxSeconds = health?.limits.max_duration_seconds ?? 3600;
  const file = candidate?.file;
  const resumable =
    !!file &&
    file.size > RESUMABLE_THRESHOLD &&
    health?.capabilities.resumable_upload === true;
  const locked = phase !== "idle" || busy || preparing || discarding;

  const updatePhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    if (mounted.current) setPhase(next);
  }, []);
  const releaseTracks = useCallback(() => {
    stream.current?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    stream.current = null;
  }, []);
  const stop = useCallback(() => {
    if (phaseRef.current !== "recording") return;
    stoppedAt.current = performance.now();
    setElapsed((stoppedAt.current - startedAt.current) / 1000);
    updatePhase("stopping");
    try {
      if (recorder.current && recorder.current.state !== "inactive")
        recorder.current.stop();
      else updatePhase("idle");
    } catch {
      updatePhase("idle");
      setError(
        "The browser could not finish this recording. Please record another take.",
      );
    } finally {
      releaseTracks();
    }
  }, [releaseTracks, updatePhase]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      recordingGeneration.current += 1;
      sourceGeneration.current += 1;
      prepareController.current?.abort();
      uploadController.current?.abort();
      const current = recorder.current;
      if (current) {
        current.ondataavailable = null;
        current.onerror = null;
        current.onstop = null;
        if (current.state !== "inactive") {
          try {
            current.stop();
          } catch {
            /* Tracks are still released below. */
          }
        }
      }
      releaseTracks();
    };
  }, [releaseTracks]);

  useEffect(() => {
    if (!file) {
      setPreview("");
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  useEffect(() => {
    return () => {
      if (preparedPreview.startsWith("blob:"))
        URL.revokeObjectURL(preparedPreview);
    };
  }, [preparedPreview]);
  useEffect(() => {
    if (phase !== "recording") return;
    const update = () =>
      setElapsed((performance.now() - startedAt.current) / 1000);
    const timer = setInterval(update, 100);
    // A short margin accommodates the encoder's final packet at the source limit.
    const deadline = setTimeout(stop, Math.max(0, maxSeconds * 1000 - 200));
    return () => {
      clearInterval(timer);
      clearTimeout(deadline);
    };
  }, [phase, maxSeconds, stop]);

  const resetSource = () => {
    sourceGeneration.current += 1;
    prepareController.current?.abort();
    prepareController.current = null;
    audio.current?.pause();
    setCandidate(null);
    setPreparedPreview("");
    setDuration(null);
    setDurationEstimated(false);
    setPlayable(false);
    setPreviewError("");
    setError("");
    setClipStart("0");
    setClipEnd("");
    setEndEdited(false);
    setClipPlaying(false);
    setPreparing(false);
    setRecordedType("");
    setUploadProgress(null);
    setPausedUpload(false);
    uploadSession.current = null;
  };
  const discardSource = async () => {
    uploadController.current?.abort();
    prepareController.current?.abort();
    const session = uploadSession.current;
    setDiscarding(true);
    try {
      if (session) await discardUpload(session.id);
      resetSource();
      return true;
    } catch (reason) {
      if (mounted.current) setError((reason as Error).message);
      return false;
    } finally {
      if (mounted.current) setDiscarding(false);
    }
  };
  const reportUpload = (progress: UploadProgress) => {
    if (mounted.current) setUploadProgress(progress);
  };
  const rememberUpload = (session: UploadSession) => {
    uploadSession.current = session;
  };
  const pauseUpload = () => {
    uploadController.current?.abort();
    prepareController.current?.abort();
    setPausedUpload(true);
    setUploadProgress((value) =>
      value
        ? {
            ...value,
            stage: "paused",
            sentBytes: value.confirmedBytes,
            message: resumable
              ? "Upload paused. Retry to continue from the server’s saved bytes. You can also reload and reselect this same file in this browser tab."
              : "Upload paused. Retry safely with the same submission key; this short request will be resent.",
          }
        : value,
    );
  };
  const changeMode = (next: Source) => {
    if (locked || next === mode) return;
    if (uploadSession.current)
      void discardSource().then((discarded) => {
        if (discarded) setMode(next);
      });
    else {
      resetSource();
      setMode(next);
    }
  };
  const selectFile = (selected: File) => {
    setError("");
    if (!/\.(wav|mp3|m4a|flac|ogg|webm|mp4)$/i.test(selected.name)) {
      setError("Choose a WAV, MP3, M4A, FLAC, OGG, WebM or MP4 audio file.");
      return;
    }
    if (!selected.size) {
      setError("This file is empty. Choose a recording with audio.");
      return;
    }
    if (selected.size > maxBytes) {
      setError(
        `This file is too large. The limit is ${Number((maxBytes / 1_000_000).toFixed(2))} MB.`,
      );
      return;
    }
    resetSource();
    setCandidate({ file: selected });
    setTitle(selected.name.replace(/\.[^.]+$/, ""));
  };

  const record = async () => {
    if (phaseRef.current !== "idle") return;
    setError("");
    if (!window.isSecureContext) {
      setError(
        "Microphone recording requires HTTPS or localhost. Open this local studio at http://127.0.0.1:5173, or use HTTPS for a hosted setup. You can also upload an audio file.",
      );
      return;
    }
    if (
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === "undefined" ||
      typeof MediaRecorder.isTypeSupported !== "function"
    ) {
      setError(
        "This browser does not support microphone recording. Use a current browser with MediaRecorder on HTTPS or localhost, or upload an audio file.",
      );
      return;
    }
    const requestedType = recordingTypes.find((type) =>
      MediaRecorder.isTypeSupported(type),
    );
    if (!requestedType) {
      setError(
        "This browser cannot record any audio format supported by this studio. Try a current browser or upload WAV, MP3, M4A, or FLAC audio.",
      );
      return;
    }
    const generation = ++recordingGeneration.current;
    updatePhase("requesting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      if (!mounted.current || generation !== recordingGeneration.current) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;
      const rec = new MediaRecorder(media, { mimeType: requestedType });
      const actualType = rec.mimeType;
      if (!actualType || !extensions[actualType.split(";")[0].toLowerCase()]) {
        throw new DOMException(
          "Unsupported recorder output",
          "NotSupportedError",
        );
      }
      recorder.current = rec;
      const releaseSessionTracks = () => {
        media.getTracks().forEach((track) => {
          track.onended = null;
          track.stop();
        });
        if (stream.current === media) stream.current = null;
      };
      const isCurrentSession = () =>
        mounted.current && generation === recordingGeneration.current;
      const chunks: Blob[] = [];
      let bytes = 0;
      let failure = "";
      rec.ondataavailable = (event) => {
        if (!isCurrentSession() || !event.data.size || failure) return;
        bytes += event.data.size;
        if (bytes > maxBytes) {
          failure =
            "The recording exceeded the upload limit. Please make a shorter recording.";
          chunks.length = 0;
          stop();
          return;
        }
        chunks.push(event.data);
      };
      rec.onerror = () => {
        releaseSessionTracks();
        if (!isCurrentSession()) return;
        failure =
          "Recording failed before the browser could finish the audio file. Check your microphone and try another take.";
        if (phaseRef.current === "recording") stop();
        if (mounted.current && generation === recordingGeneration.current)
          setError(failure);
      };
      rec.onstop = () => {
        releaseSessionTracks();
        if (!mounted.current || generation !== recordingGeneration.current)
          return;
        recorder.current = null;
        updatePhase("idle");
        if (failure) {
          setError(failure);
          return;
        }
        if (!bytes) {
          setError(
            "No audio was captured. Check your microphone and record another take.",
          );
          return;
        }
        const length = Math.max(
          0,
          ((stoppedAt.current ?? performance.now()) - startedAt.current) / 1000,
        );
        if (length < 0.1) {
          setError(
            "The recording was too short. Record at least 0.1 seconds of audio.",
          );
          return;
        }
        const type = rec.mimeType;
        const extension = extensions[type.split(";")[0].toLowerCase()];
        if (!extension) {
          setError(
            "The browser produced an unsupported recording format. Try another browser or upload a file.",
          );
          return;
        }
        const selected = new File(
          chunks,
          `Recording ${new Date().toISOString().replace(/[:.]/g, "-")}.${extension}`,
          { type },
        );
        setCandidate({ file: selected, measuredDuration: length });
        setDuration(length);
        setDurationEstimated(true);
        setClipEnd(preciseTime(length));
        setTitle(`Recording ${new Date().toLocaleDateString()}`);
        setRecordedType(type);
        setElapsed(length);
      };
      media.getAudioTracks().forEach((track) => {
        track.onended = () => {
          if (!isCurrentSession()) return;
          failure =
            "The microphone disconnected during recording. Reconnect it and record another take.";
          stop();
        };
      });
      resetSource();
      startedAt.current = performance.now();
      stoppedAt.current = null;
      setElapsed(0);
      rec.start(250);
      updatePhase("recording");
    } catch (reason) {
      if (generation === recordingGeneration.current) releaseTracks();
      if (mounted.current && generation === recordingGeneration.current) {
        updatePhase("idle");
        setError(microphoneError(reason));
      }
    }
  };
  const cancelMicrophone = () => {
    recordingGeneration.current += 1;
    releaseTracks();
    updatePhase("idle");
    setError(
      "Microphone request cancelled. If the browser permission prompt is still open, you can dismiss it.",
    );
  };

  const metadataLoaded = (element: HTMLAudioElement) => {
    const value =
      Number.isFinite(element.duration) && element.duration > 0
        ? element.duration
        : candidate?.measuredDuration;
    if (value && Number.isFinite(value)) {
      setDuration(value);
      setDurationEstimated(!Number.isFinite(element.duration));
      // Metadata-only preload avoids fetching long audio eagerly. Playback
      // itself will request the needed media bytes after the user's action.
      if (!element.error) setPlayable(true);
      if (!endEdited) setClipEnd(preciseTime(value));
    }
  };
  const preparePreview = async () => {
    if (!file || preparing) return;
    const generation = sourceGeneration.current;
    const controller = new AbortController();
    prepareController.current = controller;
    setPreparing(true);
    setError("");
    try {
      if (resumable) {
        uploadController.current = controller;
        setPausedUpload(false);
        const session = await uploadFile(
          file,
          metadata(),
          controller.signal,
          reportUpload,
          rememberUpload,
        );
        const path =
          session.preview_url || `/api/uploads/${session.id}/preview`;
        setUploadProgress((value) =>
          value
            ? {
                ...value,
                stage: "completing",
                message:
                  "Preparing a full-length playback copy. No analysis has started.",
              }
            : value,
        );
        const response = await apiFetch(path.replace(/^\/api/, ""), {
          signal: controller.signal,
        });
        if (!response.ok) {
          const failure = await response.json().catch(() => null);
          throw new Error(
            typeof failure?.detail === "string"
              ? failure.detail
              : "The studio decoder could not prepare this recording.",
          );
        }
        // Consume only response headers. Native audio streams the private copy
        // on demand, rather than retaining a decoded hour-long Blob in JS.
        await response.body?.cancel();
        if (!mounted.current || generation !== sourceGeneration.current) return;
        audio.current?.pause();
        setClipPlaying(false);
        setPlayable(false);
        setPreviewError("");
        setPreparedPreview(path);
        setUploadProgress(null);
        return;
      }
      const body = new FormData();
      body.append("file", file);
      const response = await apiFetch("/audio-preview", {
        method: "POST",
        body,
        signal: controller.signal,
      });
      if (!response.ok) {
        const failure = await response.json().catch(() => null);
        throw new Error(
          typeof failure?.detail === "string"
            ? failure.detail
            : "The studio decoder could not prepare this audio. Check the server and try another file.",
        );
      }
      const blob = await response.blob();
      if (!mounted.current || generation !== sourceGeneration.current) return;
      audio.current?.pause();
      setClipPlaying(false);
      setPlayable(false);
      setPreviewError("");
      setPreparedPreview(URL.createObjectURL(blob));
    } catch (reason) {
      if (
        !controller.signal.aborted &&
        mounted.current &&
        generation === sourceGeneration.current
      )
        setError(
          (reason as Error).message ||
            "The server is unavailable. Check your connection and try again.",
        );
    } finally {
      if (mounted.current && generation === sourceGeneration.current)
        setPreparing(false);
    }
  };

  const startValue = Number(clipStart);
  const endValue = clipEnd === "" ? duration : Number(clipEnd);
  const rangeError = !file
    ? ""
    : clipStart === "" || !Number.isFinite(startValue) || startValue < 0
      ? "Enter a start time of zero or more seconds."
      : endValue === null || !Number.isFinite(endValue)
        ? "A readable audio duration is needed before choosing your clip."
        : endValue - startValue < 0.1 - 0.000001
          ? "Choose a clip at least 0.1 seconds long, with the end after the start."
          : duration !== null &&
              (startValue >= duration || endValue > duration + 0.05)
            ? "The clip must fit within the original recording."
            : duration !== null && duration > maxSeconds + 0.05
              ? `The original recording exceeds the ${maxSeconds}-second limit. Trim the file before uploading it.`
              : "";
  const tempoError =
    tempo !== "" &&
    (!Number.isInteger(Number(tempo)) ||
      Number(tempo) < 30 ||
      Number(tempo) > 300)
      ? "Tempo must be a whole number from 30 to 300 BPM."
      : "";
  const cleanupError =
    minimumNote === "" ||
    !Number.isFinite(Number(minimumNote)) ||
    Number(minimumNote) < 0 ||
    Number(minimumNote) > 1 ||
    mergeGap === "" ||
    !Number.isFinite(Number(mergeGap)) ||
    Number(mergeGap) < 0 ||
    Number(mergeGap) > 0.2
      ? "Minimum note duration must be 0–1 seconds and merge gap must be 0–0.2 seconds."
      : "";
  const canSubmit =
    !!file &&
    !!health &&
    playable &&
    duration !== null &&
    !previewError &&
    !rangeError &&
    !tempoError &&
    !cleanupError &&
    !locked;

  const metadata = (): UploadMetadata => ({
    filename: file?.name ?? "recording.webm",
    size_bytes: file?.size ?? 0,
    title: title.trim() || file?.name || "Untitled recording",
    source: mode,
    input_mode: inputMode,
    clip_start_seconds: Number.isFinite(startValue) ? startValue : 0,
    clip_end_seconds:
      endEdited && endValue !== null && Number.isFinite(endValue)
        ? endValue
        : null,
    instrument_hint: instrument || null,
    tempo_hint: tempo === "" ? null : Number(tempo),
    time_signature_hint: meter || null,
    minimum_note_seconds: Number(minimumNote),
    merge_gap_seconds: Number(mergeGap),
  });

  const playClip = async () => {
    if (!audio.current || rangeError || !playable) return;
    if (clipPlaying) {
      audio.current.pause();
      setClipPlaying(false);
      return;
    }
    try {
      audio.current.currentTime = startValue;
      await audio.current.play();
      setClipPlaying(true);
    } catch {
      setPreviewError(
        "Your browser could not play this audio. Prepare a playable copy with the studio decoder, then try again.",
      );
    }
  };
  useEffect(() => {
    if (!clipPlaying || endValue === null) return;
    let frame: number;
    const tick = () => {
      const element = audio.current;
      if (!element) return;
      if (element.currentTime >= endValue || element.paused) {
        element.pause();
        setClipPlaying(false);
        return;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [clipPlaying, endValue]);

  const submit = async () => {
    if (!file || !canSubmit) return;
    audio.current?.pause();
    setClipPlaying(false);
    setBusy(true);
    setPausedUpload(false);
    setError("");
    const controller = new AbortController();
    uploadController.current = controller;
    const body = new FormData();
    body.append("file", file);
    body.append("title", title.trim() || file.name);
    body.append("source", mode);
    body.append("input_mode", inputMode);
    body.append("clip_start_seconds", String(startValue));
    if (endEdited && endValue !== null)
      body.append("clip_end_seconds", String(endValue));
    if (instrument) body.append("instrument_hint", instrument);
    if (tempo !== "") body.append("tempo_hint", tempo);
    if (meter) body.append("time_signature_hint", meter);
    body.append("minimum_note_seconds", minimumNote);
    body.append("merge_gap_seconds", mergeGap);
    const options = JSON.stringify(
      Array.from(body.entries()).filter(([key]) => key !== "file"),
    );
    if (
      !submission.current ||
      submission.current.file !== file ||
      submission.current.options !== options
    )
      submission.current = { file, options, key: crypto.randomUUID() };
    try {
      let project: ProjectDetail;
      if (resumable) {
        const session = await uploadFile(
          file,
          metadata(),
          controller.signal,
          reportUpload,
          rememberUpload,
        );
        setUploadProgress((value) =>
          value
            ? {
                ...value,
                stage: "completing",
                message:
                  "Verifying uploaded audio and creating the background job…",
              }
            : value,
        );
        project = await completeUpload(session, metadata(), controller.signal);
        uploadSession.current = null;
      } else {
        setUploadProgress({
          stage: "uploading",
          confirmedBytes: 0,
          sentBytes: 0,
          totalBytes: file.size,
          message: "Uploading audio. Analysis has not started.",
        });
        project = await transfer<ProjectDetail>(
          "/projects",
          "POST",
          body,
          controller.signal,
          (sent, total) =>
            reportUpload({
              stage: "uploading",
              confirmedBytes: 0,
              sentBytes: sent,
              totalBytes: total,
              message:
                sent === total
                  ? "Upload sent. Waiting for the server to save your project…"
                  : "Uploading audio. Analysis has not started.",
            }),
          { "Idempotency-Key": submission.current.key },
        );
      }
      await onCreated();
      if (mounted.current) location.hash = `/projects/${project.id}`;
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted) {
        setError((reason as Error).message);
        setPausedUpload(true);
        setUploadProgress((value) =>
          value
            ? {
                ...value,
                stage: "paused",
                sentBytes: value.confirmedBytes,
                message: resumable
                  ? "Transfer paused. Retry to resume after the last verified chunk."
                  : "Submission interrupted. Retry safely using the same submission key.",
              }
            : value,
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <section className="upload-panel" aria-labelledby="upload-heading">
      <div className="panel-heading">
        <h2 id="upload-heading">Start with a sound</h2>
        <span>01 / CREATE</span>
      </div>
      <fieldset className="input-mode-selector" disabled={locked}>
        <legend>What is in the recording?</legend>
        <label className={inputMode === "solo" ? "selected" : ""}>
          <input
            type="radio"
            name="input-mode"
            aria-label="Solo voice or instrument"
            value="solo"
            checked={inputMode === "solo"}
            onChange={() => setInputMode("solo")}
          />
          <span>
            <strong>Solo voice or instrument</strong>
            <small>Detect notes directly from a focused performance.</small>
          </span>
        </label>
        <label className={inputMode === "mixed" ? "selected" : ""}>
          <input
            type="radio"
            name="input-mode"
            aria-label="Full song / mixed recording"
            value="mixed"
            checked={inputMode === "mixed"}
            onChange={() => setInputMode("mixed")}
          />
          <span>
            <strong>Full song / mixed recording</strong>
            <small>
              Separate stems, listen, then choose one part to transcribe.
            </small>
          </span>
        </label>
      </fieldset>
      {inputMode === "mixed" && (
        <p className="input-help mixed-input-note">
          Separation can leave other sounds in each stem. Vocal melody and bass
          line are supported; other pitched material is experimental. Drum
          notation is not supported.
        </p>
      )}
      <div
        className="input-tabs"
        role="tablist"
        aria-label="Audio source"
        onKeyDown={(event) => {
          if (
            ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) &&
            !locked
          ) {
            event.preventDefault();
            const next: Source =
              event.key === "Home"
                ? "upload"
                : event.key === "End"
                  ? "microphone"
                  : mode === "upload"
                    ? "microphone"
                    : "upload";
            changeMode(next);
            event.currentTarget
              .querySelectorAll<HTMLButtonElement>("[role=tab]")
              [next === "upload" ? 0 : 1]?.focus();
          }
        }}
      >
        <button
          role="tab"
          tabIndex={mode === "upload" ? 0 : -1}
          aria-selected={mode === "upload"}
          disabled={locked}
          className={mode === "upload" ? "active" : ""}
          onClick={() => changeMode("upload")}
        >
          <Upload size={16} />
          Upload audio
        </button>
        <button
          role="tab"
          tabIndex={mode === "microphone" ? 0 : -1}
          aria-selected={mode === "microphone"}
          disabled={locked}
          className={mode === "microphone" ? "active" : ""}
          onClick={() => changeMode("microphone")}
        >
          <Mic size={16} />
          Record audio
        </button>
      </div>
      {file ? (
        <div className="selected-file">
          <div className="file-info">
            <span className="file-icon">
              <FileAudio size={24} />
            </span>
            <div>
              <strong>{file.name}</strong>
              <small>
                {(file.size / 1_000_000).toFixed(2)} MB ·{" "}
                {mode === "microphone"
                  ? "Microphone recording"
                  : "Audio upload"}
                {recordedType && ` · ${recordedType}`}
              </small>
            </div>
            <button
              className="icon-button"
              disabled={locked}
              aria-label="Discard audio"
              title="Discard audio"
              onClick={() => void discardSource()}
            >
              <X size={18} />
            </button>
          </div>
          {(preparedPreview || preview) && (
            <audio
              key={preparedPreview || preview}
              ref={audio}
              controls
              src={preparedPreview || preview}
              preload="metadata"
              aria-label="Preview selected audio"
              onLoadedMetadata={(event) => metadataLoaded(event.currentTarget)}
              onDurationChange={(event) => metadataLoaded(event.currentTarget)}
              onCanPlay={(event) => {
                metadataLoaded(event.currentTarget);
                setPlayable(true);
                setPreviewError("");
              }}
              onError={() => {
                setPlayable(false);
                setPreviewError(
                  "This browser could not preview this audio. Use the studio decoder to prepare a playable copy; your original file will be preserved.",
                );
              }}
              onTimeUpdate={(event) => {
                if (
                  clipPlaying &&
                  endValue !== null &&
                  event.currentTarget.currentTime >= endValue
                ) {
                  event.currentTarget.pause();
                  setClipPlaying(false);
                }
              }}
              onPause={() => setClipPlaying(false)}
            />
          )}
          {previewError && (
            <div className="notice" role="status">
              {previewError}
            </div>
          )}
          {(!playable ||
            duration === null ||
            durationEstimated ||
            !!previewError) && (
            <div className="preview-preparation">
              <p>
                {preparing
                  ? "Preparing a playback copy. No transcription has started."
                  : durationEstimated
                    ? "The browser reported no exact duration for this recording. Timing is estimated from the recording clock. Prepare a preview for exact clip boundaries."
                    : "If the browser cannot read the audio or its duration, the studio decoder can prepare a preview."}
              </p>
              <button
                className="button secondary small"
                disabled={locked || !health}
                onClick={() => void preparePreview()}
              >
                {preparing ? (
                  <>
                    <LoaderCircle size={14} className="spin" />
                    Preparing preview…
                  </>
                ) : (
                  "Prepare playable preview"
                )}
              </button>
            </div>
          )}
          {preparedPreview && (
            <p className="input-help">
              Playing a normalized WAV copy. Submission preserves the original
              file.
            </p>
          )}
          <div className="clip-picker">
            <div className="clip-heading">
              <h3>Choose the part to transcribe</h3>
              <span>
                {duration === null
                  ? "Reading duration…"
                  : `${durationEstimated ? "About " : ""}${preciseTime(duration)} seconds total`}
              </span>
            </div>
            <div className="clip-fields">
              <div>
                <label htmlFor="clip-start">Clip start (seconds)</label>
                <input
                  id="clip-start"
                  type="number"
                  min="0"
                  max={duration ?? undefined}
                  step="0.001"
                  value={clipStart}
                  disabled={locked}
                  onChange={(event) => {
                    audio.current?.pause();
                    setClipStart(event.target.value);
                  }}
                />
                <button
                  className="text-link"
                  disabled={locked || !playable}
                  onClick={() => {
                    audio.current?.pause();
                    setClipStart(preciseTime(audio.current?.currentTime ?? 0));
                  }}
                >
                  Use current time as start
                </button>
              </div>
              <div>
                <label htmlFor="clip-end">Clip end (seconds)</label>
                <input
                  id="clip-end"
                  type="number"
                  min="0.1"
                  max={duration ?? undefined}
                  step="0.001"
                  value={clipEnd}
                  disabled={locked}
                  onChange={(event) => {
                    audio.current?.pause();
                    setClipEnd(event.target.value);
                    setEndEdited(event.target.value !== "");
                  }}
                  placeholder="End of recording"
                />
                <button
                  className="text-link"
                  disabled={locked || !playable}
                  onClick={() => {
                    audio.current?.pause();
                    setClipEnd(preciseTime(audio.current?.currentTime ?? 0));
                    setEndEdited(true);
                  }}
                >
                  Use current time as end
                </button>
              </div>
            </div>
            {rangeError && (
              <p className="field-error" role="alert">
                {rangeError}
              </p>
            )}
            <div className="clip-actions">
              <button
                className="button secondary small"
                disabled={locked || !playable || !!rangeError}
                onClick={() => void playClip()}
              >
                {clipPlaying ? (
                  <>
                    <Pause size={14} />
                    Pause selected clip
                  </>
                ) : (
                  <>
                    <Play size={14} />
                    Play selected clip
                  </>
                )}
              </button>
              <button
                className="text-link"
                disabled={locked || duration === null}
                onClick={() => {
                  audio.current?.pause();
                  setClipStart("0");
                  setClipEnd(duration === null ? "" : preciseTime(duration));
                  setEndEdited(false);
                }}
              >
                Use full recording
              </button>
            </div>
          </div>
          <label className="field-label" htmlFor="project-title">
            Project name
          </label>
          <input
            id="project-title"
            value={title}
            maxLength={120}
            disabled={locked}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Give your idea a name"
          />
          <details className="input-hints">
            <summary>Optional musical hints</summary>
            <p>
              Tempo and meter guide draft notation. The instrument is saved as a
              hint; it does not change pitch detection.
            </p>
            <div className="hint-fields">
              <div>
                <label htmlFor="instrument-hint">Instrument hint</label>
                <select
                  id="instrument-hint"
                  value={instrument}
                  disabled={locked}
                  onChange={(event) => setInstrument(event.target.value)}
                >
                  <option value="">No hint</option>
                  <option value="voice">Voice</option>
                  <option value="piano">Piano</option>
                  <option value="guitar">Guitar</option>
                  <option value="bass">Bass</option>
                  <option value="violin">Violin</option>
                  <option value="flute">Flute</option>
                  <option value="other">Other instrument</option>
                </select>
              </div>
              <div>
                <label htmlFor="tempo-hint">Tempo hint (BPM)</label>
                <input
                  id="tempo-hint"
                  type="number"
                  min="30"
                  max="300"
                  step="1"
                  value={tempo}
                  disabled={locked}
                  onChange={(event) => setTempo(event.target.value)}
                  placeholder="e.g. 120"
                />
              </div>
              <div>
                <label htmlFor="meter-hint">Time signature hint</label>
                <select
                  id="meter-hint"
                  value={meter}
                  disabled={locked}
                  onChange={(event) => setMeter(event.target.value)}
                >
                  <option value="">No hint</option>
                  {["2/4", "3/4", "4/4", "6/8", "9/8", "12/8"].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </div>
            </div>
            {tempoError && (
              <p className="field-error" role="alert">
                {tempoError}
              </p>
            )}
            <p>
              Tempo is in quarter-note beats per minute. Without hints, draft
              notation uses 120 BPM and 4/4.
            </p>
          </details>
          <details className="input-hints cleanup-settings">
            <summary>Advanced note cleanup</summary>
            <p>
              {inputMode === "mixed"
                ? "These note-cleanup hints apply after choosing a pitched stem; they do not change source separation. Vocal and bass adapters also use their documented melody/line cleanup."
                : "Keep the original detections and apply these settings to the editable performance notes. Cleanup does not quantize timing."}
            </p>
            <div className="hint-fields">
              <div>
                <label htmlFor="minimum-note">
                  Minimum note duration (seconds)
                </label>
                <input
                  id="minimum-note"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={minimumNote}
                  disabled={locked}
                  onChange={(event) => setMinimumNote(event.target.value)}
                />
              </div>
              <div>
                <label htmlFor="merge-gap">
                  Maximum fragment gap (seconds)
                </label>
                <input
                  id="merge-gap"
                  type="number"
                  min="0"
                  max="0.2"
                  step="0.01"
                  value={mergeGap}
                  disabled={locked}
                  onChange={(event) => setMergeGap(event.target.value)}
                />
              </div>
            </div>
            <p>
              Only appropriate same-pitch fragments are merged. Evidence of a
              new attack keeps repeated notes separate. Set the merge gap to
              zero to disable gap bridging.
            </p>
            {cleanupError && (
              <p className="field-error" role="alert">
                {cleanupError}
              </p>
            )}
          </details>
        </div>
      ) : mode === "upload" ? (
        <div
          className={`dropzone ${dragging ? "dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (event.dataTransfer.files.length > 1)
              setError("Choose one audio file per project.");
            else if (event.dataTransfer.files[0])
              selectFile(event.dataTransfer.files[0]);
          }}
        >
          <span className="upload-symbol">
            <Upload size={28} strokeWidth={1.4} />
            <span>
              <Music2 size={12} />
            </span>
          </span>
          <h3>Drop your audio here</h3>
          <p>or choose a recording from your computer</p>
          <button
            className="button secondary"
            onClick={() => input.current?.click()}
          >
            Choose audio file
            <Plus size={15} />
          </button>
          <small>
            WAV, MP3, M4A, FLAC, OGG & more
            <br />
            Up to {Number((maxBytes / 1_000_000).toFixed(2))} MB ·{" "}
            {formatDuration(maxSeconds)} per original recording
            <br />
            Playback and decoding depend on installed codec support.
          </small>
          <input
            className="visually-hidden"
            type="file"
            ref={input}
            accept=".wav,.mp3,.m4a,.flac,.ogg,.webm,.mp4"
            aria-label="Choose audio file"
            onChange={(event) => {
              if (event.target.files?.[0]) selectFile(event.target.files[0]);
              event.target.value = "";
            }}
          />
        </div>
      ) : (
        <div
          className={`dropzone recording-zone ${phase === "recording" ? "is-recording" : ""}`}
        >
          <span className="upload-symbol">
            <Mic size={29} strokeWidth={1.4} />
          </span>
          <h3>
            {phase === "recording" || phase === "stopping"
              ? formatDuration(elapsed)
              : "Let your melody in"}
          </h3>
          <p>
            {phase === "recording"
              ? "Recording from your microphone"
              : phase === "stopping"
                ? "Finishing your audio file…"
                : inputMode === "mixed"
                  ? "Record the mixed performance, then choose a separated part."
                  : "Sing, hum, or play a single instrument."}
          </p>
          <button
            className={`button ${phase === "recording" ? "danger" : "secondary"}`}
            disabled={phase === "requesting" || phase === "stopping"}
            onClick={() => (phase === "recording" ? stop() : void record())}
          >
            {phase === "requesting" ? (
              <>
                <LoaderCircle size={16} className="spin" />
                Opening microphone…
              </>
            ) : phase === "stopping" ? (
              <>
                <LoaderCircle size={16} className="spin" />
                Finishing recording…
              </>
            ) : phase === "recording" ? (
              <>
                <Square size={14} fill="currentColor" />
                Stop recording
              </>
            ) : (
              <>
                <Mic size={16} />
                Start recording
              </>
            )}
          </button>
          {phase === "requesting" && (
            <button
              className="text-link cancel-microphone"
              onClick={cancelMicrophone}
            >
              Cancel microphone request
            </button>
          )}
          <small>
            {phase === "recording"
              ? `Automatically stops near ${formatDuration(maxSeconds)}. Your audio has not been submitted.`
              : "Microphone access is requested only when you start.\nRecording requires HTTPS or localhost."}
          </small>
        </div>
      )}
      {uploadProgress && (
        <section className="upload-transfer" aria-label="Audio upload progress">
          <div className="upload-transfer-heading">
            <strong>
              {uploadProgress.stage === "paused"
                ? "Upload paused"
                : uploadProgress.stage === "checking"
                  ? "Checking saved upload"
                  : uploadProgress.stage === "completing"
                    ? "Upload complete"
                    : "Uploading audio"}
            </strong>
            <span>
              {Math.min(
                100,
                Math.floor(
                  (uploadProgress.sentBytes /
                    Math.max(1, uploadProgress.totalBytes)) *
                    100,
                ),
              )}
              % transferred
            </span>
          </div>
          <progress
            max={uploadProgress.totalBytes}
            value={uploadProgress.sentBytes}
            aria-label="Bytes transferred"
          />
          <p role="status">{uploadProgress.message}</p>
          <p>
            {(uploadProgress.sentBytes / 1_000_000).toFixed(2)} of{" "}
            {(uploadProgress.totalBytes / 1_000_000).toFixed(2)} MB transferred.{" "}
            {resumable
              ? `${(uploadProgress.confirmedBytes / 1_000_000).toFixed(2)} MB verified and saved by the server.`
              : "The server confirms the project once the request finishes."}
          </p>
          <div className="upload-transfer-actions">
            {(busy || preparing) && uploadProgress.stage !== "completing" && (
              <button className="button secondary small" onClick={pauseUpload}>
                Pause upload
              </button>
            )}
            {pausedUpload && !locked && (
              <button
                className="button secondary small"
                onClick={() =>
                  void (playable && duration !== null
                    ? submit()
                    : preparePreview())
                }
              >
                Resume upload
              </button>
            )}
            {uploadSession.current && !busy && !preparing && (
              <button
                className="text-link"
                disabled={discarding}
                onClick={() => void discardSource()}
              >
                {discarding ? "Discarding upload…" : "Discard upload"}
              </button>
            )}
          </div>
        </section>
      )}
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      <div className="scope-note">
        <AudioLines size={18} />
        <div>
          <strong>
            {inputMode === "mixed"
              ? "Separate first, choose a part"
              : "Single voice or instrument"}
          </strong>
          <span>
            {inputMode === "mixed"
              ? "Estimated performed notes. No accompaniment is generated."
              : "Best for a focused melody or solo performance."}
          </span>
        </div>
        <Check size={16} />
      </div>
      <p className="analysis-explanation">
        Listen back and choose a clip before submitting. Analysis starts only
        after recording has stopped and you select{" "}
        {inputMode === "mixed" ? "Separate recording" : "Create transcription"}.
        This is not live streaming transcription.
      </p>
      <button
        className="button primary submit"
        disabled={!canSubmit}
        onClick={() => void submit()}
      >
        {busy ? (
          <>
            <LoaderCircle size={18} className="spin" />
            {uploadProgress?.stage === "completing"
              ? "Creating your project…"
              : "Uploading your audio…"}
          </>
        ) : (
          <>
            {inputMode === "mixed"
              ? "Separate recording"
              : "Create transcription"}
            <ArrowRight size={17} />
          </>
        )}
      </button>
      <p className="privacy-caption">
        <ShieldCheck size={13} />
        Processed by this studio’s worker. Audio is stored privately.
      </p>
      {!health && (
        <p className="setup-caption">
          Start the local API to create a project. See{" "}
          <a href="#/guide">Getting started</a>.
        </p>
      )}
      {health &&
        inputMode === "mixed" &&
        health.capabilities.separation === false && (
          <p className="setup-caption">
            Source separation is not ready in the analysis worker. Install the
            separator and model as described in the README before processing a
            mixed recording.
          </p>
        )}
      {health &&
        (!health.worker.online ||
          !health.capabilities.ffmpeg ||
          !health.capabilities.transcription) && (
          <p className="setup-caption">
            You can save a recording now. Analysis needs{" "}
            {!health.capabilities.ffmpeg ? "FFmpeg, " : ""}
            {!health.capabilities.transcription ? "Basic Pitch, and " : ""}a
            ready worker. <a href="#/guide">Setup guide</a>
          </p>
        )}
    </section>
  );
}

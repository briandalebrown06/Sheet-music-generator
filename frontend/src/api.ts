export type Status =
  | "queued"
  | "processing"
  | "completed"
  | "failed"
  | "canceled";
export type Source = "upload" | "microphone";
export type InputMode = "solo" | "mixed";
export interface Project {
  id: string;
  title: string;
  source: Source;
  original_filename: string;
  created_at: string;
  updated_at: string;
  status: Status;
  duration_seconds: number | null;
  original_duration_seconds: number | null;
  clip_start_seconds: number;
  clip_end_seconds: number | null;
  clip_duration_seconds: number | null;
  instrument_hint: string | null;
  tempo_hint: number | null;
  time_signature_hint: string | null;
  analysis_available: boolean;
  note_count: number | null;
  input_mode?: InputMode;
  parent_project_id?: string | null;
  stem_category?: string | null;
  transcription_profile?: "solo" | "vocals" | "bass" | "other";
  source_origin_seconds?: number;
  output_mode?: "transcription" | "accompaniment";
  arrangement_source_id?: string | null;
}
export interface SeparatedStem {
  id: string;
  category: string;
  label: string;
  duration_seconds: number;
  sample_rate: number;
  channels: number;
  audio_url: string;
  transcription_supported: boolean;
  profile: "vocals" | "bass" | "other" | null;
  detail: string;
  child_project_id: string | null;
}
export interface SeparationState {
  stems: SeparatedStem[];
  manifest: Record<string, unknown> | null;
  children: Project[];
}
export interface Job {
  id: string;
  status: Status;
  stage: string;
  error: string | null;
  created_at: string;
  updated_at: string;
}
export interface NoteEvent {
  id: string;
  part_id: string;
  voice_id?: string | null;
  new_note?: boolean;
  derived_from_ids?: string[];
  model_score: number | null;
  provenance: "detected" | "edited" | "manually_added";
  source_note_ids: string[];
  original_timing: {
    start: number;
    end: number;
    pitch: number;
    velocity: number | null;
  } | null;
  score_timing: null;
  start: number;
  end: number;
  pitch: number;
  velocity: number | null;
  hand?: "left" | "right" | null;
  locked?: boolean;
  key_release_seconds?: number | null;
  pedal_sustained?: boolean;
  fingering?: number | null;
  role?:
    | "bass"
    | "harmony"
    | "riff"
    | "countermelody"
    | "melody"
    | "texture"
    | "vocal_cue"
    | null;
}
export interface PianoSettings {
  profile: "comfortable_accompaniment";
  left_reach: number;
  right_reach: number;
  max_keys_per_hand: number;
  target_keys_per_hand: number;
  max_jump_semitones: number;
  max_jump_semitones_per_second: number;
  attack_tolerance_seconds: number;
  include_vocal_cue: boolean;
}
export interface PlayabilityIssue {
  code: string;
  severity: "error" | "warning";
  measure: number;
  hand: "left" | "right" | "both" | "unassigned";
  note_ids: string[];
  detail: string;
}
export interface PlayabilityReport {
  density?: {
    target_keys_per_hand: number;
    max_keys_per_hand: number;
    max_left_keys: number;
    max_right_keys: number;
    over_target_frames: number;
    target_met: boolean;
    protected_exceptions: {
      measure: number;
      hand: string;
      note_ids: string[];
      key_count: number;
      protected_keys: number;
      detail: string;
    }[];
  };
  issues: PlayabilityIssue[];
  summary: {
    passed_automated_constraints: boolean;
    error_count: number;
    warning_count: number;
    note_count: number;
    max_left_span: number;
    max_right_span: number;
    max_left_keys: number;
    max_right_keys: number;
  };
  limitations: string[];
  settings?: PianoSettings;
}
export interface PlayabilityState {
  settings: PianoSettings;
  report: PlayabilityReport;
  notes_revision: number;
  score_revision: number;
}
export interface ProjectDetail extends Project {
  jobs: Job[];
  notes: NoteEvent[] | null;
  exports: string[];
  engine: string;
  max_attempts: number;
  retries_remaining: number;
  notes_revision: number;
  cleanup: { minimum_note_seconds: number; merge_gap_seconds: number };
  artifacts: string[];
  result_schema_version: number;
  arrangement?: {
    edition_mode?: "faithful" | "accompaniment" | "solo_piano";
    edition_limitations?: string[];
    melody_octave_shift?: number;
    settings: PianoSettings;
    changes: unknown[];
    report: PlayabilityReport;
    before_report: PlayabilityReport;
    readability_changes: unknown[];
    source_notes_revision: number;
    source_score_revision: number;
  } | null;
}
export interface Health {
  status: string;
  engine: string;
  capabilities: {
    ffmpeg: boolean;
    transcription: boolean;
    notation: boolean;
    pdf?: boolean;
    separation?: boolean;
    resumable_upload?: boolean;
  };
  pdf_export?: { available: boolean; detail: string };
  worker: { online: boolean; last_seen_at: string | null };
  limits: { max_upload_bytes: number; max_duration_seconds: number };
}
export interface ScoreSettings {
  tempo_bpm: number | null;
  time_signature: string;
  key_signature: number;
  first_downbeat_seconds: number;
  pickup_beats: string;
  vocal_clef?: "auto" | "treble" | "bass";
  melody_quantization?: "same" | "sixteenth";
  quantization:
    | "quarter"
    | "eighth"
    | "sixteenth"
    | "eighth_triplet"
    | "sixteenth_triplet";
  clef: "auto" | "treble" | "bass" | "alto" | "tenor";
  transpose_semitones: number;
  piano_split: boolean;
  split_pitch: number;
  staff_overrides: Record<string, "treble" | "bass">;
  confirmed_fields: string[];
  estimated_fields?: string[];
  readability_mode?: "standard" | "accompaniment";
}
export interface RationalBeat {
  numerator: number;
  denominator: number;
}
export interface ScoreEvent {
  id: string;
  kind: "note" | "chord" | "rest";
  offset: RationalBeat;
  duration: RationalBeat;
  pitches: number[];
  source_note_ids: string[];
  tie: "start" | "continue" | "stop" | null;
  velocity?: number;
  pitch_velocities?: Record<string, number>;
}
export interface CanonicalScore {
  schema_version: number;
  title: string;
  beat_unit: "quarter";
  origin_seconds: number;
  source_notes_revision: number;
  source_notes: NoteEvent[];
  parts: {
    id: string;
    name: string;
    is_cue?: boolean;
    staves: {
      id: string;
      clef: string;
      measures: {
        number: number;
        start: RationalBeat;
        duration: RationalBeat;
        is_pickup: boolean;
        voices: { id: string; events: ScoreEvent[] }[];
      }[];
    }[];
  }[];
  settings: ScoreSettings;
  assumptions: string[];
  warnings: string[];
  tempo?: { bpm?: number; source?: string; method?: string };
  pedal_spans?: { part_id: string; start: RationalBeat; end: RationalBeat }[];
  readability?: {
    issues: {
      code: string;
      severity: string;
      measure: number;
      part_id: string;
      staff: string;
      hand: string[] | null;
      note_ids: string[];
      detail: string;
    }[];
    statistics: {
      visible_rests: number;
      hidden_rests: number;
      tied_fragments: number;
      sounding_events: number;
      maximum_voices_per_staff: number;
      maximum_ledger_lines: number;
    };
    limitations: string[];
  };
}
export interface ScoreState {
  score: CanonicalScore | null;
  revision: number;
  source_notes_revision: number | null;
  stale: boolean;
  defaults: ScoreSettings;
  musicxml?: string;
}
export interface ScorePreview {
  score: CanonicalScore;
  musicxml: string;
  source_notes_revision: number;
}
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export interface AuthSession {
  authentication_required: boolean;
  user: { id: string; username: string } | null;
  csrf_token: string | null;
}
let csrfToken: string | null = null;
let accountId = "local";
export function setApiSession(session: AuthSession | null) {
  csrfToken = session?.csrf_token ?? null;
  accountId =
    session?.user?.id ??
    (session?.authentication_required ? "anonymous" : "local");
}
// An owner namespace for non-secret draft metadata, never an authentication token.
export function apiAccountId() {
  return accountId;
}
export function apiRequestHeaders(
  method: string,
  initial?: HeadersInit,
): Headers {
  const headers = new Headers(initial);
  if (
    !new Set(["GET", "HEAD", "OPTIONS"]).has(method.toUpperCase()) &&
    csrfToken
  )
    headers.set("X-CSRF-Token", csrfToken);
  return headers;
}
export async function apiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = apiRequestHeaders(init.method ?? "GET", init.headers);
  const response = await fetch(`/api${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401 && !path.startsWith("/auth/"))
    window.dispatchEvent(new Event("scorecraft-session-expired"));
  return response;
}
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await apiFetch(path, init);
  } catch {
    throw new Error(
      "The server is unavailable. Check your connection and try again.",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      typeof body?.detail === "string"
        ? body.detail
        : `The server could not complete this request (${response.status}).`,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function formatDuration(seconds: number | null) {
  if (seconds === null) return "—";
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}
export function formatDate(date: string) {
  return new Date(date).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
export function pitchName(pitch: number) {
  return `${["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"][pitch % 12]}${Math.floor(pitch / 12) - 1}`;
}

// Keep comparison, undo/save state, and rendered-score invalidation aligned.
export function noteEditValues(notes: NoteEvent[]) {
  return JSON.stringify(
    notes.map((n) => [
      n.id,
      n.pitch,
      n.start,
      n.end,
      n.velocity,
      n.part_id,
      n.voice_id ?? null,
      n.hand ?? null,
      n.locked ?? null,
      n.key_release_seconds ?? null,
      n.pedal_sustained ?? false,
      n.role ?? null,
      n.fingering ?? null,
    ]),
  );
}

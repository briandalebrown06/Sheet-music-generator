from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / ".data")
    max_upload_bytes: int = 500_000_000
    max_duration_seconds: int = 3600
    upload_chunk_bytes: int = 8 * 1024 * 1024
    upload_ttl_seconds: int = 86400
    max_active_uploads_per_user: int = 3
    upload_chunk_rate_limit_per_minute: int = 120
    ffmpeg_binary: str = "ffmpeg"
    basic_pitch_runtime: str = "onnx"
    decode_timeout_seconds: int = 600
    decode_cpu_seconds: int = 300
    decode_memory_mb: int = 512
    silence_threshold_dbfs: float = -70.0
    max_job_attempts: int = 3
    job_timeout_seconds: int = 7200
    minimum_note_seconds: float = 0.08
    merge_gap_seconds: float = 0.03
    worker_poll_seconds: float = 1.0
    lease_seconds: int = 60
    pdf_timeout_seconds: int = 120
    pdf_memory_mb: int = 1024
    separation_model_dir: Path = field(default_factory=lambda: ROOT / ".data" / "models" / "umxhq")
    separation_segment_seconds: float = 10.0
    separation_overlap: float = 0.25
    separation_threads: int = 2
    separation_timeout_seconds: int = 14400
    separation_memory_mb: int = 4096
    max_separation_storage_bytes: int = 16384 * 1024 * 1024
    app_mode: str = "local"
    auth_enabled: bool = False
    public_origin: str | None = None
    session_ttl_seconds: int = 28800
    api_rate_limit_per_minute: int = 300
    write_rate_limit_per_minute: int = 60
    login_rate_limit_per_minute: int = 10
    max_projects_per_user: int = 100
    max_storage_mb_per_user: int = 32768
    max_active_jobs_per_user: int = 3
    max_jobs_per_day: int = 30
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:8000", "http://127.0.0.1:8000",
    )

    @property
    def authentication_required(self) -> bool:
        return self.app_mode == "hosted" or self.auth_enabled

    @property
    def session_secure(self) -> bool:
        return self.app_mode == "hosted"

    @property
    def session_cookie_name(self) -> str:
        return "__Host-scorecraft_session" if self.session_secure else "scorecraft_session"

    def validate_access(self) -> None:
        if self.app_mode not in {"local", "hosted"}:
            raise ValueError("APP_MODE must be local or hosted.")
        if self.app_mode == "hosted":
            if not self.public_origin:
                raise ValueError("Hosted mode requires PUBLIC_ORIGIN with an HTTPS origin.")
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or self.public_origin.endswith("/")):
                raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without a trailing slash, credentials, path, query, or fragment.")
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("PUBLIC_ORIGIN has an invalid port.") from exc
        elif self.public_origin:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("Local PUBLIC_ORIGIN must be an exact HTTP(S) loopback origin.")
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("PUBLIC_ORIGIN has an invalid port.") from exc
        for value, minimum, maximum, name in (
            (self.session_ttl_seconds, 300, 604800, "SESSION_TTL_SECONDS"),
            (self.api_rate_limit_per_minute, 10, 10000, "API_RATE_LIMIT_PER_MINUTE"),
            (self.write_rate_limit_per_minute, 1, 1000, "WRITE_RATE_LIMIT_PER_MINUTE"),
            (self.login_rate_limit_per_minute, 1, 100, "LOGIN_RATE_LIMIT_PER_MINUTE"),
            (self.max_projects_per_user, 1, 10000, "MAX_PROJECTS_PER_USER"),
            (self.max_storage_mb_per_user, 16, 1048576, "MAX_STORAGE_MB_PER_USER"),
            (self.max_active_jobs_per_user, 1, 100, "MAX_ACTIVE_JOBS_PER_USER"),
            (self.max_jobs_per_day, 1, 10000, "MAX_JOBS_PER_DAY"),
            (self.max_upload_bytes, 1, 2_000_000_000, "MAX_UPLOAD_BYTES"),
            (self.max_duration_seconds, 1, 3600, "MAX_DURATION_SECONDS"),
            (self.upload_chunk_bytes, 65536, 8 * 1024 * 1024, "UPLOAD_CHUNK_BYTES"),
            (self.upload_ttl_seconds, 60, 604800, "UPLOAD_TTL_SECONDS"),
            (self.max_active_uploads_per_user, 1, 100, "MAX_ACTIVE_UPLOADS_PER_USER"),
            (self.upload_chunk_rate_limit_per_minute, 10, 10000, "UPLOAD_CHUNK_RATE_LIMIT_PER_MINUTE"),
        ):
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be {minimum}–{maximum}.")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "projects.sqlite3"

    def prepare(self) -> None:
        self.validate_access()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.data_dir / "projects").mkdir(exist_ok=True, mode=0o700)
        (self.data_dir / ".scratch").mkdir(exist_ok=True, mode=0o700)
        (self.data_dir / ".request-spool").mkdir(exist_ok=True, mode=0o700)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        data = Path(os.getenv("DATA_DIR", ".data")).expanduser()
        if not data.is_absolute():
            data = ROOT / data
        # Explicit bytes avoid confusing the requested decimal 500 MB with
        # 500 MiB. Preserve the historical MiB env override for older installs.
        legacy_upload = os.getenv("MAX_UPLOAD_MB")
        upload = int(os.getenv("MAX_UPLOAD_BYTES", str(int(legacy_upload) * 1024 * 1024 if legacy_upload is not None else 500_000_000)))
        duration = int(os.getenv("MAX_DURATION_SECONDS", "3600"))
        timeout = int(os.getenv("DECODE_TIMEOUT_SECONDS", "600"))
        cpu = int(os.getenv("DECODE_CPU_SECONDS", "300"))
        memory = int(os.getenv("DECODE_MEMORY_MB", "512"))
        silence = float(os.getenv("SILENCE_THRESHOLD_DBFS", "-70"))
        if not 1 <= timeout <= 600 or not 1 <= cpu <= 600 or not 128 <= memory <= 4096:
            raise ValueError("Decoder limits: timeout/CPU must be 1–600 seconds; memory must be 128–4096 MB.")
        if not -100 <= silence <= -30:
            raise ValueError("SILENCE_THRESHOLD_DBFS must be between -100 and -30.")
        attempts = int(os.getenv("MAX_JOB_ATTEMPTS", "3"))
        job_timeout = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))
        minimum_note = float(os.getenv("MINIMUM_NOTE_SECONDS", "0.08"))
        merge_gap = float(os.getenv("MERGE_GAP_SECONDS", "0.03"))
        if not 1 <= attempts <= 10 or not 1 <= job_timeout <= 86400:
            raise ValueError("MAX_JOB_ATTEMPTS must be 1–10; JOB_TIMEOUT_SECONDS must be 1–86400.")
        if not 0 <= minimum_note <= 1 or not 0 <= merge_gap <= 0.2:
            raise ValueError("MINIMUM_NOTE_SECONDS must be 0–1; MERGE_GAP_SECONDS must be 0–0.2.")
        runtime = os.getenv("BASIC_PITCH_RUNTIME", "onnx")
        if runtime not in {"onnx", "coreml", "tf", "tflite"}:
            raise ValueError("BASIC_PITCH_RUNTIME must be onnx, coreml, tf, or tflite.")
        pdf_timeout = int(os.getenv("PDF_TIMEOUT_SECONDS", "120"))
        pdf_memory = int(os.getenv("PDF_MEMORY_MB", "1024"))
        if not 1 <= pdf_timeout <= 600 or not 256 <= pdf_memory <= 4096:
            raise ValueError("PDF_TIMEOUT_SECONDS must be 1–600; PDF_MEMORY_MB must be 256–4096.")
        model_dir = Path(os.getenv("SEPARATION_MODEL_DIR", ".data/models/umxhq")).expanduser()
        if not model_dir.is_absolute():
            model_dir = ROOT / model_dir
        segment = float(os.getenv("SEPARATION_SEGMENT_SECONDS", "10"))
        overlap = float(os.getenv("SEPARATION_OVERLAP", "0.25"))
        threads = int(os.getenv("SEPARATION_THREADS", "2"))
        separation_timeout = int(os.getenv("SEPARATION_TIMEOUT_SECONDS", "14400"))
        separation_memory = int(os.getenv("SEPARATION_MEMORY_MB", "4096"))
        separation_storage = int(os.getenv("MAX_SEPARATION_STORAGE_MB", "16384"))
        if not 2 <= segment <= 30 or not 0 <= overlap <= 0.5 or not 1 <= threads <= 8:
            raise ValueError("Separation segment must be 2–30 seconds, overlap 0–0.5, and threads 1–8.")
        if not 1 <= separation_timeout <= 86400 or not 512 <= separation_memory <= 32768 or not 16 <= separation_storage <= 1048576:
            raise ValueError("Separation timeout must be 1–86400 seconds, memory 512–32768 MiB, and storage 16–1048576 MiB.")
        mode = os.getenv("APP_MODE", "local")
        auth_text = os.getenv("AUTH_ENABLED", "true" if mode == "hosted" else "false").lower()
        if auth_text not in {"true", "false"}:
            raise ValueError("AUTH_ENABLED must be true or false.")
        if mode == "hosted" and auth_text != "true":
            raise ValueError("Hosted mode cannot disable authentication.")
        public_origin = os.getenv("PUBLIC_ORIGIN") or None
        origins = (public_origin,) if mode == "hosted" and public_origin else cls.__dataclass_fields__["allowed_origins"].default
        if mode == "local" and public_origin:
            origins = tuple(dict.fromkeys((*origins, public_origin)))
        result = cls(data_dir=data.resolve(), app_mode=mode, auth_enabled=auth_text == "true",
                   public_origin=public_origin, allowed_origins=origins,
                   session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "28800")),
                   api_rate_limit_per_minute=int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "300")),
                   write_rate_limit_per_minute=int(os.getenv("WRITE_RATE_LIMIT_PER_MINUTE", "60")),
                   login_rate_limit_per_minute=int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "10")),
                   max_projects_per_user=int(os.getenv("MAX_PROJECTS_PER_USER", "100")),
                   max_storage_mb_per_user=int(os.getenv("MAX_STORAGE_MB_PER_USER", "32768")),
                   max_active_jobs_per_user=int(os.getenv("MAX_ACTIVE_JOBS_PER_USER", "3")),
                   max_jobs_per_day=int(os.getenv("MAX_JOBS_PER_DAY", "30")), max_upload_bytes=upload,
                   max_duration_seconds=duration,
                   upload_chunk_bytes=int(os.getenv("UPLOAD_CHUNK_BYTES", str(8 * 1024 * 1024))),
                   upload_ttl_seconds=int(os.getenv("UPLOAD_TTL_SECONDS", "86400")),
                   max_active_uploads_per_user=int(os.getenv("MAX_ACTIVE_UPLOADS_PER_USER", "3")),
                   upload_chunk_rate_limit_per_minute=int(os.getenv("UPLOAD_CHUNK_RATE_LIMIT_PER_MINUTE", "120")),
                   ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"), basic_pitch_runtime=runtime,
                   decode_timeout_seconds=timeout, decode_cpu_seconds=cpu,
                   decode_memory_mb=memory, silence_threshold_dbfs=silence,
                   max_job_attempts=attempts, job_timeout_seconds=job_timeout,
                   minimum_note_seconds=minimum_note, merge_gap_seconds=merge_gap,
                   pdf_timeout_seconds=pdf_timeout, pdf_memory_mb=pdf_memory,
                   separation_model_dir=model_dir.resolve(), separation_segment_seconds=segment,
                   separation_overlap=overlap, separation_threads=threads,
                   separation_timeout_seconds=separation_timeout, separation_memory_mb=separation_memory,
                   max_separation_storage_bytes=separation_storage * 1024 * 1024)
        result.validate_access()
        return result

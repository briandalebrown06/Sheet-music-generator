import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_mb: int = 50
    max_audio_seconds: int = 300
    frontend_origin: str = "http://localhost:5173"
    engine: str = "basic-pitch"

    def __post_init__(self):
        if self.max_upload_mb < 1 or not 1 <= self.max_audio_seconds <= 600:
            raise ValueError("Upload limit must be positive; duration must be 1–600 seconds.")

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.getenv("DATA_DIR", "./data")).resolve(),
            int(os.getenv("MAX_UPLOAD_MB", "50")),
            int(os.getenv("MAX_AUDIO_SECONDS", "300")),
            os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
            os.getenv("TRANSCRIPTION_ENGINE", "basic-pitch"),
        )

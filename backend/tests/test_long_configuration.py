"""Independent byte/duration configuration boundaries; no fake model inference."""
from dataclasses import replace

import pytest

from backend.app.config import Settings
from backend.app.score import ScoreSettings, build_score


def test_defaults_use_decimal_500_mb_and_one_hour():
    settings = Settings()
    assert settings.max_upload_bytes == 500_000_000
    assert settings.max_duration_seconds == 3600
    assert settings.upload_chunk_bytes <= 8 * 1024 * 1024
    assert settings.job_timeout_seconds >= 7200
    assert settings.max_separation_storage_bytes >= 8 * 1024**3


def test_byte_setting_precedence_and_legacy_mib_compatibility(monkeypatch):
    monkeypatch.setattr("backend.app.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    monkeypatch.delenv("MAX_DURATION_SECONDS", raising=False)
    monkeypatch.setenv("MAX_UPLOAD_MB", "25")
    assert Settings.from_env().max_upload_bytes == 25 * 1024 * 1024
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "500000000")
    value = Settings.from_env()
    assert value.max_upload_bytes == 500_000_000
    assert value.max_duration_seconds == 3600
    monkeypatch.setenv("MAX_DURATION_SECONDS", "900")
    value = Settings.from_env()
    assert value.max_upload_bytes == 500_000_000 and value.max_duration_seconds == 900


@pytest.mark.parametrize("changes", [{"max_upload_bytes": 0}, {"max_duration_seconds": 3601},
    {"max_duration_seconds": 0}, {"upload_chunk_bytes": 8 * 1024 * 1024 + 1},
    {"upload_ttl_seconds": 0}, {"max_active_uploads_per_user": 0}])
def test_limits_reject_unbounded_or_invalid_configuration(changes):
    with pytest.raises(ValueError):
        replace(Settings(), **changes).validate_access()


def test_one_hour_score_retains_final_measure_instead_of_old_thousand_bar_cap():
    # Explicit notation unit fixture, not model detections or an accuracy test.
    note = {"id": "DEVELOPMENT-last-note", "pitch": 72, "start": 3599, "end": 3599.9, "velocity": 80}
    score = build_score([note], ScoreSettings(tempo_bpm=120), title="DEVELOPMENT hour-end score", clip_duration_seconds=3600)
    measures = score["parts"][0]["staves"][0]["measures"]
    assert len(measures) == 1800
    assert any(72 in event["pitches"] for voice in measures[-1]["voices"] for event in voice["events"])


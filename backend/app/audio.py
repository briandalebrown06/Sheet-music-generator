from __future__ import annotations

import array
import math
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".webm"}
SAMPLE_RATE = 22050
MIN_CLIP_SECONDS = 0.1


class AnalysisError(RuntimeError):
    """An actionable, safe-to-display analysis failure."""


@dataclass(frozen=True)
class PreparedAudio:
    original_duration: float
    clip_start: float
    clip_end: float

    @property
    def clip_duration(self) -> float:
        return self.clip_end - self.clip_start


def sniff_audio(header: bytes) -> str | None:
    # Admission check only. The worker still decodes the ENTIRE original stream.
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    if header[:4] == b"fLaC":
        return "audio/flac"
    if header[:4] == b"OggS":
        return "audio/ogg"
    if header[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    if header[4:8] == b"ftyp":
        return "audio/mp4"
    if header[:3] == b"ID3" or (len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224):
        return "audio/mpeg"
    return None


def normalize_audio(source: Path, target: Path, settings: Settings, *, channels: int = 1,
                    sample_rate: int = SAMPLE_RATE) -> float:
    """Decode and validate the whole bounded source, never trust client metadata.

    A one-second sentinel detects over-duration input without decoding an
    unbounded stream. decode_exec applies OS process limits without preexec_fn
    in a threaded server; FFmpeg limits also apply where OS limits are absent.
    """
    binary = shutil.which(settings.ffmpeg_binary)
    if not binary:
        raise AnalysisError("FFmpeg is not installed. Install FFmpeg or set FFMPEG_BINARY in .env, restart the API and worker, then retry.")
    binary = str(Path(binary).resolve())
    if channels not in {1, 2} or sample_rate not in {SAMPLE_RATE, 44100}:
        raise ValueError("Only controlled analysis PCM formats are supported.")
    output_limit = (settings.max_duration_seconds + 1) * sample_rate * channels * 2 + 65536
    command = [
        sys.executable, "-m", "backend.app.decode_exec",
        "--cpu-seconds", str(settings.decode_cpu_seconds),
        "--file-bytes", str(output_limit),
        "--memory-mb", str(settings.decode_memory_mb), "--",
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-xerror", "-max_error_rate", "0", "-max_alloc", str(64 * 1024 * 1024),
        "-timelimit", str(settings.decode_cpu_seconds), "-threads", "1",
        "-filter_threads", "1", "-filter_complex_threads", "1",
        "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "wav,mp3,mov,matroska,webm,ogg,flac",
        "-err_detect", "explode", "-i", str(source), "-map", "0:a:0",
        "-vn", "-sn", "-dn", "-t", str(settings.max_duration_seconds + 1),
        "-ac", str(channels), "-ar", str(sample_rate), "-threads", "1",
        "-c:a", "pcm_s16le", "-fs", str(output_limit), "-f", "wav", str(target),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=settings.decode_timeout_seconds)
        with wave.open(str(target), "rb") as audio:
            if audio.getnchannels() != channels or audio.getsampwidth() != 2 or audio.getframerate() != sample_rate:
                raise AnalysisError("The decoded audio has an unexpected format. Try another recording.")
            duration = audio.getnframes() / audio.getframerate()
        if target.stat().st_size > output_limit:
            raise AnalysisError("The decoded audio exceeded the resource limit. Try a shorter recording.")
        if duration > settings.max_duration_seconds:
            raise AnalysisError(f"This recording exceeds the {settings.max_duration_seconds}-second limit. Trim the original and upload again.")
        if duration < MIN_CLIP_SECONDS:
            raise AnalysisError("The recording is empty or shorter than 0.1 seconds.")
        return duration
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise AnalysisError("Audio decoding exceeded the time limit. Try a shorter recording.") from exc
    except (subprocess.CalledProcessError, OSError, wave.Error, EOFError) as exc:
        target.unlink(missing_ok=True)
        raise AnalysisError("This recording is corrupt, unsupported by the installed FFmpeg decoder, or exceeded decoding resource limits. Try exporting a valid WAV, MP3, M4A, or FLAC file.") from exc
    except AnalysisError:
        target.unlink(missing_ok=True)
        raise


def validate_clip(start: float, end: float | None, duration: float) -> tuple[float, float]:
    if not math.isfinite(start) or (end is not None and not math.isfinite(end)):
        raise AnalysisError("Clip start and end must be finite numbers of seconds.")
    if start < 0 or start >= duration:
        raise AnalysisError("The clip start is outside the decoded recording.")
    stop = duration if end is None else end
    # Browsers round container durations; tolerate at most 50 ms, never silently
    # clamp a materially invalid selection. Resolve to actual PCM frame bounds.
    if stop > duration + 0.05:
        raise AnalysisError("The clip end is beyond the decoded recording. Choose a shorter range.")
    stop = min(stop, duration)
    if stop - start < MIN_CLIP_SECONDS - 1e-9:
        raise AnalysisError("Select at least 0.1 seconds of audio, with the end after the start.")
    return start, stop


def prepare_analysis_audio(source: Path, target: Path, settings: Settings,
                           start: float = 0, end: float | None = None, *, channels: int = 1,
                           sample_rate: int = SAMPLE_RATE) -> PreparedAudio:
    decoded = target.with_name("decoded-original.wav")
    try:
        duration = normalize_audio(source, decoded, settings, channels=channels, sample_rate=sample_rate)
        start, end = validate_clip(start, end, duration)
        with wave.open(str(decoded), "rb") as original:
            start_frame = round(start * sample_rate)
            end_frame = min(original.getnframes(), round(end * sample_rate))
            original.setpos(start_frame)
            remaining = end_frame - start_frame
            total, sum_squares, peak = 0, 0, 0
            try:
                import numpy as np
            except ImportError:
                np = None
            with wave.open(str(target), "wb") as clip:
                clip.setnchannels(channels)
                clip.setsampwidth(2)
                clip.setframerate(sample_rate)
                while remaining:
                    chunk = original.readframes(min(remaining, sample_rate))
                    if not chunk:
                        raise AnalysisError("The decoded recording ended unexpectedly. Try another file.")
                    clip.writeframesraw(chunk)
                    if np is not None:
                        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float64)
                        sum_squares += float(np.dot(samples, samples))
                        peak = max(peak, float(np.max(np.abs(samples), initial=0)))
                    else:
                        samples = array.array("h", chunk)
                        if sys.byteorder != "little":
                            samples.byteswap()
                        sum_squares += sum(value * value for value in samples)
                        peak = max(peak, max(abs(value) for value in samples))
                    total += len(samples)
                    remaining -= len(samples) // channels
        # Reject essentially silent clips, not merely quiet notes with pauses.
        # BOTH average level and peak must be below the configured floor.
        floor = 32768 * 10 ** (settings.silence_threshold_dbfs / 20)
        rms = math.sqrt(sum_squares / max(1, total))
        if rms <= floor and peak <= floor:
            raise AnalysisError("The selected clip is silent or below the configured silence threshold. Choose a section with audible music or record again.")
        return PreparedAudio(duration, start_frame / sample_rate, end_frame / sample_rate)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        decoded.unlink(missing_ok=True)

from typing import Protocol


class TranscriptionEngine(Protocol):
    def transcribe(self, audio_path: str) -> list[dict]: ...


class BasicPitchEngine:
    def transcribe(self, audio_path: str) -> list[dict]:
        try:
            from basic_pitch.inference import predict
        except ImportError as exc:
            raise RuntimeError(
                "Basic Pitch is not installed. Use the optional Python 3.11 analysis environment "
                "described in README.md, then retry."
            ) from exc
        _, _, events = predict(audio_path)
        return [
            {"start": float(start), "end": float(end), "pitch": int(pitch),
             "amplitude": float(amplitude)}
            for start, end, pitch, amplitude, _ in events
        ]


def get_engine(name: str) -> TranscriptionEngine:
    if name != "basic-pitch":
        raise RuntimeError(f"Unsupported transcription engine: {name}")
    return BasicPitchEngine()

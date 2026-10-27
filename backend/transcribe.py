"""Isolated inference entrypoint; the worker enforces a process timeout."""
import json
import sys
from pathlib import Path

from backend.engines import get_engine


def main():
    audio, output, engine = sys.argv[1:]
    try:
        notes = get_engine(engine).transcribe(audio)
        Path(output).write_text(json.dumps(notes, allow_nan=False))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

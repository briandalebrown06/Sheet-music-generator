"""Real HTTP uploads, interrupted transfer/restart, model coverage and separate limits.

Uses only generated DEVELOPMENT audio and an isolated directory/loopback port.
The references are used for evaluation after inference, never as model output.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_long_fixture import generate, file_hash


def wait_until(function, label, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(.1)
    raise RuntimeError(f"Timed out: {label}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--skip-boundaries", action="store_true")
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="scorecraft-hour-audit-"))
    output.mkdir(parents=True, exist_ok=True)
    data = output / "data"
    data.mkdir(exist_ok=True)
    fixture_dir = args.fixture or output / "fixture"
    reference = json.loads((fixture_dir / "reference.json").read_text()) if (fixture_dir / "reference.json").exists() else generate(fixture_dir)
    source = fixture_dir / reference["path"]
    assert reference["development_fixture"] and reference["duration_seconds"] == 720
    assert source.stat().st_size > 25 * 1024**2 and file_hash(source) == reference["sha256"]
    env = {**os.environ, "DATA_DIR": str(data), "APP_MODE": "local", "AUTH_ENABLED": "false",
           "MAX_UPLOAD_BYTES": "500000000", "MAX_DURATION_SECONDS": "3600",
           "FFMPEG_BINARY": str(ROOT / ".runtime/bin/ffmpeg"), "BASIC_PITCH_RUNTIME": "onnx"}
    log = (output / "services.log").open("w")
    processes = []
    assertions = 0

    def check(value, label):
        nonlocal assertions
        assertions += 1
        assert value, label

    def start_api():
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(args.port), "--no-access-log"], cwd=ROOT, env=env, stdout=log, stderr=log)
        processes.append(process)
        wait_until(lambda: request("GET", "/health", optional=True), "isolated API startup")
        return process

    def start_worker():
        # Short lease is audit-only so a killed supervisor is promptly recovered.
        code = "from dataclasses import replace;from backend.app.config import Settings;from backend.app.worker import Worker;Worker(replace(Settings.from_env(),lease_seconds=2)).run()"
        process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdout=log, stderr=log)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)

    def request(method, path, value=None, body=None, headers=None, optional=False):
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=660)
        try:
            actual_headers = dict(headers or {})
            if value is not None:
                body = json.dumps(value).encode()
                actual_headers["Content-Type"] = "application/json"
            connection.request(method, "/api" + path, body=body, headers=actual_headers)
            response = connection.getresponse()
            payload = response.read()
            content = json.loads(payload) if "json" in response.getheader("Content-Type", "") and payload else payload
            return response.status, content, dict(response.getheaders())
        except (ConnectionRefusedError, OSError):
            if optional:
                return None
            raise
        finally:
            connection.close()

    def create_upload(path, title):
        status, value, _ = request("POST", "/uploads", {"filename": path.name, "size_bytes": path.stat().st_size, "title": title,
            "source": "upload", "input_mode": "solo", "clip_start_seconds": 0, "clip_end_seconds": None,
            "tempo_hint": 120, "time_signature_hint": "4/4"}, headers={"Idempotency-Key": uuid.uuid4().hex})
        check(status == 201, str(value))
        return value

    def chunk(identifier, offset, content):
        return request("PUT", f"/uploads/{identifier}/chunks/{offset}", body=content,
                       headers={"Content-Type": "application/octet-stream", "X-Chunk-SHA256": hashlib.sha256(content).hexdigest()})

    report = {"development_fixture": True, "source": {key: reference[key] for key in ("duration_seconds", "bytes", "sha256", "sample_rate", "channels")},
              "platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "cpu_count": os.cpu_count()}
    try:
        api = start_api()
        limits = request("GET", "/health")[1]["limits"]
        check(limits["max_upload_bytes"] == 500_000_000 and limits["max_duration_seconds"] == 3600, "API advertises exact independent defaults")
        session = create_upload(source, "DEVELOPMENT 12-minute full-coverage performance")
        identifier, size = session["id"], session["chunk_size"]
        with source.open("rb") as audio:
            first, second = audio.read(size), audio.read(size)
        status, state, _ = chunk(identifier, 0, first)
        check(status == 200 and state["offset"] == len(first), "first chunk persisted")
        # Disconnect halfway through the next declared chunk. No partial commit.
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=10)
        connection.putrequest("PUT", f"/api/uploads/{identifier}/chunks/{len(first)}")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(len(second)))
        connection.putheader("X-Chunk-SHA256", hashlib.sha256(second).hexdigest())
        connection.endheaders()
        connection.send(second[:len(second)//2])
        connection.close()
        state = request("GET", f"/uploads/{identifier}")[1]
        check(state["offset"] == len(first), "interrupted chunk does not advance offset")
        stop(api)
        api = start_api()
        state = request("GET", f"/uploads/{identifier}")[1]
        check(state["offset"] == len(first), "acknowledged bytes survive real API restart")
        status, state, _ = chunk(identifier, len(first), second)
        check(status == 200 and state["offset"] == len(first) + len(second), "resume starts at acknowledged offset")
        status, replay, _ = chunk(identifier, len(first), second)
        check(status == 200 and replay["offset"] == state["offset"], "identical replay does not append duplicate bytes")
        with source.open("rb") as audio:
            audio.seek(state["offset"])
            while block := audio.read(size):
                status, state, _ = chunk(identifier, state["offset"], block)
                check(status == 200, str(state))
        status, project, _ = request("POST", f"/uploads/{identifier}/complete")
        check(status == 201, str(project))
        status, repeated, _ = request("POST", f"/uploads/{identifier}/complete")
        check(status == 201 and repeated["id"] == project["id"], "idempotent finalize creates one durable project")
        project_id = project["id"]
        preserved = data / "projects" / project_id / "source"
        check(file_hash(preserved) == reference["sha256"], "original bytes preserved across interrupted transfer")
        report["transfer"] = {"bytes": preserved.stat().st_size, "sha256": file_hash(preserved), "chunk_bytes": size,
                              "interrupted_chunk": True, "api_restart": True, "idempotent_chunk_replay": True, "idempotent_finalize": True}
        print(json.dumps({"transfer": report["transfer"]}), flush=True)
        worker_started = time.monotonic()
        worker = start_worker()
        checkpoints = data / "projects" / project_id / ".analysis-checkpoints"
        def checkpoint_ready():
            return len(list(checkpoints.rglob("*.npz"))) >= 5
        wait_until(checkpoint_ready, "real completed model-window checkpoints", 120)
        checkpoint_count = len(list(checkpoints.rglob("*.npz")))
        worker.send_signal(signal.SIGKILL)
        worker.wait(10)
        worker = start_worker()
        stages = []
        def completed():
            status, detail, _ = request("GET", f"/projects/{project_id}")
            if status in {429, 503}:
                return None
            check(status == 200, str(detail))
            stage = detail["jobs"][0]["stage"]
            if not stages or stages[-1] != stage:
                stages.append(stage)
                print(stage, flush=True)
            return detail if detail["status"] in {"completed", "failed", "canceled"} else None
        project = wait_until(completed, "full 12-minute real transcription after worker restart", 1200)
        wall = time.monotonic() - worker_started
        check(project["status"] == "completed", str(project["jobs"]))
        check(len(project["jobs"]) == 2, "worker death recovered as a bounded new attempt")
        check(abs(project["original_duration_seconds"] - 720) < 1 / 22050, "whole original duration retained")
        check(abs(project["clip_duration_seconds"] - 720) < 1 / 22050, "full selected duration retained")
        status, manifest, _ = request("GET", f"/projects/{project_id}/artifacts/manifest")
        check(status == 200, "real model manifest retained")
        windowing = manifest["windowing"]
        check(windowing["covered_input_frames"] == 720 * 22050, "model covered every decoded PCM frame")
        check(windowing["last_activation_seconds"] >= 719.9, "activation timeline reaches end of long recording")
        check(windowing["checkpoint_windows_reused"] >= 1, "replacement worker reused completed real inference")
        notes = project["notes"]
        tail = [note for note in notes if note["pitch"] == 72 and note["start"] > 717.5 and note["end"] > 719.4]
        check(bool(tail), "real final C5 survives past the old five-minute limit")
        check(max(note["end"] for note in notes) > 719.4, "editable events reach the audible ending")
        waveform = request("GET", f"/projects/{project_id}/waveform")[1]
        check(abs(waveform["duration"] - 720) < 1 / 22050 and len(waveform["peaks"]) <= 2000, "bounded waveform covers entire recording")
        check(max(waveform["peaks"][-4:]) > .02, "waveform includes ending audio")
        status, midi, _ = request("GET", f"/projects/{project_id}/exports/raw-midi")
        check(status == 200 and midi.startswith(b"MThd"), "real raw MIDI export")
        midi_path = output / "DEVELOPMENT-full-12-minute.mid"
        midi_path.write_bytes(midi)
        import pretty_midi
        midi_notes = [note for instrument in pretty_midi.PrettyMIDI(str(midi_path)).instruments for note in instrument.notes]
        check(any(note.pitch == 72 and note.start > 717.5 and note.end > 719.4 for note in midi_notes), "MIDI retains the performed ending")
        references = reference["expected_notes"]
        onset_errors = []
        missing = 0
        used = set()
        for expected in references:
            candidates = [(abs(note["start"] - expected["start"]), index, note) for index, note in enumerate(notes)
                          if index not in used and note["pitch"] == expected["pitch"] and abs(note["start"] - expected["start"]) <= .15]
            if candidates:
                error, index, _ = min(candidates, key=lambda item: item[0])
                used.add(index)
                onset_errors.append(error)
            else:
                missing += 1
        report["analysis"] = {"status": project["status"], "worker_wall_seconds_including_restart": wall,
            "attempts": len(project["jobs"]), "checkpoints_before_kill": checkpoint_count, "windowing": windowing,
            "original_duration_seconds": project["original_duration_seconds"], "selected_duration_seconds": project["clip_duration_seconds"],
            "detected_notes": len(notes), "last_note_end_seconds": max(note["end"] for note in notes), "final_C5_events": tail,
            "midi_last_note_end_seconds": max(note.end for note in midi_notes), "waveform_bins": len(waveform["peaks"]),
            "reference_notes": len(references), "matched_pitch_onsets_within_150ms": len(used), "missing_reference_notes": missing,
            "unmatched_detected_notes": len(notes) - len(used), "mean_absolute_onset_error_seconds": sum(onset_errors) / max(1, len(onset_errors)),
            "resource_usage": manifest.get("measured_resources")}
        stop(worker)
        print(json.dumps({"analysis": report["analysis"]}), flush=True)

        if not args.skip_boundaries:
            from backend.app.config import Settings
            from backend.app.audio import normalize_audio, AnalysisError
            from dataclasses import replace
            settings = replace(Settings.from_env(), data_dir=data, ffmpeg_binary=env["FFMPEG_BINARY"],
                               max_upload_bytes=500_000_000, max_duration_seconds=3600)
            # Duration boundary stays well below the old 25 MiB byte ceiling.
            duration_results = []
            for seconds in (3600, 3600.1):
                path = output / f"DEVELOPMENT-duration-{seconds}.wav"
                rate = 4000
                frames = round(seconds * rate)
                with wave.open(str(path), "wb") as audio:
                    audio.setnchannels(1); audio.setsampwidth(1); audio.setframerate(rate)
                    block = bytes([128]) * rate
                    for _ in range(frames // rate): audio.writeframesraw(block)
                    audio.writeframesraw(block[:frames % rate])
                check(path.stat().st_size < 25 * 1024**2, "duration boundary independent of large file size")
                target = output / f"decoded-{seconds}.wav"
                try:
                    duration = normalize_audio(path, target, settings)
                    check(seconds == 3600 and duration == 3600, "exact one-hour decode accepted")
                    duration_results.append({"seconds": seconds, "bytes": path.stat().st_size, "decoded_seconds": duration, "accepted": True})
                except AnalysisError as error:
                    check(seconds > 3600 and "3600-second limit" in str(error), str(error))
                    duration_results.append({"seconds": seconds, "bytes": path.stat().st_size, "accepted": False, "error": str(error)})
                target.unlink(missing_ok=True)
            # Actual 500,000,000-byte WAV, with a legal JUNK padding chunk and
            # one second of PCM. Padding tests transfer bytes independently.
            boundary = output / "DEVELOPMENT-500MB-padded-boundary.wav"
            with source.open("rb") as audio:
                audio.seek(44)
                pcm = audio.read(44100 * 4)
            base = b"RIFF" + struct.pack("<I", 500_000_000 - 8) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm
            padding = 500_000_000 - len(base) - 8
            with boundary.open("wb") as stream:
                stream.write(base + b"JUNK" + struct.pack("<I", padding))
                stream.seek(500_000_000 - 1); stream.write(b"\0")
            check(boundary.stat().st_size == 500_000_000, "exact decimal byte-boundary fixture")
            session = create_upload(boundary, "DEVELOPMENT byte boundary only")
            with boundary.open("rb") as stream:
                state = session
                while block := stream.read(session["chunk_size"]):
                    status, state, _ = chunk(session["id"], state["offset"], block)
                    check(status == 200, str(state))
            status, too_much, _ = chunk(session["id"], 500_000_000, b"x")
            check(status in {409, 413}, "actual bytes beyond declared ceiling rejected")
            status, large_project, _ = request("POST", f"/uploads/{session['id']}/complete")
            check(status == 201, str(large_project))
            server_source = data / "projects" / large_project["id"] / "source"
            check(server_source.stat().st_size == 500_000_000 and file_hash(server_source) == file_hash(boundary), "all 500,000,000 bytes transferred intact")
            duration = normalize_audio(server_source, output / "boundary-decoded.wav", settings)
            check(duration == 1, "byte boundary independent of decoded duration")
            status, rejected, _ = request("POST", "/uploads", {"filename": "DEVELOPMENT-over-limit.wav", "size_bytes": 500_000_001}, headers={"Idempotency-Key": uuid.uuid4().hex})
            check(status == 413, "500,000,001-byte declaration rejected")
            report["boundaries"] = {"duration": duration_results, "bytes": {"actual_bytes_accepted": 500_000_000,
                "full_sha256_match": True, "decoded_seconds": duration, "excess_actual_byte_rejected": True,
                "declared_bytes_rejected": 500_000_001, "error": rejected}}
        report["assertions"] = assertions
        report["passed"] = True
        report["limitations"] = ["Generated audio is a real decoded waveform, not a commercial recording accuracy benchmark.",
            "60-minute boundaries test actual decoding; the real model integration fixture is 12 minutes.",
            "500 MB boundary fixture uses valid WAV padding to isolate byte size from duration.",
            "Native macOS HTTP/CPU execution; this does not qualify Linux container capacity or public hosting."]
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": True, "assertions": assertions, "report": str(output / 'report.json')}), flush=True)
    finally:
        for process in processes:
            stop(process)
        log.close()


if __name__ == "__main__":
    main()

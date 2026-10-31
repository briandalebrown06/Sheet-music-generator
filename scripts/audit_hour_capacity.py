"""Isolated generated one-hour capacity audit; invokes the real installed model."""
import argparse
import json
import tempfile
import os
import resource
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path)
parser.add_argument('--source', type=Path, help='Generated 720-second development WAV; omit to generate it')
args = parser.parse_args()
WORK = (args.output or Path(tempfile.mkdtemp(prefix='scorecraft-hour-capacity-'))).resolve()
WORK.mkdir(parents=True, exist_ok=True)
os.environ['DATA_DIR'] = str(WORK / 'isolated-data')
sys.path.insert(0, str(ROOT))
from scripts.generate_long_fixture import generate
SOURCE = args.source
if SOURCE is None:
    fixture = generate(WORK / 'fixture')
    SOURCE = WORK / 'fixture' / fixture['path']
INPUT = WORK / 'DEVELOPMENT-3600-second-performance.wav'
report = {'fixture': 'Generated original 720-second harmonic performance, looped five times; not commercial audio',
          'source': str(SOURCE), 'input': str(INPUT), 'scope': 'Actual Basic Pitch adapter and performance MIDI; no browser, API, worker restart, or notation rendering in this capacity check',
          'analysis_timeout_seconds': 600}

def save():
    (WORK / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

def expire(signum, frame):
    report['status'] = 'analysis_timeout'
    save()
    raise TimeoutError('Real one-hour inference exceeded the explicit 600-second audit bound')

signal.signal(signal.SIGALRM, expire)
try:
    started = time.monotonic()
    command = [str(ROOT / '.runtime/bin/ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-stream_loop', '4', '-i', str(SOURCE), '-t', '3600', '-ac', '1', '-ar', '22050',
               '-threads', '1', '-c:a', 'pcm_s16le', str(INPUT)]
    subprocess.run(command, check=True, timeout=120)
    report['fixture_generation_seconds'] = round(time.monotonic() - started, 3)
    report['input_bytes'] = INPUT.stat().st_size
    with wave.open(str(INPUT), 'rb') as audio:
        report.update(input_frames=audio.getnframes(), sample_rate=audio.getframerate(),
                      channels=audio.getnchannels(), input_duration_seconds=audio.getnframes()/audio.getframerate())
        assert audio.getnframes() == 3600 * 22050
    save()
    from backend.app.engine import BasicPitchEngine
    import pretty_midi
    progress = {'stage': None}
    def stage(label):
        progress['stage'] = label
        if 'window ' not in label or any(f'window {i} of' in label for i in (1, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2196)):
            print(label, flush=True)
        (WORK / 'progress.json').write_text(json.dumps({'stage': label, 'elapsed_seconds': round(time.monotonic()-started, 3)}))
    started = time.monotonic()
    signal.alarm(600)
    result = BasicPitchEngine().analyze(INPUT, WORK / 'analysis', 'EXPLICIT-ONE-HOUR-CAPACITY-AUDIT', stage_callback=stage)
    signal.alarm(0)
    report['analysis_wall_seconds'] = round(time.monotonic()-started, 3)
    report['manifest'] = result['manifest']
    notes = result['notes']
    report['normalized_note_count'] = len(notes)
    report['last_normalized_notes'] = sorted(notes, key=lambda n: n['end'])[-5:]
    raw = json.loads((WORK / 'analysis/raw-note-events.json').read_text())['note_events']
    report['raw_note_count'] = len(raw)
    report['last_raw_note_events'] = sorted(raw, key=lambda n: n[1])[-5:]
    midi = pretty_midi.PrettyMIDI(str(WORK / 'analysis/raw-performance.mid'))
    midi_notes = [note for instrument in midi.instruments for note in instrument.notes]
    report['raw_midi_note_count'] = len(midi_notes)
    report['raw_midi_last_note_seconds'] = max(n.end for n in midi_notes)
    final_c5 = [n for n in notes if n['pitch'] == 72 and n['start'] > 3598 and n['end'] > 3599.5]
    report['final_c5_notes'] = final_c5
    timeline = result['manifest']['windowing']
    intervals = timeline['coverage_intervals_seconds']
    report['coverage_intervals_contiguous'] = intervals[0][0] == 0 and intervals[-1][1] == 3600 and all(a[1] == b[0] for a,b in zip(intervals, intervals[1:]))
    assert timeline['covered_input_frames'] == report['input_frames']
    assert 3599.98 < timeline['last_activation_seconds'] < 3600
    assert report['coverage_intervals_contiguous']
    assert final_c5, 'Final expected C5 missing or ends too early'
    assert report['raw_midi_last_note_seconds'] > 3599.5
    assert len(midi_notes) == len(raw)
    report['status'] = 'passed'
except BaseException as exc:
    report.setdefault('status', 'failed')
    report['error'] = repr(exc)
    raise
finally:
    signal.alarm(0)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    report['process_peak_rss_bytes'] = int(usage.ru_maxrss if sys.platform == 'darwin' else usage.ru_maxrss*1024)
    report['process_user_cpu_seconds'] = round(usage.ru_utime, 3)
    report['process_system_cpu_seconds'] = round(usage.ru_stime, 3)
    report['resources_scope'] = 'Entire audit Python process, including model/MIDI parsing; excludes FFmpeg child'
    save()
    print(json.dumps({k:v for k,v in report.items() if k not in ('manifest', 'last_normalized_notes', 'last_raw_note_events')}, indent=2), flush=True)

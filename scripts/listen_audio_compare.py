"""Private, bounded audio-language-model review of local WAV clips.

This consumes sound through MOSS-Music's audio encoder. Its answers are model
observations, not ground truth, a calibrated accuracy score, or pianist approval.
It never reads MIDI/notes and never changes a project. Install/run it in the
separate .venv-listening environment, not the transcription environment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import wave

MODEL_ID = 'mlx-community/MOSS-Music-8B-Thinking-4bit'
MODEL_REVISION = 'b14123a419b6254b92d9e55b8a5b6f7285e05c70'
LIMITATION = ('Audio-language-model observations, not ground truth or pianist approval. '
              'Blind audio controls and corroboration are required before musical edits.')
OFFLINE_ENV = {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'HF_DATASETS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
               'DO_NOT_TRACK': '1', 'TOKENIZERS_PARALLELISM': 'false'}
AUDIO_SPAN = '<|audio_bos|><|AUDIO|><|audio_eos|>'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def audio_info(path, max_seconds):
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError('Audio must be an existing absolute local WAV path.')
    if path.stat().st_size > 64*1024*1024:
        raise ValueError('A review clip exceeds the 64 MiB input bound.')
    with wave.open(str(path), 'rb') as wav:
        frames, rate = wav.getnframes(), wav.getframerate()
        duration = frames / rate
        if wav.getcomptype() != 'NONE' or wav.getsampwidth() not in (1, 2, 3, 4):
            raise ValueError('Use decoded PCM WAV for local review.')
        if not 0 < duration <= max_seconds or wav.getnchannels() not in (1, 2):
            raise ValueError('Review clips must be nonempty mono/stereo PCM within the duration bound.')
        # Reject a truncated container whose declared duration exceeds its PCM.
        actual = len(wav.readframes(frames))
        if actual != frames * wav.getnchannels() * wav.getsampwidth():
            raise ValueError('Truncated PCM data in review clip.')
        result = {'path': str(path.resolve()), 'duration_seconds': duration,
                  'sample_rate': rate, 'channels': wav.getnchannels(), 'frames': frames}
    return {**result, 'sha256': sha256(path), 'bytes': path.stat().st_size}


def build_prompt(prompt, count, *, no_thinking=False):
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 6000:
        raise ValueError('Provide a nonempty question of at most 6,000 characters.')
    if '<|' in prompt or not 1 <= count <= 3:
        raise ValueError('Questions cannot contain model control tokens; use one to three clips.')
    clips = '\n'.join(f'Audio {chr(65+i)}:\n{AUDIO_SPAN}' for i in range(count))
    result = ('<|im_start|>system\nYou are an audio comparison assistant. '
            'Describe only audible evidence. State uncertainty. Give a concise answer '
            'without a reasoning trace.<|im_end|>\n<|im_start|>user\n'
            + clips + '\n' + prompt + '<|im_end|>\n<|im_start|>assistant\n')
    # Exact suffix emitted by this pinned checkpoint's chat_template.jinja for
    # enable_thinking=False. The MLX generate API does not implement that kwarg.
    return result + ('<think>\n\n</think>\n\n' if no_thinking else '')


def validate_cases(manifest, max_seconds):
    cases = manifest.get('cases') if isinstance(manifest, dict) else None
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError('A manifest must contain one to 64 cases.')
    result, seen = [], set()
    for case in cases:
        identifier = case.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier) or identifier in seen:
            raise ValueError('Case IDs must be unique short letters, numbers, underscores or hyphens.')
        seen.add(identifier)
        audio = case.get('audio')
        if not isinstance(audio, list) or not 1 <= len(audio) <= 3:
            raise ValueError('Each case must contain one to three local WAV clips.')
        inputs = [audio_info(path, max_seconds) for path in audio]
        if sum(x['duration_seconds'] for x in inputs) > 60:
            raise ValueError('Total audio in one case cannot exceed 60 seconds.')
        build_prompt(case.get('prompt'), len(inputs))
        result.append({'id': identifier, 'inputs': inputs, 'prompt': case['prompt']})
    return result


def final_text(text):
    # The runtime strips complete thinking blocks. Fail closed if token exhaustion
    # leaves an incomplete one. This does not detect untagged reasoning prose.
    text = re.sub(r'<think>.*?</think>', '', str(text), flags=re.S | re.I).strip()
    if re.search(r'</?think\b', text, re.I):
        return '', 'The model did not finish a final answer within the generation bound.'
    if not text:
        return '', 'No final answer was emitted within the generation bound.'
    return text, None


def sampling_settings(args):
    temperature = getattr(args, 'temperature', 0.0)
    top_p = getattr(args, 'top_p', 1.0)
    top_k = getattr(args, 'top_k', 0)
    seed = getattr(args, 'seed', None)
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError('Temperature must be finite and between 0 and 2.')
    if not math.isfinite(top_p) or not 0 < top_p <= 1:
        raise ValueError('Top-p must be finite, greater than zero and at most 1.')
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 0 <= top_k <= 1000:
        raise ValueError('Top-k must be an integer between 0 and 1000.')
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**32):
        raise ValueError('Seed must be an unsigned 32-bit integer.')
    return {'temperature': temperature, 'top_p': top_p, 'top_k': top_k, 'seed': seed}


def classify_response(text, generation_tokens, max_tokens):
    text, warning = final_text(text)
    limit_reached = generation_tokens >= max_tokens
    if limit_reached:
        warning = 'Generation limit reached; any final text is partial and the review is incomplete.'
    return text, ('completed' if text and not limit_reached else 'incomplete'), warning


def write_json(path, value):
    target = Path(path)
    temp = target.with_suffix(target.suffix+'.tmp')
    temp.write_text(json.dumps(value, indent=2))
    os.chmod(temp, 0o600)
    temp.replace(target)


def worker(request_path, result_path):
    os.environ.update(OFFLINE_ENV)
    request = json.loads(Path(request_path).read_text())
    case = request['case']
    started = time.monotonic()
    import mlx.core as mx
    from mlx_audio.stt import load
    mx.set_memory_limit(request['memory_limit_bytes'])
    mx.set_cache_limit(256*1024*1024)
    if request.get('seed') is not None:
        mx.random.seed(request['seed'])
    model = load(request['model_path'])
    load_seconds = time.monotonic() - started
    # Recheck every file immediately before inference. A changed audio file must
    # not be attributed to the parent's earlier hash or duration.
    for item in case['inputs']:
        if sha256(item['path']) != item['sha256']:
            raise ValueError('Audio changed after the review manifest was validated.')
    prompt = build_prompt(case['prompt'], len(case['inputs']), no_thinking=request.get('no_thinking', False))
    response = model.generate([item['path'] for item in case['inputs']], prompt=prompt,
        max_tokens=request['max_tokens'], temperature=request.get('temperature', 0.0),
        top_p=request.get('top_p', 1.0), top_k=request.get('top_k', 0), strip_thinking=True,
        enable_time_marker=True, prefill_step_size=512)
    text, status, warning = classify_response(response.text, response.generation_tokens, request['max_tokens'])
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {**case, 'status': status, 'response': text,
        'warning': warning, 'generation_limit_reached': response.generation_tokens >= request['max_tokens'],
        'prompt_tokens': response.prompt_tokens, 'generation_tokens': response.generation_tokens,
        'elapsed_seconds': time.monotonic()-started, 'model_load_seconds': load_seconds,
        'peak_rss_bytes': int(rss if sys.platform == 'darwin' else rss*1024),
        'peak_mlx_allocated_bytes': int(mx.get_peak_memory()),
        'runtime_versions': {name: importlib.metadata.version(name) for name in ('mlx-audio', 'mlx', 'transformers')},
        'limitation': LIMITATION}
    write_json(result_path, result)


def terminate_and_reap(process):
    """Stop this isolated case and any descendants before releasing its files."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_case(case, model_path, args):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='audio-listening-') as directory:
        request_path, result_path = Path(directory)/'request.json', Path(directory)/'result.json'
        write_json(request_path, {'case': case, 'model_path': str(model_path),
                   'memory_limit_bytes': int(args.memory_gib*1024**3), 'max_tokens': args.max_tokens,
                   'no_thinking': args.no_thinking, **sampling_settings(args)})
        # Discard library stdout and retain only the runtime's response text with
        # tagged thinking removed. Untagged reasoning-like prose is not reliably
        # distinguishable from an answer and needs review before presentation.
        with open(os.devnull, 'wb') as null:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--worker', str(request_path), str(result_path)], stdout=null, stderr=null,
                env={**os.environ, **OFFLINE_ENV}, start_new_session=True)
            try:
                code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                terminate_and_reap(process)
                return {**case, 'status': 'timed_out', 'response': '',
                    'elapsed_seconds': time.monotonic()-started, 'limitation': LIMITATION}
            except (KeyboardInterrupt, SystemExit) as exc:
                terminate_and_reap(process)
                exit_code = 130 if isinstance(exc, KeyboardInterrupt) else (exc.code if isinstance(exc.code, int) else 143)
                return {**case, 'status': 'canceled', 'response': '',
                    'cancellation_exit_code': exit_code,
                    'elapsed_seconds': time.monotonic()-started, 'limitation': LIMITATION}
        if code != 0 or not result_path.is_file():
            diagnostic = json.loads(result_path.read_text()) if result_path.is_file() else {}
            return {**case, 'status': 'failed', 'response': '', 'exit_code': code,
                    'error_type': diagnostic.get('error_type'), 'error': diagnostic.get('error'),
                    'elapsed_seconds': time.monotonic()-started, 'limitation': LIMITATION}
        return json.loads(result_path.read_text())


def terminate_parent(_signum, _frame):
    # Raising makes the active case's cancellation path kill and reap its whole
    # process group before the parent exits. The worker itself keeps SIGTERM's
    # default behavior; only the orchestration process installs this handler.
    raise SystemExit(143)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-tokens', type=int, default=1536)
    parser.add_argument('--max-clip-seconds', type=float, default=30)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--memory-gib', type=float, default=16)
    parser.add_argument('--temperature', type=float, default=0.0)
    parser.add_argument('--top-p', type=float, default=1.0)
    parser.add_argument('--top-k', type=int, default=0)
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--no-thinking', action='store_true',
                        help='Use the pinned checkpoint\'s non-thinking prompt suffix; musical quality must be evaluated separately.')
    parser.add_argument('--worker', nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            worker(*args.worker)
        except Exception as exc:
            # A bounded error diagnostic is useful for real installation/runtime
            # failures; no library stdout or partial generation is saved.
            write_json(args.worker[1], {'status': 'failed', 'response': '',
                'error_type': type(exc).__name__, 'error': str(exc)[:1200],
                'limitation': LIMITATION})
            raise SystemExit(1)
        return
    signal.signal(signal.SIGTERM, terminate_parent)
    sampling = sampling_settings(args)
    if any(x is None for x in (args.model, args.manifest, args.output)):
        parser.error('--model, --manifest and --output are required')
    if not 64 <= args.max_tokens <= 4096 or not 0 < args.max_clip_seconds <= 30:
        raise ValueError('Use 64–4096 generation tokens and a maximum clip duration up to 30 seconds.')
    if not 1 <= args.timeout <= 1800 or not 4 <= args.memory_gib <= 20:
        raise ValueError('Use a 1–1800 second timeout and 4–20 GiB MLX allocation bound.')
    model = args.model.resolve(strict=True)
    config = json.loads((model/'config.json').read_text())
    tokenizer = json.loads((model/'tokenizer_config.json').read_text())
    if config.get('model_type') != 'moss_music' or tokenizer.get('auto_map'):
        raise ValueError('Use the reviewed local MOSS-Music model with the built-in tokenizer.')
    cases = validate_cases(json.loads(args.manifest.read_text()), args.max_clip_seconds)
    files = sorted(p for p in model.iterdir() if p.is_file() and p.suffix in ('.json', '.safetensors', '.jinja', '.txt'))
    if not any(p.suffix == '.safetensors' for p in files):
        raise ValueError('No local safetensors weights found.')
    result = {'schema_version': 1, 'model_id': MODEL_ID, 'requested_model_revision': MODEL_REVISION,
        'model_path': str(model), 'model_files': {p.name: {'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in files},
        'settings': {**sampling, 'max_tokens': args.max_tokens, 'timeout_per_case_seconds': args.timeout,
                     'mlx_memory_limit_gib': args.memory_gib, 'per_case_fresh_process': True, 'offline': True,
                     'no_thinking': args.no_thinking},
        'limitation': LIMITATION, 'cases': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(cases):
        entry = run_case(case, model, args)
        result['cases'].append(entry)
        if entry['status'] == 'canceled':
            result['status'] = 'canceled'
            result['cases'].extend({**item, 'status': 'not_run', 'response': '',
                'reason': 'The review run was canceled before this case started.',
                'limitation': LIMITATION} for item in cases[index+1:])
        write_json(args.output, result)
        print(case['id'], entry['status'], round(entry['elapsed_seconds'], 2), 'seconds', flush=True)
        if entry['status'] == 'canceled':
            raise SystemExit(entry['cancellation_exit_code'])
    if any(case['status'] != 'completed' for case in result['cases']):
        raise SystemExit(2)


if __name__ == '__main__':
    main()

"""Runner contract tests use generated PCM, never a fabricated musical review."""
import wave
from copy import deepcopy
import json
import signal
import subprocess
from types import SimpleNamespace

import pytest

from scripts.listen_audio_compare import AUDIO_SPAN, audio_info, build_prompt, classify_response, final_text, validate_cases
from scripts import listen_audio_compare as runner


def pcm(path, seconds=1):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\0\0' * int(seconds*16000))
    return str(path.resolve())


def test_multiple_audio_prompt_has_one_span_per_ordered_clip():
    prompt = build_prompt('Compare rhythm only; say if uncertain.', 2)
    assert prompt.count(AUDIO_SPAN) == 2
    assert prompt.index('Audio A:') < prompt.index('Audio B:') < prompt.index('Compare rhythm')
    assert prompt.startswith('<|im_start|>system\n')
    assert prompt.endswith('<|im_start|>assistant\n')


def test_wav_metadata_and_hash_reflect_complete_pcm(tmp_path):
    path = pcm(tmp_path/'control.wav', 2)
    actual = audio_info(path, 30)
    assert actual['duration_seconds'] == 2
    assert actual['frames'] == 32000 and len(actual['sha256']) == 64
    with open(path, 'r+b') as file:
        file.truncate(100)
    with pytest.raises(ValueError, match='Truncated'):
        audio_info(path, 30)


def test_validation_preserves_case_order_and_enforces_limits(tmp_path):
    a = pcm(tmp_path/'a.wav')
    b = pcm(tmp_path/'b.wav', 2)
    source = {'cases': [{'id': 'blind-1', 'audio': [a, b], 'prompt': 'What differs?'}]}
    before = deepcopy(source)
    result = validate_cases(source, 30)
    assert source == before
    assert [x['path'] for x in result[0]['inputs']] == [a, b]
    with pytest.raises(ValueError, match='duration'):
        validate_cases(source, 1)
    with pytest.raises(ValueError, match='unique'):
        validate_cases({'cases': source['cases']*2}, 30)
    source['cases'][0]['audio'] = ['https://example.com/private.wav']
    with pytest.raises(ValueError, match='local'):
        validate_cases(source, 30)


def test_control_tokens_cannot_change_clip_identity():
    with pytest.raises(ValueError, match='control tokens'):
        build_prompt('Extra audio '+AUDIO_SPAN, 2)
    with pytest.raises(ValueError):
        build_prompt('Compare', 4)


def test_tagged_reasoning_is_removed_and_incomplete_tags_are_rejected():
    assert final_text('<think>private reasoning</think> Clip A repeats faster.')[0] == 'Clip A repeats faster.'
    assert final_text('<think>unfinished reasoning')[0] == ''
    assert final_text('leftover reasoning</think>')[0] == ''
    assert final_text('')[0] == ''


def test_optional_no_thinking_matches_pinned_checkpoint_template_suffix():
    normal = build_prompt('Compare the two clips.', 2)
    switched = build_prompt('Compare the two clips.', 2, no_thinking=True)
    assert switched == normal + '<think>\n\n</think>\n\n'
    assert switched.count(AUDIO_SPAN) == 2


def test_partial_final_answer_at_token_limit_is_not_completed():
    text, status, warning = classify_response('Clip A sounds', 1024, 1024)
    assert text == 'Clip A sounds' and status == 'incomplete'
    assert 'partial' in warning
    assert classify_response('The clips sound identical.', 20, 1024) == ('The clips sound identical.', 'completed', None)
    assert classify_response('<think>unfinished', 1024, 1024)[0] == ''


@pytest.mark.parametrize('interruption,expected', [
    (KeyboardInterrupt(), 'canceled'),
    (SystemExit(143), 'canceled'),
    (subprocess.TimeoutExpired('fixture-worker', 1), 'timed_out'),
])
def test_interruption_kills_process_group_and_reaps_worker(monkeypatch, tmp_path, interruption, expected):
    calls, killed = [], []
    class Process:
        pid = 4321
        def wait(self, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise interruption
            return -signal.SIGKILL
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **kw: Process())
    monkeypatch.setattr(runner.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))
    args = SimpleNamespace(memory_gib=12, max_tokens=64, no_thinking=False, timeout=1)
    actual = runner.run_case({'id': 'fixture', 'inputs': [], 'prompt': 'Fixture.'}, tmp_path, args)
    assert actual['status'] == expected
    assert killed == [(4321, signal.SIGKILL)]
    assert calls == [1, None]  # the terminated child was reaped
    assert actual['response'] == ''


def test_canceled_run_preserves_finished_results_and_marks_remaining_not_run(monkeypatch, tmp_path):
    model = tmp_path/'generated-model-fixture'
    model.mkdir()
    (model/'config.json').write_text('{"model_type":"moss_music"}')
    (model/'tokenizer_config.json').write_text('{}')
    (model/'model.safetensors').write_bytes(b'GENERATED TEST FIXTURE; NEVER LOADED')
    audio = pcm(tmp_path/'control.wav')
    manifest = tmp_path/'cases.json'
    manifest.write_text(json.dumps({'cases': [{'id': str(i), 'audio': [audio], 'prompt': 'Fixture.'} for i in range(3)]}))
    output = tmp_path/'result.json'
    called = []
    def fake_run(case, _model, _args):
        called.append(case['id'])
        assert runner.sampling_settings(_args) == {'temperature': .6, 'top_p': .95, 'top_k': 20, 'seed': 0}
        return {**case, 'status': 'completed' if case['id'] == '0' else 'canceled',
                'response': 'generated fixture response' if case['id'] == '0' else '',
                'elapsed_seconds': .1, 'cancellation_exit_code': 130}
    monkeypatch.setattr(runner, 'run_case', fake_run)
    monkeypatch.setattr(runner.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(runner.sys, 'argv', ['runner', '--model', str(model), '--manifest', str(manifest), '--output', str(output),
                                         '--temperature', '.6', '--top-p', '.95', '--top-k', '20', '--seed', '0'])
    with pytest.raises(SystemExit) as stopped:
        runner.main()
    assert stopped.value.code == 130
    result = json.loads(output.read_text())
    assert result['status'] == 'canceled'
    assert {key: result['settings'][key] for key in ('temperature', 'top_p', 'top_k', 'seed')} == {
        'temperature': .6, 'top_p': .95, 'top_k': 20, 'seed': 0}
    assert [item['status'] for item in result['cases']] == ['completed', 'canceled', 'not_run']
    assert result['cases'][0]['response'] == 'generated fixture response'
    assert called == ['0', '1']


def test_sampling_options_preserve_defaults_and_accept_seeded_control():
    assert runner.sampling_settings(SimpleNamespace()) == {'temperature': 0.0, 'top_p': 1.0, 'top_k': 0, 'seed': None}
    assert runner.sampling_settings(SimpleNamespace(temperature=.6, top_p=.95, top_k=20, seed=0)) == {
        'temperature': .6, 'top_p': .95, 'top_k': 20, 'seed': 0}


@pytest.mark.parametrize('override', [
    {'temperature': float('nan')}, {'temperature': float('inf')}, {'temperature': -1},
    {'top_p': 0}, {'top_p': 1.01}, {'top_k': -1}, {'top_k': 1.5},
    {'seed': -1}, {'seed': 2**32},
])
def test_sampling_parameters_are_bounded(override):
    with pytest.raises(ValueError):
        runner.sampling_settings(SimpleNamespace(**override))


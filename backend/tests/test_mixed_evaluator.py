"""A result file alone must never hide a native evaluation-process failure."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.evaluate_mixed import run_isolated_adapter


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(-11, ["explicit-test-child"]),
    subprocess.TimeoutExpired(["explicit-test-child"], 12),
])
def test_child_failure_is_propagated_despite_existing_result(tmp_path, monkeypatch, failure):
    saved = tmp_path / "existing-result.json"
    saved.write_text(json.dumps({"explicit_test_fixture": "already written but not accepted"}))

    def failed_child(command, *, check, timeout):
        assert check is True and timeout == 12
        raise failure

    monkeypatch.setattr("scripts.evaluate_mixed.subprocess.run", failed_child)
    settings = SimpleNamespace(separation_timeout_seconds=12, job_timeout_seconds=34)
    with pytest.raises(type(failure)) as caught:
        run_isolated_adapter("separate", tmp_path / "input.wav", tmp_path / "output", saved, settings)
    assert caught.value is failure


def test_result_is_loaded_only_after_checked_child_exit(tmp_path, monkeypatch):
    saved = tmp_path / "result.json"
    payload = {"explicit_test_fixture": "process handshake only"}

    def successful_child(command, *, check, timeout):
        assert check is True and timeout == 34
        assert command[1:3] == ["-X", "faulthandler"]
        assert command[-4:] == ["--category", "other", "--source-name", "separated"]
        assert not saved.exists()
        saved.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("scripts.evaluate_mixed.subprocess.run", successful_child)
    settings = SimpleNamespace(separation_timeout_seconds=12, job_timeout_seconds=34)
    result = run_isolated_adapter("transcribe", tmp_path / "input.wav", tmp_path / "output", saved,
                                  settings, category="other", source_name="separated")
    assert result == payload

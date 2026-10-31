"""Real isolated-process limit checks using a clearly labeled slow test adapter."""
import os
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.app.pipeline import process_job
from backend.app.worker import Worker
from backend.tests.test_mixed_workflow import FixtureSeparator, client, settings, upload


class SleepingTestSeparator:
    def separate(self, input_path, output_dir, stage_callback=None):
        stage_callback("Explicit slow test separator; no model output")
        (output_dir / "test-partial-output").write_bytes(b"TEST FIXTURE ONLY")
        time.sleep(30)
        raise AssertionError("The configured resource guard should stop this test child.")


def isolated_slow_test_job(settings, job, worker_id):
    process_job(settings, job, worker_id, isolated=True, separator=SleepingTestSeparator())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process and RSS limit integration")
@pytest.mark.parametrize("limit,expected", [("timeout", "SEPARATION_TIMEOUT_SECONDS"), ("memory", "SEPARATION_MEMORY_MB")])
def test_mixed_limits_stop_isolated_child_and_clean_partial_output(client, settings, monkeypatch, limit, expected):
    import backend.app.worker as module
    project = upload(client)
    original = (settings.data_dir / "projects" / project["id"] / "source").read_bytes()
    # A deliberately tiny RSS threshold tests the actual monitor without
    # allocating excessive test memory. Public environment configuration retains
    # its validated 512 MB minimum. Timeout uses an actual sleeping subprocess.
    limits = replace(settings, separation_timeout_seconds=1 if limit == "timeout" else 10,
                     separation_memory_mb=1 if limit == "memory" else 4096)
    monkeypatch.setattr(module, "process_job", isolated_slow_test_job)
    assert Worker(limits).run_once()
    result = client.get(f"/api/projects/{project['id']}").json()
    assert result["status"] == "failed" and expected in result["jobs"][0]["error"]
    assert result["notes"] is None and not result["separation_available"]
    directory = settings.data_dir / "projects" / project["id"]
    assert (directory / "source").read_bytes() == original
    assert not (directory / result["jobs"][0]["id"]).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX peak RSS check")
def test_fast_separator_exceeding_memory_cannot_publish_between_guard_polls(client, settings, monkeypatch):
    import resource
    project = upload(client)
    directory = settings.data_dir / "projects" / project["id"]
    original = (directory / "source").read_bytes()
    # The injected adapter returns complete stems immediately. No monitor thread
    # runs in this test: the synchronous publication check must catch the peak.
    peak = [1]
    over_limit_bytes = (settings.separation_memory_mb + 1) * 1024 * 1024
    def exceed_limit():
        peak[0] = over_limit_bytes if sys.platform == "darwin" else over_limit_bytes // 1024
    monkeypatch.setattr(resource, "getrusage", lambda _: SimpleNamespace(ru_maxrss=peak[0]))
    separator = FixtureSeparator(on_separate=exceed_limit)
    assert Worker(settings, separator=separator).run_once()
    result = client.get(f"/api/projects/{project['id']}").json()
    assert separator.calls == 1
    assert result["status"] == "failed" and "SEPARATION_MEMORY_MB" in result["jobs"][0]["error"]
    assert result["notes"] is None and not result["separation_available"]
    assert (directory / "source").read_bytes() == original
    assert not (directory / result["jobs"][0]["id"]).exists()

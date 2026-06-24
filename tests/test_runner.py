import subprocess
from pathlib import Path

import pytest

from s3backup import runner, state, manifest
from s3backup.config import Config, Job, Settings


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("S3BACKUP_CONFIG", str(tmp_path / "config.toml"))
    # Find a fake aws binary path without requiring aws installed.
    monkeypatch.setattr(runner.aws, "find_aws", lambda: "/usr/local/bin/aws")
    # Don't touch the real bucket lifecycle / notifications in tests.
    monkeypatch.setattr(runner.aws, "set_abort_incomplete_uploads_lifecycle",
                        lambda *a, **k: None)
    monkeypatch.setattr(runner.notify, "notify", lambda *a, **k: True)


def make_job_and_config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.txt").write_text("hello")
    job = Job(name="j", local_path=str(data), bucket="b", prefix="p",
              storage_class="STANDARD_IA")
    config = Config(settings=Settings(), jobs={"j": job})
    return job, config, data


def test_skips_when_no_changes(tmp_path, monkeypatch):
    job, config, data = make_job_and_config(tmp_path)
    # Save a manifest matching the current tree so nothing looks changed.
    m = manifest.build_manifest(Path(job.local_path), job.effective_excludes())
    manifest.save_manifest("j", m)

    # If sync runs, fail the test.
    def boom(*a, **k):
        raise AssertionError("sync should not run when nothing changed")

    monkeypatch.setattr(runner, "_stream_sync", boom)

    code = runner.run_backup(config, job, keep_awake=False)
    assert code == 0
    st = state.read_state("j")
    assert st.phase == state.PHASE_NO_CHANGES


def test_runs_sync_when_changed_and_succeeds(tmp_path, monkeypatch):
    job, config, data = make_job_and_config(tmp_path)
    # No prior manifest => everything is "new" => should sync.

    calls = {"n": 0}

    def fake_stream(argv, env, st, publish):
        calls["n"] += 1
        st.done_files = st.total_files
        publish()
        return 0, ""

    monkeypatch.setattr(runner, "_stream_sync", fake_stream)

    code = runner.run_backup(config, job, keep_awake=False)
    assert code == 0
    assert calls["n"] == 1
    st = state.read_state("j")
    assert st.phase == state.PHASE_DONE
    # Manifest should now be saved for next time.
    assert manifest.load_manifest("j")  # non-empty


def test_retries_then_fails(tmp_path, monkeypatch):
    job, config, data = make_job_and_config(tmp_path)
    monkeypatch.setattr(runner, "MAX_ATTEMPTS", 3)
    monkeypatch.setattr(runner, "BACKOFF_BASE_SECONDS", 0)  # no real sleeping
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)

    attempts = {"n": 0}

    def always_fail(argv, env, st, publish):
        attempts["n"] += 1
        return 1, "boom"

    monkeypatch.setattr(runner, "_stream_sync", always_fail)

    code = runner.run_backup(config, job, keep_awake=False)
    assert code == 1
    assert attempts["n"] == 3
    st = state.read_state("j")
    assert st.phase == state.PHASE_FAILED
    # Manifest must NOT be saved on failure (so next run retries).
    assert manifest.load_manifest("j") == {}


def test_force_runs_even_when_no_changes(tmp_path, monkeypatch):
    job, config, data = make_job_and_config(tmp_path)
    m = manifest.build_manifest(Path(job.local_path), job.effective_excludes())
    manifest.save_manifest("j", m)

    ran = {"n": 0}

    def fake_stream(argv, env, st, publish):
        ran["n"] += 1
        return 0, ""

    monkeypatch.setattr(runner, "_stream_sync", fake_stream)

    code = runner.run_backup(config, job, force=True, keep_awake=False)
    assert code == 0
    assert ran["n"] == 1  # forced past change detection

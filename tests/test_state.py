import os

import pytest

from s3backup import state


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("S3BACKUP_CONFIG", str(tmp_path / "config.toml"))


def test_write_then_read_roundtrip(tmp_path):
    st = state.RunState(job="j", phase=state.PHASE_UPLOADING,
                        total_files=100, total_bytes=1000,
                        done_files=25, done_bytes=250)
    state.write_state(st)
    loaded = state.read_state("j")
    assert loaded is not None
    assert loaded.phase == state.PHASE_UPLOADING
    assert loaded.done_files == 25


def test_read_missing_returns_none(tmp_path):
    assert state.read_state("nope") is None


def test_percent_and_eta():
    st = state.RunState(job="j", phase=state.PHASE_UPLOADING,
                        total_bytes=1000, done_bytes=250)
    assert st.percent == 25.0
    # No elapsed time set yet -> ETA unknown
    assert st.eta_seconds is None


def test_eta_computes_from_throughput():
    import time
    st = state.RunState(job="j", phase=state.PHASE_UPLOADING,
                        started_at=time.time() - 10,
                        total_bytes=1000, done_bytes=500)
    eta = st.eta_seconds
    # 500 bytes in 10s = 50 B/s; 500 remaining => ~10s
    assert eta is not None
    assert 8 < eta < 12


def test_eta_none_when_not_uploading():
    st = state.RunState(job="j", phase=state.PHASE_DONE,
                        total_bytes=1000, done_bytes=1000)
    assert st.eta_seconds is None


def test_is_active():
    assert state.RunState(job="j", phase=state.PHASE_UPLOADING).is_active
    assert state.RunState(job="j", phase=state.PHASE_RETRYING).is_active
    assert not state.RunState(job="j", phase=state.PHASE_DONE).is_active
    assert not state.RunState(job="j", phase=state.PHASE_NO_CHANGES).is_active


def test_pid_alive():
    assert state.pid_alive(os.getpid()) is True
    assert state.pid_alive(None) is False
    # An almost-certainly-dead PID
    assert state.pid_alive(2_000_000_000) is False


def test_read_tolerates_unknown_fields(tmp_path):
    # Simulate a state file from a newer version with an extra key.
    import json
    path = state.state_file("j")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job": "j", "phase": "done", "future_field": 1}))
    loaded = state.read_state("j")
    assert loaded is not None
    assert loaded.phase == "done"

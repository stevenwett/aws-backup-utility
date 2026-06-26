import pytest

from s3backup import daemon


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("S3BACKUP_CONFIG", str(tmp_path / "config.toml"))
    # Keep LaunchAgents writes inside the tmp HOME.
    monkeypatch.setenv("HOME", str(tmp_path))


def test_continuous_plist_uses_keepalive():
    doc = daemon.build_plist("job1", scheduled=False)
    assert doc["Label"] == "com.s3backup.job1"
    assert doc["KeepAlive"] == {"SuccessfulExit": False}
    assert doc["RunAtLoad"] is True
    assert "StartCalendarInterval" not in doc
    # Internal _run command, not a user-facing one.
    assert "_run" in doc["ProgramArguments"]
    assert "job1" in doc["ProgramArguments"]


def test_scheduled_plist_uses_calendar_interval():
    doc = daemon.build_plist("job1", scheduled=True, hour=4, minute=30)
    assert doc["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert doc["RunAtLoad"] is False
    assert "KeepAlive" not in doc
    assert "--scheduled" in doc["ProgramArguments"]


def test_plist_sets_home_and_path_env():
    doc = daemon.build_plist("job1", scheduled=False)
    env = doc["EnvironmentVariables"]
    assert "HOME" in env
    assert "/usr/local/bin" in env["PATH"]


def test_write_plist_is_valid_plist(tmp_path):
    import plistlib

    path = daemon.write_plist("job1", scheduled=True)
    assert path.exists()
    with open(path, "rb") as fh:
        loaded = plistlib.load(fh)
    assert loaded["Label"] == "com.s3backup.job1"


def test_schedule_info_none_when_no_plist():
    assert daemon.schedule_info("never-created") is None


def test_schedule_info_scheduled(monkeypatch):
    daemon.write_plist("job1", scheduled=True, hour=4, minute=30)
    monkeypatch.setattr(daemon, "is_loaded", lambda j: True)
    info = daemon.schedule_info("job1")
    assert info["kind"] == "scheduled"
    assert info["hour"] == 4 and info["minute"] == 30
    assert info["loaded"] is True
    assert info["next_run"] > 0


def test_schedule_info_continuous(monkeypatch):
    daemon.write_plist("job1", scheduled=False)
    monkeypatch.setattr(daemon, "is_loaded", lambda j: False)
    info = daemon.schedule_info("job1")
    assert info["kind"] == "continuous"
    assert info["loaded"] is False


def test_next_daily_run_is_in_future():
    import time
    nxt = daemon._next_daily_run(3, 0)
    assert nxt > time.time()
    # Within the next 24h.
    assert nxt - time.time() <= 24 * 3600 + 1

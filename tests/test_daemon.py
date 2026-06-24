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

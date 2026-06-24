import subprocess

import pytest

from s3backup import aws


def test_profile_args():
    assert aws.profile_args(None) == []
    assert aws.profile_args("work") == ["--profile", "work"]


def test_find_aws_missing(monkeypatch):
    monkeypatch.setattr(aws.shutil, "which", lambda _: None)
    with pytest.raises(aws.AwsError, match="Could not find"):
        aws.find_aws()


def test_find_aws_found(monkeypatch):
    monkeypatch.setattr(aws.shutil, "which", lambda _: "/usr/local/bin/aws")
    assert aws.find_aws() == "/usr/local/bin/aws"


def _fake_run(returncode=0, stdout="", stderr=""):
    def runner(args, **kwargs):
        runner.args = args
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return runner


def test_check_credentials_success(monkeypatch):
    monkeypatch.setattr(aws.shutil, "which", lambda _: "/usr/local/bin/aws")
    runner = _fake_run(stdout="arn:aws:iam::123:user/me\n")
    monkeypatch.setattr(aws.subprocess, "run", runner)
    arn = aws.check_credentials("work")
    assert arn == "arn:aws:iam::123:user/me"
    assert "--profile" in runner.args and "work" in runner.args
    assert "get-caller-identity" in runner.args


def test_check_credentials_failure(monkeypatch):
    monkeypatch.setattr(aws.shutil, "which", lambda _: "/usr/local/bin/aws")
    monkeypatch.setattr(
        aws.subprocess, "run", _fake_run(returncode=255, stderr="Unable to locate credentials")
    )
    with pytest.raises(aws.AwsError, match="credentials check failed"):
        aws.check_credentials()


def test_bucket_exists(monkeypatch):
    monkeypatch.setattr(aws.shutil, "which", lambda _: "/usr/local/bin/aws")
    monkeypatch.setattr(aws.subprocess, "run", _fake_run(returncode=0))
    assert aws.bucket_exists("b", "work") is True

    monkeypatch.setattr(aws.subprocess, "run", _fake_run(returncode=255))
    assert aws.bucket_exists("b") is False

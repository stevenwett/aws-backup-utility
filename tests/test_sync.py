import subprocess

import pytest

from s3backup import sync
from s3backup.config import Job, Settings


AWS = "/usr/local/bin/aws"


def make(job_kwargs=None, settings_kwargs=None):
    job = Job(name="docs", local_path="/data/docs", bucket="b", prefix="docs",
              **(job_kwargs or {}))
    settings = Settings(**(settings_kwargs or {}))
    return job, settings


def test_basic_argv():
    job, settings = make(settings_kwargs={"default_storage_class": "STANDARD_IA"})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    # Command head is exact; default macOS-cruft excludes follow.
    assert plan.argv[:7] == [
        AWS, "s3", "sync", "/data/docs", "s3://b/docs",
        "--storage-class", "STANDARD_IA",
    ]
    assert plan.dry_run is False
    assert plan.delete is False
    # Built-in excludes are applied automatically.
    assert "--exclude" in plan.argv
    assert ".DS_Store" in plan.argv


def test_profile_and_dryrun_and_delete():
    job, settings = make(settings_kwargs={"aws_profile": "work"})
    plan = sync.build_argv(job, settings, aws_path=AWS, dry_run=True, delete=True)
    assert plan.argv[:3] == [AWS, "--profile", "work"]
    assert "--dryrun" in plan.argv
    assert "--delete" in plan.argv


def test_storage_class_override():
    job, settings = make(job_kwargs={"storage_class": "GLACIER"})
    plan = sync.build_argv(job, settings, aws_path=AWS, storage_class="DEEP_ARCHIVE")
    assert plan.storage_class == "DEEP_ARCHIVE"
    idx = plan.argv.index("--storage-class")
    assert plan.argv[idx + 1] == "DEEP_ARCHIVE"


def test_job_storage_class_used_when_no_override():
    job, settings = make(job_kwargs={"storage_class": "GLACIER"})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    assert plan.storage_class == "GLACIER"


def test_delete_defaults_to_job_value():
    job, settings = make(job_kwargs={"delete": True})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    assert "--delete" in plan.argv
    # Explicit override wins.
    plan2 = sync.build_argv(job, settings, aws_path=AWS, delete=False)
    assert "--delete" not in plan2.argv


def _exclude_values(argv):
    return [argv[i + 1] for i, v in enumerate(argv) if v == "--exclude"]


def test_exclude_patterns_repeated():
    job, settings = make(job_kwargs={"exclude": ["*.tmp"]})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    values = _exclude_values(plan.argv)
    # User pattern plus the built-in macOS-cruft defaults.
    assert "*.tmp" in values
    assert ".DS_Store" in values
    assert ".fseventsd" in values


def test_default_excludes_applied_without_user_patterns():
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS)
    values = _exclude_values(plan.argv)
    from s3backup.config import DEFAULT_EXCLUDES
    assert set(DEFAULT_EXCLUDES).issubset(set(values))


def test_user_exclude_duplicating_default_is_not_repeated():
    # ".DS_Store" is already a default; specifying it again must not duplicate.
    job, settings = make(job_kwargs={"exclude": [".DS_Store"]})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    values = _exclude_values(plan.argv)
    assert values.count(".DS_Store") == 1


def test_no_prefix_destination():
    job = Job(name="x", local_path="/data", bucket="b")
    plan = sync.build_argv(job, Settings(), aws_path=AWS)
    assert plan.destination == "s3://b"


def test_parse_dry_run_counts():
    out = """
(dryrun) upload: ./a.txt to s3://b/a.txt
(dryrun) upload: ./sub/c.txt to s3://b/sub/c.txt
(dryrun) delete: s3://b/old.txt
""".strip()
    summary = sync.parse_dry_run(out)
    assert summary.uploads == 2
    assert summary.deletes == 1
    assert summary.total == 3
    assert summary.nothing_to_do is False
    assert len(summary.sample) == 3


def test_parse_dry_run_empty():
    summary = sync.parse_dry_run("")
    assert summary.nothing_to_do is True
    assert summary.total == 0


def test_run_dry_run_requires_dry_flag():
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS, dry_run=False)
    with pytest.raises(ValueError):
        sync.run_dry_run(plan)


def test_run_sync_rejects_dry_plan():
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS, dry_run=True)
    with pytest.raises(ValueError):
        sync.run_sync(plan)


def test_run_dry_run_parses(monkeypatch):
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS, dry_run=True)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, "(dryrun) upload: ./a.txt to s3://b/docs/a.txt\n", ""
        )

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    summary = sync.run_dry_run(plan)
    assert summary.uploads == 1


def test_run_dry_run_error(monkeypatch):
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS, dry_run=True)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "Access Denied")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    with pytest.raises(sync.aws.AwsError, match="Access Denied"):
        sync.run_dry_run(plan)

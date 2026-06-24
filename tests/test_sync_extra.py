from s3backup import sync
from s3backup.config import Job, Settings

AWS = "/usr/local/bin/aws"


def make(job_kwargs=None, settings_kwargs=None):
    job = Job(name="j", local_path="/data", bucket="b", prefix="p",
              **(job_kwargs or {}))
    return job, Settings(**(settings_kwargs or {}))


def test_glacier_classes_add_ignore_warnings():
    for sc in ("GLACIER", "DEEP_ARCHIVE"):
        job, settings = make(job_kwargs={"storage_class": sc})
        plan = sync.build_argv(job, settings, aws_path=AWS)
        assert "--ignore-glacier-warnings" in plan.argv


def test_non_glacier_no_ignore_warnings():
    job, settings = make(job_kwargs={"storage_class": "STANDARD_IA"})
    plan = sync.build_argv(job, settings, aws_path=AWS)
    assert "--ignore-glacier-warnings" not in plan.argv


def test_quiet_progress_uses_no_progress_not_only_show_errors():
    job, settings = make()
    plan = sync.build_argv(job, settings, aws_path=AWS, quiet_progress=True)
    # --no-progress keeps the per-file "upload:" lines we count;
    # --only-show-errors would suppress them, so it must NOT be used.
    assert "--no-progress" in plan.argv
    assert "--only-show-errors" not in plan.argv
    plan2 = sync.build_argv(job, settings, aws_path=AWS, quiet_progress=False)
    assert "--no-progress" not in plan2.argv


def test_transfer_env_sets_tuning_defaults():
    env = sync.transfer_env({})
    assert env["AWS_MAX_CONCURRENT_REQUESTS"] == "20"
    assert env["AWS_MULTIPART_THRESHOLD"] == "64MB"
    assert env["AWS_MULTIPART_CHUNKSIZE"] == "64MB"


def test_transfer_env_does_not_override_user_values():
    env = sync.transfer_env({"AWS_MAX_CONCURRENT_REQUESTS": "5"})
    assert env["AWS_MAX_CONCURRENT_REQUESTS"] == "5"


def test_parse_progress_line():
    assert sync.parse_progress_line("upload: ./a.txt to s3://b/a.txt") == "upload"
    assert sync.parse_progress_line("delete: s3://b/old.txt") == "delete"
    assert sync.parse_progress_line("Completed 256.0 KiB/1.0 MiB (1.2 MiB/s)") is None
    assert sync.parse_progress_line("") is None
    # A dry-run line is not a completed action line.
    assert sync.parse_progress_line("(dryrun) upload: ./a.txt to s3://b/a.txt") is None

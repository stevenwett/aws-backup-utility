import pytest

from s3backup import config as cfg


def write_config(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_config_path_honors_env(tmp_path, monkeypatch):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("S3BACKUP_CONFIG", str(target))
    assert cfg.config_path() == target


def test_config_path_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("S3BACKUP_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cfg.config_path() == tmp_path / "s3backup" / "config.toml"


def test_ensure_config_creates_template(tmp_path):
    path = tmp_path / "s3backup" / "config.toml"
    cfg.ensure_config(path)
    assert path.exists()
    assert "default_storage_class" in path.read_text()


def test_load_valid_config(tmp_path):
    path = write_config(
        tmp_path,
        """
[settings]
default_storage_class = "GLACIER"
aws_profile = "work"

[jobs.docs]
local_path = "~/Documents"
bucket = "my-bucket"
prefix = "docs"
storage_class = "DEEP_ARCHIVE"
delete = true
exclude = ["*.tmp"]
""",
    )
    config = cfg.load_config(path)
    assert config.settings.default_storage_class == "GLACIER"
    assert config.settings.aws_profile == "work"
    job = config.job("docs")
    assert job.bucket == "my-bucket"
    assert job.destination() == "s3://my-bucket/docs"
    assert job.storage_class == "DEEP_ARCHIVE"
    assert job.effective_storage_class(config.settings) == "DEEP_ARCHIVE"
    assert job.delete is True
    assert job.exclude == ["*.tmp"]


def test_job_inherits_default_storage_class(tmp_path):
    path = write_config(
        tmp_path,
        """
[settings]
default_storage_class = "STANDARD_IA"

[jobs.docs]
local_path = "~/Documents"
bucket = "my-bucket"
""",
    )
    config = cfg.load_config(path)
    job = config.job("docs")
    assert job.effective_storage_class(config.settings) == "STANDARD_IA"
    assert job.destination() == "s3://my-bucket"


def test_destination_strips_slashes(tmp_path):
    job = cfg.Job(name="x", local_path="/tmp", bucket="b", prefix="/a/b/")
    assert job.destination() == "s3://b/a/b"


def test_missing_required_field(tmp_path):
    path = write_config(
        tmp_path,
        """
[jobs.docs]
local_path = "~/Documents"
""",
    )
    with pytest.raises(cfg.ConfigError, match="missing required"):
        cfg.load_config(path)


def test_invalid_storage_class(tmp_path):
    path = write_config(
        tmp_path,
        """
[jobs.docs]
local_path = "~/Documents"
bucket = "b"
storage_class = "BOGUS"
""",
    )
    with pytest.raises(cfg.ConfigError, match="unknown storage_class"):
        cfg.load_config(path)


def test_invalid_default_storage_class(tmp_path):
    path = write_config(
        tmp_path,
        """
[settings]
default_storage_class = "NOPE"
""",
    )
    with pytest.raises(cfg.ConfigError, match="not a valid"):
        cfg.load_config(path)


def test_exclude_must_be_string_list(tmp_path):
    path = write_config(
        tmp_path,
        """
[jobs.docs]
local_path = "~/Documents"
bucket = "b"
exclude = [1, 2]
""",
    )
    with pytest.raises(cfg.ConfigError, match="list of strings"):
        cfg.load_config(path)


def test_job_lookup_error_lists_available(tmp_path):
    path = write_config(
        tmp_path,
        """
[jobs.docs]
local_path = "~/Documents"
bucket = "b"
""",
    )
    config = cfg.load_config(path)
    with pytest.raises(cfg.ConfigError, match="docs"):
        config.job("missing")


def test_validate_job_paths(tmp_path):
    good = cfg.Job(name="g", local_path=str(tmp_path), bucket="b")
    cfg.validate_job_paths(good)  # no raise

    missing = cfg.Job(name="m", local_path=str(tmp_path / "nope"), bucket="b")
    with pytest.raises(cfg.ConfigError, match="does not exist"):
        cfg.validate_job_paths(missing)

    f = tmp_path / "afile"
    f.write_text("x")
    notdir = cfg.Job(name="f", local_path=str(f), bucket="b")
    with pytest.raises(cfg.ConfigError, match="not a directory"):
        cfg.validate_job_paths(notdir)


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    config = cfg.Config(
        settings=cfg.Settings(default_storage_class="GLACIER", aws_profile="work"),
        jobs={
            "docs": cfg.Job(
                name="docs",
                local_path="~/Documents",
                bucket="my-bucket",
                prefix="docs",
                storage_class="DEEP_ARCHIVE",
                delete=True,
                exclude=["*.tmp"],
            )
        },
    )
    cfg.save_config(config, path)
    reloaded = cfg.load_config(path)
    assert reloaded.settings.default_storage_class == "GLACIER"
    assert reloaded.settings.aws_profile == "work"
    job = reloaded.job("docs")
    assert job.storage_class == "DEEP_ARCHIVE"
    assert job.delete is True
    assert job.exclude == ["*.tmp"]

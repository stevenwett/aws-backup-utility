"""Loading, validating, and writing the s3backup config file.

The config is TOML at ``~/.config/s3backup/config.toml`` (override with
``$S3BACKUP_CONFIG``). It defines global ``[settings]`` and one ``[jobs.<name>]``
table per backup job.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.9/3.10
    import tomli as tomllib

import tomli_w

from s3backup import tiers


class ConfigError(Exception):
    """Raised for any problem loading or validating the config."""


# macOS / filesystem cruft that should never be backed up. Applied to every job
# automatically, on top of each job's own ``exclude`` patterns.
DEFAULT_EXCLUDES: List[str] = [
    ".DS_Store",
    ".Spotlight-V100",
    ".Spotlight-V100/*",
    ".fseventsd",
    ".fseventsd/*",
    ".Trashes",
    ".Trashes/*",
    ".TemporaryItems",
    ".TemporaryItems/*",
    ".DocumentRevisions-V100",
    ".DocumentRevisions-V100/*",
]


@dataclass
class Settings:
    default_storage_class: str = tiers.DEFAULT_TIER
    aws_profile: Optional[str] = None


@dataclass
class Job:
    name: str
    local_path: str
    bucket: str
    prefix: str = ""
    storage_class: Optional[str] = None
    delete: bool = False
    exclude: List[str] = field(default_factory=list)

    def resolved_local_path(self) -> Path:
        """Expand ``~`` and environment variables in ``local_path``."""
        return Path(os.path.expanduser(os.path.expandvars(self.local_path)))

    def destination(self) -> str:
        """The ``s3://bucket/prefix`` destination URI."""
        prefix = self.prefix.strip("/")
        if prefix:
            return f"s3://{self.bucket}/{prefix}"
        return f"s3://{self.bucket}"

    def effective_storage_class(self, settings: Settings) -> str:
        return self.storage_class or settings.default_storage_class

    def effective_excludes(self) -> List[str]:
        """Built-in macOS-cruft excludes plus this job's own, de-duplicated."""
        seen = set()
        combined: List[str] = []
        for pattern in (*DEFAULT_EXCLUDES, *self.exclude):
            if pattern not in seen:
                seen.add(pattern)
                combined.append(pattern)
        return combined


@dataclass
class Config:
    settings: Settings
    jobs: Dict[str, Job]

    def job(self, name: str) -> Job:
        try:
            return self.jobs[name]
        except KeyError:
            available = ", ".join(sorted(self.jobs)) or "(none)"
            raise ConfigError(
                f"No job named '{name}'. Configured jobs: {available}"
            )


_TEMPLATE = """\
# s3backup configuration
#
# [settings] applies to every job unless a job overrides it.
[settings]
default_storage_class = "STANDARD_IA"
# aws_profile = "default"

# Define one [jobs.<name>] table per backup job. Run with: s3backup sync <name>
#
# [jobs.documents]
# local_path = "~/Documents"
# bucket = "my-backup-bucket"
# prefix = "documents"            # -> s3://my-backup-bucket/documents
# storage_class = "DEEP_ARCHIVE"  # optional; overrides default_storage_class
# delete = false                  # propagate local deletions to the bucket
# exclude = ["*.tmp"]             # extra patterns; macOS cruft (.DS_Store,
#                                 # .Spotlight-V100, .fseventsd, .Trashes, …)
#                                 # is always excluded automatically.
"""


def config_path() -> Path:
    """Resolve the config file path, honoring ``$S3BACKUP_CONFIG``."""
    override = os.environ.get("S3BACKUP_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "s3backup" / "config.toml"


def ensure_config(path: Optional[Path] = None) -> Path:
    """Create a commented template config on first run; return its path."""
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE)
    return path


def _parse_job(name: str, raw: dict) -> Job:
    if not isinstance(raw, dict):
        raise ConfigError(f"Job '{name}' must be a table.")
    missing = [k for k in ("local_path", "bucket") if not raw.get(k)]
    if missing:
        raise ConfigError(
            f"Job '{name}' is missing required field(s): {', '.join(missing)}"
        )
    storage_class = raw.get("storage_class")
    if storage_class is not None and not tiers.is_valid(storage_class):
        raise ConfigError(
            f"Job '{name}' has unknown storage_class '{storage_class}'. "
            f"Valid: {', '.join(tiers.TIER_NAMES)}"
        )
    exclude = raw.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(e, str) for e in exclude):
        raise ConfigError(f"Job '{name}': 'exclude' must be a list of strings.")
    return Job(
        name=name,
        local_path=str(raw["local_path"]),
        bucket=str(raw["bucket"]),
        prefix=str(raw.get("prefix", "")),
        storage_class=storage_class,
        delete=bool(raw.get("delete", False)),
        exclude=list(exclude),
    )


def _parse_settings(raw: dict) -> Settings:
    default_sc = raw.get("default_storage_class", tiers.DEFAULT_TIER)
    if not tiers.is_valid(default_sc):
        raise ConfigError(
            f"settings.default_storage_class '{default_sc}' is not a valid "
            f"storage class. Valid: {', '.join(tiers.TIER_NAMES)}"
        )
    profile = raw.get("aws_profile")
    return Settings(
        default_storage_class=default_sc,
        aws_profile=str(profile) if profile else None,
    )


def load_config(path: Optional[Path] = None) -> Config:
    """Load and validate the config, creating a template if absent."""
    path = ensure_config(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc

    settings = _parse_settings(raw.get("settings", {}))
    jobs_raw = raw.get("jobs", {})
    if not isinstance(jobs_raw, dict):
        raise ConfigError("'jobs' must be a table of named jobs.")
    jobs = {name: _parse_job(name, body) for name, body in jobs_raw.items()}
    return Config(settings=settings, jobs=jobs)


def validate_job_paths(job: Job) -> None:
    """Check that the job's local path exists and is a directory.

    Kept separate from parsing so that ``list``/``edit`` work even when a path
    is temporarily unavailable (e.g. an unmounted volume).
    """
    local = job.resolved_local_path()
    if not local.exists():
        raise ConfigError(f"Job '{job.name}': local_path does not exist: {local}")
    if not local.is_dir():
        raise ConfigError(f"Job '{job.name}': local_path is not a directory: {local}")


def save_config(config: Config, path: Optional[Path] = None) -> Path:
    """Serialize a Config back to TOML (used by add/edit)."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {"default_storage_class": config.settings.default_storage_class}
    if config.settings.aws_profile:
        settings["aws_profile"] = config.settings.aws_profile

    jobs: dict = {}
    for name, job in config.jobs.items():
        body: dict = {
            "local_path": job.local_path,
            "bucket": job.bucket,
            "prefix": job.prefix,
            "delete": job.delete,
        }
        if job.storage_class:
            body["storage_class"] = job.storage_class
        if job.exclude:
            body["exclude"] = job.exclude
        jobs[name] = body

    doc = {"settings": settings, "jobs": jobs}
    with open(path, "wb") as fh:
        tomli_w.dump(doc, fh)
    return path

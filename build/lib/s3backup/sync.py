"""Build and run ``aws s3 sync`` commands for a backup job.

Everything goes through an argv list passed to ``subprocess`` (never a shell
string), so paths and patterns with spaces are safe and there is no injection
surface.
"""

import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from s3backup import aws
from s3backup.config import Job, Settings


@dataclass
class SyncPlan:
    """A fully-resolved set of arguments for one sync invocation."""

    argv: List[str]
    source: str
    destination: str
    storage_class: str
    dry_run: bool
    delete: bool


# Storage classes where objects can't be read back without a restore. Syncing
# into these prints a per-object warning unless we pass --ignore-glacier-warnings.
_GLACIER_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}


@dataclass
class DryRunSummary:
    uploads: int = 0
    deletes: int = 0
    sample: List[str] = field(default_factory=list)  # first few human lines

    @property
    def total(self) -> int:
        return self.uploads + self.deletes

    @property
    def nothing_to_do(self) -> bool:
        return self.total == 0


# Matches lines like:
#   (dryrun) upload: ./a.txt to s3://bucket/a.txt
#   (dryrun) delete: s3://bucket/old.txt
_LINE = re.compile(r"^\(dryrun\)\s+(upload|delete):", re.IGNORECASE)

# Matches a *completed* (non-dryrun) action line streamed during a real sync:
#   upload: ./a.txt to s3://bucket/a.txt
#   delete: s3://bucket/old.txt
_PROGRESS_LINE = re.compile(r"^(upload|delete):\s+(.*)$")


def parse_progress_line(line: str) -> Optional[str]:
    """Return the action ('upload'/'delete') if ``line`` is a completed action.

    Used by the streaming runner to count finished files. In-place transfer
    progress lines (e.g. 'Completed 256.0 KiB/...') are ignored.
    """
    m = _PROGRESS_LINE.match(line.strip())
    if not m:
        return None
    return m.group(1).lower()


def build_argv(
    job: Job,
    settings: Settings,
    *,
    aws_path: str,
    dry_run: bool = False,
    delete: Optional[bool] = None,
    storage_class: Optional[str] = None,
    quiet_progress: bool = False,
) -> SyncPlan:
    """Construct the argv for ``aws s3 sync`` for this job.

    ``delete`` and ``storage_class`` override the job/settings values when given
    (used by CLI flags and the TUI). ``delete=None`` falls back to the job.

    ``quiet_progress`` adds ``--no-progress``: the CLI still prints one
    ``upload: …`` line per completed file (which we parse to advance progress),
    but drops the in-place per-file byte counter that spams a piped log. Note we
    must NOT use ``--only-show-errors`` here — that suppresses the per-file
    lines too, leaving us nothing to count.
    """
    source = str(job.resolved_local_path())
    destination = job.destination()
    sc = storage_class or job.effective_storage_class(settings)
    do_delete = job.delete if delete is None else delete

    argv: List[str] = [aws_path]
    argv += aws.profile_args(settings.aws_profile)
    argv += ["s3", "sync", source, destination, "--storage-class", sc]
    if dry_run:
        argv.append("--dryrun")
    if do_delete:
        argv.append("--delete")
    # Suppress per-object "you can't read this back" warnings for archive tiers;
    # without it a 250k-object DEEP_ARCHIVE upload prints 250k warning lines.
    if sc in _GLACIER_CLASSES:
        argv.append("--ignore-glacier-warnings")
    if quiet_progress:
        argv.append("--no-progress")
    for pattern in job.effective_excludes():
        argv += ["--exclude", pattern]

    return SyncPlan(
        argv=argv,
        source=source,
        destination=destination,
        storage_class=sc,
        dry_run=dry_run,
        delete=do_delete,
    )


def transfer_env(base_env: Optional[dict] = None) -> dict:
    """Environment tuned for high-throughput large transfers.

    The AWS CLI reads these ``AWS_*`` transfer settings; bumping concurrency and
    multipart sizing materially improves throughput for a multi-TB upload over a
    fast link, without affecting correctness.
    """
    import os as _os

    env = dict(base_env if base_env is not None else _os.environ)
    env.setdefault("AWS_MAX_CONCURRENT_REQUESTS", "20")
    env.setdefault("AWS_MULTIPART_THRESHOLD", "64MB")
    env.setdefault("AWS_MULTIPART_CHUNKSIZE", "64MB")
    return env


def parse_dry_run(output: str) -> DryRunSummary:
    """Count upload/delete actions from captured ``--dryrun`` output."""
    summary = DryRunSummary()
    for line in output.splitlines():
        line = line.strip()
        m = _LINE.match(line)
        if not m:
            continue
        if m.group(1).lower() == "upload":
            summary.uploads += 1
        else:
            summary.deletes += 1
        if len(summary.sample) < 20:
            summary.sample.append(line)
    return summary


def run_dry_run(plan: SyncPlan) -> DryRunSummary:
    """Run a dry-run plan, capturing output, and return the parsed summary."""
    if not plan.dry_run:
        raise ValueError("run_dry_run requires a plan built with dry_run=True")
    result = subprocess.run(plan.argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise aws.AwsError(
            "Dry-run failed:\n" + (result.stderr.strip() or result.stdout.strip())
        )
    return parse_dry_run(result.stdout)


def run_sync(plan: SyncPlan) -> int:
    """Run a real sync, streaming output live to the terminal. Returns exit code."""
    if plan.dry_run:
        raise ValueError("run_sync must not be called with a dry-run plan")
    result = subprocess.run(plan.argv)
    return result.returncode

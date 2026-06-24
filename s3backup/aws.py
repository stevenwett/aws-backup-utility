"""Thin helpers around the ``aws`` CLI binary: discovery and preflight checks.

These shell out to ``aws`` for credential and bucket checks so the rest of the
app can fail early with a clear message instead of midway through a sync.
"""

import shutil
import subprocess
from typing import List, Optional


class AwsError(Exception):
    """Raised when the aws binary is missing or a preflight check fails."""


def find_aws() -> str:
    """Return the path to the ``aws`` binary or raise ``AwsError``."""
    path = shutil.which("aws")
    if not path:
        raise AwsError(
            "Could not find the 'aws' CLI on your PATH. Install it from "
            "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
        )
    return path


def profile_args(profile: Optional[str]) -> List[str]:
    """``--profile P`` as a list (empty when no profile configured)."""
    return ["--profile", profile] if profile else []


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def check_credentials(profile: Optional[str] = None) -> str:
    """Verify credentials work via ``sts get-caller-identity``.

    Returns the caller's ARN on success; raises ``AwsError`` otherwise.
    """
    aws = find_aws()
    args = [aws, *profile_args(profile), "sts", "get-caller-identity",
            "--query", "Arn", "--output", "text"]
    result = _run(args)
    if result.returncode != 0:
        raise AwsError(
            "AWS credentials check failed. Configure credentials with "
            "'aws configure'.\n" + (result.stderr.strip() or result.stdout.strip())
        )
    return result.stdout.strip()


def bucket_exists(bucket: str, profile: Optional[str] = None) -> bool:
    """Return True if the bucket is reachable via ``s3api head-bucket``."""
    aws = find_aws()
    args = [aws, *profile_args(profile), "s3api", "head-bucket",
            "--bucket", bucket]
    result = _run(args)
    return result.returncode == 0


def prefix_is_empty(bucket: str, prefix: str, profile: Optional[str] = None) -> bool:
    """True if no objects exist under ``prefix`` (one cheap LIST, max 1 key).

    Used to detect an initial upload so we can skip the slow full dry-run
    preview — when the destination is empty, everything uploads anyway.
    """
    aws = find_aws()
    args = [aws, *profile_args(profile), "s3api", "list-objects-v2",
            "--bucket", bucket, "--max-items", "1",
            "--query", "Contents[0].Key", "--output", "text"]
    if prefix:
        args += ["--prefix", prefix.strip("/") + "/"]
    result = _run(args)
    if result.returncode != 0:
        # If we can't tell, assume not empty so we don't skip a needed preview.
        return False
    out = result.stdout.strip()
    return out in ("", "None")


def set_abort_incomplete_uploads_lifecycle(
    bucket: str, days: int = 7, profile: Optional[str] = None
) -> None:
    """Set a lifecycle rule to abort incomplete multipart uploads after ``days``.

    Plugs the silent cost leak where an interrupted large upload leaves billable
    orphaned multipart parts in the bucket indefinitely. Idempotent: applies the
    same rule each time.
    """
    import json
    import os
    import tempfile

    aws = find_aws()
    config = {
        "Rules": [
            {
                "ID": "s3backup-abort-incomplete-multipart-uploads",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": days},
            }
        ]
    }
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(config, fh)
        args = [aws, *profile_args(profile), "s3api",
                "put-bucket-lifecycle-configuration",
                "--bucket", bucket,
                "--lifecycle-configuration", f"file://{tmp}"]
        result = _run(args)
        if result.returncode != 0:
            raise AwsError(
                "Could not set lifecycle rule on the bucket:\n"
                + (result.stderr.strip() or result.stdout.strip())
            )
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

"""Walk a local directory to estimate totals used as the progress denominator.

This is intentionally a cheap, best-effort estimate: file count and total byte
size of what *would* be synced. It applies the job's effective excludes with
``fnmatch`` semantics that approximate ``aws s3 sync --exclude``. It does not
talk to S3, so on an incremental run it over-counts (it counts everything, not
just what changed) — callers should only use it as a denominator for the
initial upload, where remote is empty and the estimate is accurate.
"""

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import List


@dataclass
class ScanResult:
    files: int = 0
    bytes: int = 0


def _excluded(rel_path: str, patterns: List[str]) -> bool:
    """Approximate ``aws s3 sync --exclude`` matching for a relative path.

    AWS matches each ``--exclude`` pattern against the key (the path relative to
    the source root, using ``/`` separators). We also match against the
    basename so a bare ``.DS_Store`` excludes the file in any directory, which
    mirrors how users expect these patterns to behave.
    """
    base = rel_path.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch(rel_path, pat) or fnmatch(base, pat):
            return True
    return False


def scan_local(root: Path, exclude: List[str]) -> ScanResult:
    """Return (files, bytes) under ``root``, skipping excluded paths.

    Symlinks are not followed (matching ``aws s3 sync`` default of treating the
    link target's contents conservatively). Unreadable entries are skipped.
    """
    result = ScanResult()
    root = Path(root)
    root_str = str(root)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_str)
        rel_dir = "" if rel_dir == "." else rel_dir

        # Prune excluded directories in-place so we don't descend into them.
        kept = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if not _excluded(rel, exclude):
                kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _excluded(rel, exclude):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if os.path.islink(full):
                continue
            result.files += 1
            result.bytes += st.st_size

    return result


def human_bytes(n: int) -> str:
    """Format a byte count as a human-readable string (e.g. '2.1 TB')."""
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(n)
    for unit in units:
        if value < step or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= step
    return f"{n} B"

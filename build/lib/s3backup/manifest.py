"""Local change detection so we avoid the paid S3 LIST when nothing changed.

After each successful sync we snapshot the local tree (path -> size, mtime) to
``~/.config/s3backup/state/<job>.manifest.json``. Before the next sync we walk
the tree again (free, local-only) and compare. If no file is new, modified, or
larger, we skip the run entirely — zero S3 requests, zero cost.

This mirrors ``aws s3 sync``'s own change heuristic (size + mtime), so we don't
skip anything the real sync would have uploaded.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Tuple

from s3backup.config import config_path
from s3backup.scan import _excluded


# path -> (size, mtime_ns)
Manifest = Dict[str, Tuple[int, int]]


@dataclass
class ChangeSet:
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.modified)


def manifest_file(job: str) -> Path:
    return config_path().parent / "state" / f"{job}.manifest.json"


def build_manifest(root: Path, exclude: List[str]) -> Manifest:
    """Snapshot the local tree as {relpath: (size, mtime_ns)}, honoring excludes."""
    root = Path(root)
    root_str = str(root)
    manifest: Manifest = {}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_str)
        rel_dir = "" if rel_dir == "." else rel_dir

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
            if os.path.islink(full):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            manifest[rel] = (st.st_size, st.st_mtime_ns)

    return manifest


def diff(previous: Manifest, current: Manifest) -> ChangeSet:
    """Compute what's new or changed in ``current`` relative to ``previous``.

    We treat a file as changed when its size differs or its mtime moved (newer
    or older both count, matching how a restore/edit would look). Local-only
    deletions are intentionally ignored: with delete disabled they don't need a
    sync, and reporting them would trigger needless paid runs.
    """
    changes = ChangeSet()
    for path, (size, mtime) in current.items():
        if path not in previous:
            changes.added.append(path)
            continue
        prev_size, prev_mtime = previous[path]
        if size != prev_size or mtime != prev_mtime:
            changes.modified.append(path)
    return changes


def load_manifest(job: str) -> Manifest:
    """Load the saved manifest, or empty if none (=> everything looks new)."""
    path = manifest_file(job)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    # JSON turns tuples into lists; coerce back.
    return {k: (int(v[0]), int(v[1])) for k, v in raw.items() if len(v) == 2}


def save_manifest(job: str, manifest: Manifest) -> None:
    """Atomically persist the manifest."""
    path = manifest_file(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({k: [v[0], v[1]] for k, v in manifest.items()})
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{job}.manifest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

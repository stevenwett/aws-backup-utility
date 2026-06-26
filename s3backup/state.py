"""Persistent per-job run state, shared between the runner and readers.

The running sync (which may be a background launchd job) writes a small JSON
state file; ``s3backup status`` / ``--watch`` read it from any terminal. This
decoupling is what lets the user close their laptop and still check progress
later.

State lives at ``~/.config/s3backup/state/<job>.json`` and is written
atomically (write temp + rename) so a reader never sees a half-written file.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from s3backup.config import config_path


# Run phases, surfaced in `status`.
PHASE_IDLE = "idle"
PHASE_SCANNING = "scanning"          # walking local tree
PHASE_NO_CHANGES = "no-changes"      # nothing to do; skipped (no S3 calls)
PHASE_UPLOADING = "uploading"
PHASE_RETRYING = "retrying"          # transient failure, backing off
PHASE_DONE = "done"
PHASE_FAILED = "failed"              # gave up after retries


@dataclass
class RunState:
    job: str
    phase: str = PHASE_IDLE
    pid: Optional[int] = None
    started_at: Optional[float] = None      # epoch seconds
    updated_at: Optional[float] = None
    finished_at: Optional[float] = None

    # The whole backup set (everything under the job's local path).
    total_files: int = 0
    total_bytes: int = 0
    # What this run actually needs to upload (new/changed files only).
    pending_files: int = 0
    pending_bytes: int = 0
    # What has been uploaded so far this run.
    done_files: int = 0
    done_bytes: int = 0

    attempt: int = 0                         # current retry attempt (0 = first)
    last_line: str = ""                      # last meaningful aws output line
    message: str = ""                        # human status / error summary
    exit_code: Optional[int] = None

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def percent(self) -> float:
        # Progress is measured against this run's pending work, not the whole
        # library — so an already-backed-up library doesn't read as 0%.
        if self.pending_bytes <= 0:
            # Nothing to upload => fully in sync.
            return 100.0
        return min(100.0, 100.0 * self.done_bytes / self.pending_bytes)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def eta_seconds(self) -> Optional[float]:
        """Estimate remaining seconds from average byte throughput so far."""
        if self.phase != PHASE_UPLOADING:
            return None
        if self.done_bytes <= 0 or self.elapsed <= 0:
            return None
        rate = self.done_bytes / self.elapsed  # bytes/sec
        if rate <= 0:
            return None
        remaining = max(0, self.pending_bytes - self.done_bytes)
        return remaining / rate

    @property
    def is_active(self) -> bool:
        return self.phase in (PHASE_SCANNING, PHASE_UPLOADING, PHASE_RETRYING)


def state_dir() -> Path:
    return config_path().parent / "state"


def state_file(job: str) -> Path:
    return state_dir() / f"{job}.json"


def write_state(state: RunState) -> None:
    """Atomically persist state to disk."""
    state.touch()
    path = state_file(state.job)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(asdict(state), indent=2)
    # Write to a temp file in the same dir, then rename for atomicity.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{state.job}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_state(job: str) -> Optional[RunState]:
    """Load state for a job, or None if no run has been recorded."""
    path = state_file(job)
    if not path.exists():
        return None
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    # Tolerate older/newer schemas: keep only known fields.
    known = {f for f in RunState.__dataclass_fields__}  # type: ignore[attr-defined]
    filtered = {k: v for k, v in raw.items() if k in known}
    filtered.setdefault("job", job)
    return RunState(**filtered)


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with this PID currently exists."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True

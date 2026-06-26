"""The resilient, cost-conscious backup runner.

One ``run_backup`` call performs a complete, walk-away-safe backup of a job:

1. Scan the local tree (free) and diff against the saved manifest. If nothing
   changed, exit immediately — no S3 calls, no cost.
2. Otherwise compute totals (for progress) and decide whether this is an
   initial upload (empty remote prefix) to skip the slow dry-run preview.
3. Run ``aws s3 sync`` with tuned concurrency, streaming output: parse each
   completed-file line to advance a persisted progress state.
4. On transient failure, retry with backoff (``aws s3 sync`` resumes by
   skipping already-uploaded objects). Surface attempts in the state file.
5. On success, save the new manifest and notify; on permanent failure, notify.

The progress state is written to disk continuously so ``s3backup status`` /
``--watch`` can observe it from any terminal, including while this runs as a
background daemon.
"""

import subprocess
import time
from typing import Callable, Optional

from s3backup import aws, manifest, notify, scan, state
from s3backup.config import Config, Job
from s3backup.sync import build_argv, parse_progress_line, transfer_env


# Retry policy for transient failures (network blips, throttling, sleep/wake).
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 10


def _emit(write_state, st: state.RunState) -> None:
    state.write_state(st)


def run_backup(
    config: Config,
    job: Job,
    *,
    force: bool = False,
    delete: Optional[bool] = None,
    storage_class: Optional[str] = None,
    on_line: Optional[Callable[[state.RunState], None]] = None,
    keep_awake: bool = True,
) -> int:
    """Run a complete backup for ``job``. Returns a process-style exit code.

    ``force`` skips local-change detection and always runs the sync.
    ``on_line`` is called after each state update (used to drive a live bar).
    """
    aws_path = aws.find_aws()
    settings = config.settings
    profile = settings.aws_profile

    st = state.RunState(job=job.name, pid=_pid(), started_at=time.time())

    def publish():
        state.write_state(st)
        if on_line:
            on_line(st)

    # --- Phase 1: local change detection (free, no S3) --------------------
    st.phase = state.PHASE_SCANNING
    st.message = "Scanning local files…"
    publish()

    root = job.resolved_local_path()
    excludes = job.effective_excludes()
    current = manifest.build_manifest(root, excludes)

    # The whole backup set — shown so the user sees the full picture.
    st.total_files = len(current)
    st.total_bytes = sum(size for size, _ in current.values())

    previous = manifest.load_manifest(job.name)
    changes = manifest.diff(previous, current)

    if not force and previous and not changes.has_changes:
        st.phase = state.PHASE_NO_CHANGES
        st.finished_at = time.time()
        st.exit_code = 0
        st.message = "No local changes — nothing to upload (no S3 calls made)."
        publish()
        return 0

    # What this run actually needs to upload: the changed/new files. When we
    # have no prior manifest (first run, or it was cleared) we can't know what's
    # already in S3 locally, so treat the whole set as pending — aws s3 sync
    # will still skip anything already uploaded, and the bar tracks real
    # transfers as they complete.
    if previous and not force:
        changed_paths = set(changes.added) | set(changes.modified)
        st.pending_files = len(changed_paths)
        st.pending_bytes = sum(
            current[p][0] for p in changed_paths if p in current
        )
    else:
        st.pending_files = st.total_files
        st.pending_bytes = st.total_bytes

    # Cost guard: make sure interrupted multipart uploads can't linger as
    # billable orphans. Best-effort; don't fail the backup if it can't be set.
    try:
        aws.set_abort_incomplete_uploads_lifecycle(job.bucket, days=7, profile=profile)
    except aws.AwsError:
        pass

    # --- Phase 2: sync with retries --------------------------------------
    plan = build_argv(
        job, settings, aws_path=aws_path, dry_run=False,
        delete=delete, storage_class=storage_class, quiet_progress=True,
    )
    env = transfer_env()
    argv = _wrap_keep_awake(plan.argv) if keep_awake else plan.argv

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        st.attempt = attempt
        st.phase = state.PHASE_UPLOADING
        st.message = (
            f"Uploading to {plan.destination} ({plan.storage_class})"
            if attempt == 0
            else f"Resuming upload (attempt {attempt + 1}/{MAX_ATTEMPTS})…"
        )
        # On a resume, already-uploaded files are skipped by aws; reset the
        # per-attempt counters so progress reflects this attempt's transfers
        # while totals stay fixed.
        publish()

        code, last_error = _stream_sync(argv, env, st, publish)
        if code == 0:
            # aws s3 sync's exit code is the source of truth: 0 means every
            # file that needed uploading was uploaded (it skips files already
            # in S3). Record the manifest so future runs can detect no-ops.
            st.phase = state.PHASE_DONE
            st.finished_at = time.time()
            st.exit_code = 0
            st.done_bytes = st.pending_bytes  # finished => pending fully done
            if st.done_files == 0:
                st.message = "Already in sync — no new files to upload."
            else:
                st.message = f"Backup complete — uploaded {st.done_files:,} new file(s)."
            manifest.save_manifest(job.name, current)
            publish()
            notify.notify("s3backup", f"'{job.name}': {st.message}")
            return 0

        # Failed this attempt. Back off and retry unless we're out of attempts.
        if attempt < MAX_ATTEMPTS - 1:
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt)
            st.phase = state.PHASE_RETRYING
            st.message = f"Transfer failed; retrying in {delay}s… ({last_error[:120]})"
            publish()
            time.sleep(delay)

    # Out of retries.
    st.phase = state.PHASE_FAILED
    st.finished_at = time.time()
    st.exit_code = 1
    st.message = f"Backup failed after {MAX_ATTEMPTS} attempts: {last_error[:200]}"
    publish()
    notify.notify("s3backup", f"'{job.name}' backup FAILED. Check 's3backup status'.")
    return 1


def _stream_sync(argv, env, st: state.RunState, publish) -> tuple:
    """Run one ``aws s3 sync`` attempt, streaming and counting completed files.

    Returns ``(exit_code, last_error_text)``. Updates ``st`` as lines arrive but
    throttles disk writes to ~once per second. Progress bytes are estimated from
    the fraction of *pending* files completed (the CLI stream gives no per-file
    size), so the bar tracks this run's actual upload work.
    """
    def _estimate_bytes():
        if st.pending_files:
            st.done_bytes = int(
                st.pending_bytes * min(st.done_files, st.pending_files) / st.pending_files
            )

    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    st.pid = proc.pid
    last_write = 0.0
    error_tail = []

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        action = parse_progress_line(line)
        if action == "upload":
            st.done_files += 1
            st.last_line = line[:200]
        elif action is None:
            # Non-action output: keep a short tail for error reporting only.
            lowered = line.lower()
            if "error" in lowered or "fail" in lowered:
                error_tail.append(line)
                error_tail[:] = error_tail[-5:]

        now = time.time()
        if now - last_write >= 1.0:
            _estimate_bytes()
            publish()
            last_write = now

    proc.wait()
    _estimate_bytes()
    publish()
    return proc.returncode, "\n".join(error_tail)


def _wrap_keep_awake(argv):
    """Prefix the command with ``caffeinate`` so the Mac won't sleep mid-upload.

    ``-i`` prevents idle sleep, ``-s`` prevents system sleep on AC power; the
    sync runs as caffeinate's child so the assertion lasts exactly as long as
    the transfer.
    """
    import shutil

    caffeinate = shutil.which("caffeinate")
    if not caffeinate:
        return argv
    return [caffeinate, "-i", "-s", *argv]


def _pid() -> int:
    import os

    return os.getpid()

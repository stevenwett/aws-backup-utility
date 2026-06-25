"""Managed background runs via launchd: start, stop, and schedule a job.

Two flavors of agent:

* **continuous** (``s3backup start <job>``): a one-shot KeepAlive agent that
  runs the backup to completion, restarting on crash or reboot. Used for the
  big initial upload — the user runs one command, closes the laptop, and it
  keeps going until done. It tears itself down on success.
* **scheduled** (``s3backup schedule <job>``): runs daily; with manifest-based
  change detection a no-change day costs nothing.

The agent shells out to ``s3backup _run <job>`` (a hidden internal command) so
all the runner logic lives in one place.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def _label(job: str) -> str:
    return f"com.s3backup.{job}"


def plist_path(job: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(job)}.plist"


def _executable() -> str:
    """Absolute path to the installed ``s3backup`` entry point.

    Prefer the console script next to the running interpreter so a venv install
    keeps working from launchd's minimal PATH; fall back to ``-m s3backup``.
    """
    candidate = Path(sys.argv[0])
    if candidate.name == "s3backup" and candidate.exists():
        return str(candidate.resolve())
    guess = Path(sys.executable).parent / "s3backup"
    if guess.exists():
        return str(guess)
    return ""


def _program_args(job: str, scheduled: bool) -> List[str]:
    exe = _executable()
    sub = ["_run", job]
    if scheduled:
        sub.append("--scheduled")
    if exe:
        return [exe, *sub]
    # Fallback: invoke the module via the interpreter.
    return [sys.executable, "-m", "s3backup", *sub]


def _log_dir() -> Path:
    from s3backup.config import config_path

    d = config_path().parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_plist(job: str, *, scheduled: bool, hour: int = 3, minute: int = 0) -> dict:
    """Construct the launchd plist dict for a job's agent."""
    logs = _log_dir()
    doc = {
        "Label": _label(job),
        "ProgramArguments": _program_args(job, scheduled),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(logs / f"{job}.out.log"),
        "StandardErrorPath": str(logs / f"{job}.err.log"),
    }
    if scheduled:
        # Daily at HH:MM. launchd runs a missed job at next wake.
        doc["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
        doc["RunAtLoad"] = False
    else:
        # Continuous: run now and keep alive until it exits 0 (success).
        doc["RunAtLoad"] = True
        doc["KeepAlive"] = {"SuccessfulExit": False}
    return doc


def write_plist(job: str, *, scheduled: bool, hour: int = 3, minute: int = 0) -> Path:
    path = plist_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(job, scheduled=scheduled, hour=hour, minute=minute), fh)
    return path


def _domain_target(job: str) -> str:
    return f"gui/{os.getuid()}/{_label(job)}"


def is_loaded(job: str) -> bool:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return _label(job) in result.stdout


def load(job: str) -> None:
    """Bootstrap the agent into the user GUI domain (idempotent)."""
    # Boot out any stale copy first so we pick up a rewritten plist.
    subprocess.run(["launchctl", "bootout", _domain_target(job)],
                   capture_output=True, text=True)
    path = plist_path(job)
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not load launchd agent:\n"
            + (result.stderr.strip() or result.stdout.strip())
        )


def unload(job: str) -> bool:
    """Boot out the agent. Returns True if it was loaded."""
    result = subprocess.run(["launchctl", "bootout", _domain_target(job)],
                            capture_output=True, text=True)
    return result.returncode == 0


def kickstart(job: str) -> None:
    """Force an immediate run of a loaded agent."""
    subprocess.run(["launchctl", "kickstart", "-k", _domain_target(job)],
                   capture_output=True, text=True)


def remove(job: str) -> None:
    """Unload and delete the agent's plist entirely."""
    unload(job)
    p = plist_path(job)
    if p.exists():
        p.unlink()

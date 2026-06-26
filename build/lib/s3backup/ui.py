"""Shared terminal rendering used by both the CLI and the TUI."""

import time
from typing import Optional

from rich.console import Console
from rich.table import Table

from s3backup import aws, tiers
from s3backup.config import Config, Job, Settings
from s3backup.sync import (
    DryRunSummary,
    SyncPlan,
    build_argv,
    run_dry_run,
    run_sync,
)

console = Console()


def render_jobs(config: Config) -> None:
    table = Table(title="Configured backup jobs")
    table.add_column("Job", style="bold")
    table.add_column("Local path")
    table.add_column("Destination")
    table.add_column("Tier")
    table.add_column("Delete")
    if not config.jobs:
        console.print("[yellow]No jobs configured yet.[/] Run 's3backup add'.")
        return
    for name, job in config.jobs.items():
        table.add_row(
            name,
            str(job.resolved_local_path()),
            job.destination(),
            job.effective_storage_class(config.settings),
            "[red]on[/]" if job.delete else "off",
        )
    console.print(table)


def render_dry_run(summary: DryRunSummary, plan: SyncPlan) -> None:
    console.print()
    console.rule("[bold]Dry-run preview")
    console.print(f"Source:      {plan.source}")
    console.print(f"Destination: {plan.destination}")
    console.print(f"Storage:     [cyan]{plan.storage_class}[/]")
    console.print(f"Delete mode: {'[red]ON[/]' if plan.delete else 'off'}")
    console.print()
    if summary.nothing_to_do:
        console.print("[green]Nothing to do — already in sync.[/]")
        return
    console.print(
        f"[bold]{summary.uploads}[/] upload(s), "
        f"[bold]{summary.deletes}[/] delete(s)"
    )
    for line in summary.sample:
        styled = line.replace("(dryrun) ", "")
        color = "red" if styled.startswith("delete") else "green"
        console.print(f"  [{color}]{styled}[/]")
    if summary.total > len(summary.sample):
        console.print(f"  … and {summary.total - len(summary.sample)} more")


def execute_job(
    config: Config,
    job: Job,
    *,
    dry_run_only: bool = False,
    delete: Optional[bool] = None,
    storage_class: Optional[str] = None,
    assume_yes: bool = False,
    confirm=None,
) -> int:
    """Run the dry-run preview then (optionally) the real sync for ``job``.

    ``confirm`` is an optional callable returning a bool used to gate the real
    run. When ``assume_yes`` is True or ``dry_run_only`` is True, it is skipped.
    Returns a process-style exit code (0 = success).
    """
    aws_path = aws.find_aws()
    settings: Settings = config.settings

    preview_plan = build_argv(
        job, settings, aws_path=aws_path, dry_run=True,
        delete=delete, storage_class=storage_class,
    )
    summary = run_dry_run(preview_plan)
    render_dry_run(summary, preview_plan)

    if dry_run_only:
        return 0
    if summary.nothing_to_do:
        return 0

    if not assume_yes:
        if confirm is None or not confirm(summary, preview_plan):
            console.print("[yellow]Aborted — no changes made.[/]")
            return 1

    real_plan = build_argv(
        job, settings, aws_path=aws_path, dry_run=False,
        delete=delete, storage_class=storage_class,
    )
    console.print()
    console.rule("[bold]Syncing")
    start = time.monotonic()
    code = run_sync(real_plan)
    elapsed = time.monotonic() - start
    console.print()
    if code == 0:
        console.print(
            f"[green]Done[/] — synced to [cyan]{real_plan.destination}[/] "
            f"({real_plan.storage_class}) in {elapsed:.1f}s"
        )
    else:
        console.print(f"[red]Sync failed[/] (exit {code}).")
    return code


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


_PHASE_STYLE = {
    "idle": "dim",
    "scanning": "cyan",
    "no-changes": "green",
    "uploading": "cyan",
    "retrying": "yellow",
    "done": "green",
    "failed": "red",
}


def _display_phase(st) -> str:
    """User-facing label for a run state.

    The internal phases ``no-changes`` (skipped via local scan, no S3 calls) and
    ``done`` with zero uploads (verified against S3, nothing new) both mean the
    same thing to a user — so we surface both as ``up to date``. The how-it-got-
    there detail stays in the dim message line.
    """
    from s3backup import state as state_mod

    # A successful run means everything that needed uploading was uploaded, so
    # the job is up to date — whether it uploaded files or found nothing new.
    if st.phase in (state_mod.PHASE_NO_CHANGES, state_mod.PHASE_DONE):
        return "up to date"
    return st.phase


def render_status(st, job_name: Optional[str] = None) -> None:
    """Render a one-shot snapshot of a job's run state.

    ``job_name`` lets us still show schedule info for a job that has a schedule
    but has never run yet (so ``st`` is None).
    """
    from s3backup import state as state_mod
    from s3backup.scan import human_bytes

    if st is None:
        name = job_name or "(unknown)"
        console.print(f"[bold]{name}[/] — [dim]no run recorded yet[/]")
        if job_name:
            _render_schedule_line(job_name)
        return

    label = _display_phase(st)
    style = "green" if label == "up to date" else _PHASE_STYLE.get(st.phase, "white")
    running = st.is_active and state_mod.pid_alive(st.pid)
    live = " [dim](running)[/]" if running else ""
    console.print(f"[bold]{st.job}[/] — [{style}]{label}[/]{live}")

    _render_schedule_line(st.job)

    # Line 1 — the whole backup set, so you always see the full picture.
    if st.total_files:
        console.print(
            f"  Backup:    {human_bytes(st.total_bytes)} "
            f"across {st.total_files:,} files"
        )

    # Upload detail + bar — only while there is (or was) real work this run.
    has_pending = st.phase in ("uploading", "retrying") or (
        st.phase == "done" and st.pending_files > 0
    )
    if has_pending:
        pending_files = max(st.pending_files, 0)
        remaining_files = max(pending_files - st.done_files, 0)
        remaining_bytes = max(st.pending_bytes - st.done_bytes, 0)
        console.print(
            f"  To upload: {human_bytes(st.pending_bytes)} "
            f"in {pending_files:,} new file(s) "
            f"[dim]({human_bytes(remaining_bytes)} / "
            f"{remaining_files:,} remaining)[/]"
        )
        console.print(
            "  " + _progress_bar(st.percent)
            + f"  [bold]{st.percent:.1f}%[/]  "
            + f"{st.done_files:,} / {pending_files:,} uploaded"
        )
        line2 = f"  elapsed {_fmt_duration(st.elapsed)}"
        if st.phase == "uploading":
            line2 += f" · ETA ~{_fmt_duration(st.eta_seconds)}"
        if st.attempt:
            line2 += f" · attempt {st.attempt + 1}"
        console.print(line2)

    # Dim detail line. For an up-to-date job this carries the only extra info
    # worth keeping (whether S3 was contacted); the header already says the rest.
    if st.message:
        console.print(f"  [dim]{st.message}[/]")
    if st.last_line and running:
        console.print(f"  [dim]last: {st.last_line}[/]")


def _fmt_until(seconds) -> str:
    """Human 'time from now' for a future epoch delta (e.g. 'in 3h 5m')."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"in {h}h {m}m"
    if m:
        return f"in {m}m"
    return "in <1m"


def _render_schedule_line(job: str) -> None:
    """Show whether the job is scheduled/continuous and when it next runs."""
    import time as _time

    from s3backup import daemon

    info = daemon.schedule_info(job)
    if info is None:
        console.print("  [dim]Schedule:  not scheduled (run manually with "
                      f"'s3backup start {job}' or 's3backup schedule {job}')[/]")
        return

    if info["kind"] == "continuous":
        state_txt = "active" if info["loaded"] else "installed but not loaded"
        console.print(f"  [dim]Schedule:[/]  continuous backup ({state_txt})")
        return

    when = f"{info['hour']:02d}:{info['minute']:02d}"
    if info["loaded"]:
        delta = info["next_run"] - _time.time()
        console.print(
            f"  Schedule:  daily at {when} — next run {_fmt_until(delta)}"
        )
    else:
        console.print(
            f"  [yellow]Schedule:  daily at {when} configured but NOT loaded[/] "
            f"(run 's3backup schedule {job}')"
        )


def _progress_bar(percent: float, width: int = 28) -> str:
    """A simple text progress bar like ``[█████████·············]``."""
    pct = max(0.0, min(100.0, percent))
    filled = int(round(width * pct / 100.0))
    return "[cyan][" + "█" * filled + "·" * (width - filled) + "][/]"


def tier_table() -> Table:
    table = Table(title="S3 storage classes")
    table.add_column("Class", style="bold cyan")
    table.add_column("Description")
    for tier in tiers.TIERS:
        table.add_row(tier.name, tier.description)
    return table

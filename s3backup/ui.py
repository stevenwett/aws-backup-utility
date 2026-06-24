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


def render_status(st) -> None:
    """Render a one-shot snapshot of a job's run state."""
    from s3backup import state as state_mod
    from s3backup.scan import human_bytes

    if st is None:
        console.print("[dim]No run recorded for this job yet.[/]")
        return

    style = _PHASE_STYLE.get(st.phase, "white")
    running = st.is_active and state_mod.pid_alive(st.pid)
    live = " [dim](running)[/]" if running else ""
    console.print(f"[bold]{st.job}[/] — [{style}]{st.phase}[/]{live}")

    if st.phase in ("uploading", "retrying", "done") and st.total_files:
        bar_done = human_bytes(st.done_bytes)
        bar_total = human_bytes(st.total_bytes)
        console.print(
            f"  {bar_done} / {bar_total}  "
            f"([bold]{st.percent:.1f}%[/])   "
            f"{st.done_files:,} / {st.total_files:,} files"
        )
        line2 = f"  elapsed {_fmt_duration(st.elapsed)}"
        if st.phase == "uploading":
            line2 += f" · ETA ~{_fmt_duration(st.eta_seconds)}"
        if st.attempt:
            line2 += f" · attempt {st.attempt + 1}"
        console.print(line2)

    if st.message:
        console.print(f"  [dim]{st.message}[/]")
    if st.last_line and running:
        console.print(f"  [dim]last: {st.last_line}[/]")


def tier_table() -> Table:
    table = Table(title="S3 storage classes")
    table.add_column("Class", style="bold cyan")
    table.add_column("Description")
    for tier in tiers.TIERS:
        table.add_row(tier.name, tier.description)
    return table

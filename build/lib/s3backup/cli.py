"""Command-line entry point for s3backup.

Run with no subcommand to launch the interactive TUI; use subcommands for
scriptable, non-interactive operation.
"""

import argparse
import sys

from s3backup import __version__, aws, tiers, ui
from s3backup.config import (
    ConfigError,
    config_path,
    load_config,
    validate_job_paths,
)


def _load():
    """Load config, printing a friendly error and exiting on failure."""
    try:
        return load_config()
    except ConfigError as exc:
        ui.console.print(f"[red]Config error:[/] {exc}")
        raise SystemExit(2)


def cmd_sync(args) -> int:
    config = _load()
    try:
        job = config.job(args.job)
        validate_job_paths(job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2

    if args.storage_class and not tiers.is_valid(args.storage_class):
        ui.console.print(
            f"[red]Unknown storage class '{args.storage_class}'.[/] "
            f"Valid: {', '.join(tiers.TIER_NAMES)}"
        )
        return 2

    delete = True if args.delete else None  # None => fall back to job config
    try:
        return ui.execute_job(
            config,
            job,
            dry_run_only=args.dry_run,
            delete=delete,
            storage_class=args.storage_class,
            assume_yes=args.yes,
            confirm=_cli_confirm,
        )
    except aws.AwsError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 1


def _cli_confirm(summary, plan) -> bool:
    """Non-interactive default: require --yes. Without a TTY, refuse."""
    if not sys.stdin.isatty():
        ui.console.print(
            "[yellow]Refusing to apply changes without --yes in a "
            "non-interactive shell.[/]"
        )
        return False
    answer = input(
        f"Apply these {summary.total} change(s) to {plan.destination}? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")


def cmd_list(args) -> int:
    config = _load()
    ui.render_jobs(config)
    return 0


def cmd_check(args) -> int:
    config = _load()
    try:
        job = config.job(args.job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2

    ok = True
    # Local path
    try:
        validate_job_paths(job)
        ui.console.print(f"[green]✓[/] Local path exists: {job.resolved_local_path()}")
    except ConfigError as exc:
        ui.console.print(f"[red]✗[/] {exc}")
        ok = False

    # Credentials
    try:
        arn = aws.check_credentials(config.settings.aws_profile)
        ui.console.print(f"[green]✓[/] AWS credentials OK: {arn}")
    except aws.AwsError as exc:
        ui.console.print(f"[red]✗[/] {exc}")
        return 1

    # Bucket
    if aws.bucket_exists(job.bucket, config.settings.aws_profile):
        ui.console.print(f"[green]✓[/] Bucket reachable: s3://{job.bucket}")
    else:
        ui.console.print(
            f"[red]✗[/] Bucket not reachable (missing or no access): "
            f"s3://{job.bucket}"
        )
        ok = False

    return 0 if ok else 1


def cmd_config(args) -> int:
    ui.console.print(str(config_path()))
    return 0


def cmd_tiers(args) -> int:
    ui.console.print(ui.tier_table())
    return 0


def cmd_add(args) -> int:
    from s3backup import tui

    return tui.add_job_flow()


def cmd_edit(args) -> int:
    from s3backup import tui

    return tui.edit_job_flow(args.job)


def cmd_start(args) -> int:
    """Start a continuous background backup that survives sleep/reboot."""
    from s3backup import daemon

    config = _load()
    try:
        job = config.job(args.job)
        validate_job_paths(job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2

    daemon.write_plist(job.name, scheduled=False)
    try:
        daemon.load(job.name)
    except RuntimeError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 1
    ui.console.print(
        f"[green]Started background backup for '{job.name}'.[/]\n"
        f"It runs until complete and resumes after sleep or reboot.\n"
        f"Check progress any time with:  [bold]s3backup status {job.name}[/]\n"
        f"Stop it with:                  [bold]s3backup stop {job.name}[/]"
    )
    return 0


def cmd_stop(args) -> int:
    from s3backup import daemon

    was_loaded = daemon.unload(args.job)
    daemon.remove(args.job)
    if was_loaded:
        ui.console.print(f"[yellow]Stopped background backup for '{args.job}'.[/]")
    else:
        ui.console.print(f"[dim]No running background backup for '{args.job}'.[/]")
    return 0


def cmd_status(args) -> int:
    from s3backup import state as state_mod

    config = _load()
    jobs = [args.job] if args.job else list(config.jobs)

    if args.watch:
        return _watch_status(jobs)

    for i, name in enumerate(jobs):
        if i:
            ui.console.print()
        ui.render_status(state_mod.read_state(name))
    return 0


def _watch_status(jobs) -> int:
    """Live, auto-refreshing status. Ctrl-C exits the view, not the backup."""
    import time

    from rich.live import Live
    from rich.console import Group

    from s3backup import state as state_mod

    def render():
        from io import StringIO
        from rich.console import Console as _C

        buf = StringIO()
        tmp = _C(file=buf, force_terminal=True)
        # Reuse render_status by temporarily swapping the module console.
        original = ui.console
        ui.console = tmp
        try:
            for i, name in enumerate(jobs):
                if i:
                    tmp.print()
                ui.render_status(state_mod.read_state(name))
            tmp.print("\n[dim]watching — Ctrl-C to exit (backup keeps running)[/]")
        finally:
            ui.console = original
        return buf.getvalue()

    try:
        with Live(refresh_per_second=2, screen=False) as live:
            while True:
                from rich.text import Text

                live.update(Text.from_ansi(render()))
                time.sleep(0.5)
    except KeyboardInterrupt:
        ui.console.print("[dim]Stopped watching. The backup continues in the background.[/]")
    return 0


def cmd_schedule(args) -> int:
    """Install a daily background backup (cheap: skips when nothing changed)."""
    from s3backup import daemon

    config = _load()
    try:
        job = config.job(args.job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2

    daemon.write_plist(job.name, scheduled=True, hour=args.hour, minute=args.minute)
    try:
        daemon.load(job.name)
    except RuntimeError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 1
    ui.console.print(
        f"[green]Scheduled daily backup for '{job.name}'[/] at "
        f"{args.hour:02d}:{args.minute:02d}.\n"
        f"Days with no local changes make no S3 calls. "
        f"Remove with:  [bold]s3backup unschedule {job.name}[/]"
    )
    return 0


def cmd_unschedule(args) -> int:
    from s3backup import daemon

    was = daemon.unload(args.job)
    daemon.remove(args.job)
    ui.console.print(
        f"[yellow]Removed schedule for '{args.job}'.[/]" if was
        else f"[dim]No schedule found for '{args.job}'.[/]"
    )
    return 0


def cmd_run_internal(args) -> int:
    """Hidden entry point invoked by the launchd agent. Runs the backup."""
    from s3backup import runner

    config = _load()
    try:
        job = config.job(args.job)
        validate_job_paths(job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2
    try:
        code = runner.run_backup(config, job, force=args.force)
    except aws.AwsError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 1

    # A continuous (non-scheduled) agent uses KeepAlive to retry until success.
    # Once it succeeds, it has finished its purpose — tear itself down so it
    # doesn't loop forever. Scheduled agents stay installed for the next day.
    if code == 0 and not args.scheduled:
        from s3backup import daemon

        daemon.remove(job.name)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s3backup",
        description="Keep a local folder in sync with an S3 bucket, with "
        "storage-tier control. Run with no command for the interactive menu.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="Sync a named job (local → S3).")
    p_sync.add_argument("job", help="Name of the job to sync.")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Preview changes only; make no changes.")
    p_sync.add_argument("--delete", action="store_true",
                        help="Propagate local deletions to the bucket.")
    p_sync.add_argument("--storage-class", metavar="CLASS",
                        help="Override the storage class for this run.")
    p_sync.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="List configured jobs.")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser("check", help="Verify creds, bucket, and local path.")
    p_check.add_argument("job", help="Name of the job to check.")
    p_check.set_defaults(func=cmd_check)

    p_add = sub.add_parser("add", help="Add a new job interactively.")
    p_add.set_defaults(func=cmd_add)

    p_edit = sub.add_parser("edit", help="Edit an existing job interactively.")
    p_edit.add_argument("job", help="Name of the job to edit.")
    p_edit.set_defaults(func=cmd_edit)

    p_start = sub.add_parser(
        "start",
        help="Start a continuous background backup (survives sleep/reboot).",
    )
    p_start.add_argument("job", help="Name of the job to back up.")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop a continuous background backup.")
    p_stop.add_argument("job", help="Name of the job to stop.")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="Show backup progress for a job.")
    p_status.add_argument("job", nargs="?", help="Job name (default: all jobs).")
    p_status.add_argument("-w", "--watch", action="store_true",
                          help="Live, auto-refreshing view (Ctrl-C to exit).")
    p_status.set_defaults(func=cmd_status)

    p_sched = sub.add_parser("schedule",
                             help="Install a daily background backup for a job.")
    p_sched.add_argument("job", help="Name of the job to schedule.")
    p_sched.add_argument("--hour", type=int, default=3, help="Hour (0-23). Default 3.")
    p_sched.add_argument("--minute", type=int, default=0, help="Minute (0-59). Default 0.")
    p_sched.set_defaults(func=cmd_schedule)

    p_unsched = sub.add_parser("unschedule", help="Remove a job's daily schedule.")
    p_unsched.add_argument("job", help="Name of the job to unschedule.")
    p_unsched.set_defaults(func=cmd_unschedule)

    # Hidden internal command invoked by the launchd agent.
    p_run = sub.add_parser("_run")
    p_run.add_argument("job")
    p_run.add_argument("--scheduled", action="store_true")
    p_run.add_argument("--force", action="store_true")
    p_run.set_defaults(func=cmd_run_internal)

    p_tiers = sub.add_parser("tiers", help="Show available S3 storage classes.")
    p_tiers.set_defaults(func=cmd_tiers)

    p_config = sub.add_parser("config", help="Config-related helpers.")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_path = config_sub.add_parser("path", help="Print config file path.")
    p_config_path.set_defaults(func=cmd_config)
    p_config.set_defaults(func=lambda a: cmd_config(a))

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        from s3backup import tui

        return tui.main_menu()

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

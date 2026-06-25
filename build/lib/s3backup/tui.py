"""Interactive arrow-key menus on top of the config + sync engine."""

import os
from typing import Optional

import questionary

from s3backup import aws, tiers, ui
from s3backup.config import (
    Config,
    ConfigError,
    Job,
    load_config,
    save_config,
    validate_job_paths,
)


def _load_or_report() -> Optional[Config]:
    try:
        return load_config()
    except ConfigError as exc:
        ui.console.print(f"[red]Config error:[/] {exc}")
        return None


def _tier_choices(current: str):
    return [
        questionary.Choice(
            title=f"{t.name}  —  {t.description}",
            value=t.name,
            checked=(t.name == current),
        )
        for t in tiers.TIERS
    ]


def _confirm_changes(summary, plan) -> bool:
    if plan.delete:
        ui.console.print(
            "[red bold]Delete mode is ON[/] — files removed locally will be "
            "deleted from the bucket."
        )
    return bool(
        questionary.confirm(
            f"Apply {summary.total} change(s) to {plan.destination}?",
            default=False,
        ).ask()
    )


def run_job_flow(config: Config, job: Job) -> int:
    """Interactive: confirm tier + delete, dry-run, confirm, sync."""
    try:
        validate_job_paths(job)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2

    current_tier = job.effective_storage_class(config.settings)
    tier = questionary.select(
        "Storage tier for this run:",
        choices=_tier_choices(current_tier),
        default=current_tier,
    ).ask()
    if tier is None:
        return 1

    delete = questionary.confirm(
        "Propagate local deletions to the bucket (--delete)?",
        default=job.delete,
    ).ask()
    if delete is None:
        return 1

    try:
        return ui.execute_job(
            config,
            job,
            delete=delete,
            storage_class=tier,
            confirm=_confirm_changes,
        )
    except aws.AwsError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 1


def main_menu() -> int:
    config = _load_or_report()
    if config is None:
        return 2

    while True:
        choices = [questionary.Choice(f"Sync: {name}", value=("sync", name))
                   for name in config.jobs]
        choices += [
            questionary.Choice("➕ Add a new job", value=("add", None)),
            questionary.Choice("📋 List jobs", value=("list", None)),
            questionary.Choice("Quit", value=("quit", None)),
        ]
        action = questionary.select("What would you like to do?", choices=choices).ask()
        if action is None or action[0] == "quit":
            return 0
        kind, name = action
        if kind == "sync":
            run_job_flow(config, config.job(name))
        elif kind == "add":
            add_job_flow()
            config = _load_or_report() or config
        elif kind == "list":
            ui.render_jobs(config)


def _prompt_job_fields(defaults: Optional[Job] = None) -> Optional[dict]:
    d = defaults
    name = questionary.text(
        "Job name:", default=(d.name if d else "")
    ).ask()
    if not name:
        return None
    local_path = questionary.path(
        "Local folder to back up:",
        default=(d.local_path if d else os.path.expanduser("~/")),
        only_directories=True,
    ).ask()
    if not local_path:
        return None
    bucket = questionary.text("S3 bucket name:", default=(d.bucket if d else "")).ask()
    if not bucket:
        return None
    prefix = questionary.text(
        "Prefix (folder inside the bucket, optional):",
        default=(d.prefix if d else ""),
    ).ask()
    tier = questionary.select(
        "Default storage tier for this job:",
        choices=_tier_choices(d.storage_class if d and d.storage_class else tiers.DEFAULT_TIER),
    ).ask()
    if tier is None:
        return None
    delete = questionary.confirm(
        "Propagate local deletions by default?",
        default=(d.delete if d else False),
    ).ask()
    exclude_raw = questionary.text(
        "Exclude patterns (comma-separated, optional):",
        default=(", ".join(d.exclude) if d and d.exclude else ""),
    ).ask()
    exclude = [p.strip() for p in (exclude_raw or "").split(",") if p.strip()]
    return {
        "name": name.strip(),
        "local_path": local_path.strip(),
        "bucket": bucket.strip(),
        "prefix": prefix.strip() if prefix else "",
        "storage_class": tier,
        "delete": bool(delete),
        "exclude": exclude,
    }


def add_job_flow() -> int:
    config = _load_or_report()
    if config is None:
        return 2
    fields = _prompt_job_fields()
    if fields is None:
        ui.console.print("[yellow]Cancelled.[/]")
        return 1
    if fields["name"] in config.jobs:
        if not questionary.confirm(
            f"Job '{fields['name']}' exists. Overwrite?", default=False
        ).ask():
            return 1
    config.jobs[fields["name"]] = Job(**fields)
    path = save_config(config)
    ui.console.print(f"[green]Saved job '{fields['name']}'[/] to {path}")
    return 0


def edit_job_flow(name: str) -> int:
    config = _load_or_report()
    if config is None:
        return 2
    try:
        existing = config.job(name)
    except ConfigError as exc:
        ui.console.print(f"[red]{exc}[/]")
        return 2
    fields = _prompt_job_fields(existing)
    if fields is None:
        ui.console.print("[yellow]Cancelled.[/]")
        return 1
    # If renamed, drop the old entry.
    if fields["name"] != name:
        config.jobs.pop(name, None)
    config.jobs[fields["name"]] = Job(**fields)
    path = save_config(config)
    ui.console.print(f"[green]Saved job '{fields['name']}'[/] to {path}")
    return 0

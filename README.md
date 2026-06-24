# s3backup

A terminal utility for macOS that wraps the AWS CLI to keep a local folder in
sync with an S3 bucket, with control over the storage tier. One-way backup
(local → S3), driven by named jobs in a config file. Use it interactively with
arrow-key menus, or non-interactively for scripts and cron.

It shells out to `aws s3 sync` under the hood, so you get its battle-tested
delta syncing for free.

## Requirements

- macOS (or any Unix), Python 3.9+
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  installed and on your `PATH`, with credentials configured (`aws configure`)

## Install

```sh
# Recommended: isolated install via pipx
pipx install .

# Or in a virtualenv for development
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

This installs an `s3backup` command.

## Configuration

Config lives at `~/.config/s3backup/config.toml` (override with the
`S3BACKUP_CONFIG` environment variable). A commented template is created on
first run. Find the path with:

```sh
s3backup config path
```

Example config:

```toml
[settings]
default_storage_class = "STANDARD_IA"
# aws_profile = "default"          # optional, uses this AWS named profile

[jobs.documents]
local_path = "~/Documents"
bucket = "my-backup-bucket"
prefix = "documents"               # -> s3://my-backup-bucket/documents
storage_class = "DEEP_ARCHIVE"     # optional; overrides default_storage_class
delete = false                     # propagate local deletions to the bucket
exclude = ["*.tmp"]                # optional extra glob patterns

[jobs.photos]
local_path = "~/Pictures/Library"
bucket = "my-backup-bucket"
prefix = "photos"
```

## Usage

### Interactive

```sh
s3backup
```

Launches a menu: pick a job, confirm/override the storage tier, choose whether
to propagate deletions, review a **dry-run preview**, then confirm to run the
real sync.

### Non-interactive (scripts / cron)

```sh
s3backup list                      # show configured jobs
s3backup check documents           # verify creds, bucket access, local path
s3backup sync documents --dry-run  # preview changes, make none
s3backup sync documents            # sync (prompts to confirm)
s3backup sync documents --yes      # sync without confirmation (for cron)
s3backup sync documents --delete --storage-class GLACIER --yes
s3backup tiers                     # list available storage classes
s3backup add                       # add a job interactively
s3backup edit documents            # edit a job interactively
```

`--yes` is required to apply changes in a non-interactive shell (no TTY); this
prevents an unattended run from making unexpected changes.

## Storage classes

`STANDARD`, `STANDARD_IA`, `ONEZONE_IA`, `INTELLIGENT_TIERING`, `GLACIER_IR`,
`GLACIER`, `DEEP_ARCHIVE`. Run `s3backup tiers` for descriptions.

## Safety notes

- **Backups are one-way (local → S3).** This tool never writes to your local
  folder.
- **Deletions are off by default.** Remote files are kept even if deleted
  locally. Enable mirroring per run with `--delete` (or per job via
  `delete = true`). A dry-run preview always runs first so you see what
  `--delete` would remove before confirming.
- **macOS metadata is excluded automatically.** Every job skips `.DS_Store`,
  `.Spotlight-V100`, `.fseventsd`, `.Trashes`, `.TemporaryItems`, and
  `.DocumentRevisions-V100` — useful when backing up a mounted volume under
  `/Volumes`. Add your own patterns via a job's `exclude` list.

## Scheduling (optional)

The scriptable form is cron/launchd-ready. For example, a daily backup via
`launchd` would run:

```sh
s3backup sync documents --yes
```

## Development

```sh
.venv/bin/pytest        # run the test suite (no AWS calls)
```

# Backup & Restore Guide

Part of M4.9 — Production Readiness & Release Engineering.

## Scopes

Backups cover six independently-fetched scopes
(`services/integration/production/release_models.py::BackupScope`):

| Scope               | Typical source                                   |
|----------------------|---------------------------------------------------|
| `database`           | Primary Postgres database                          |
| `artifacts`          | Generated exports/reports on disk                  |
| `workflow_layouts`   | Designer-managed layouts (M4.8)                    |
| `plugin_registry`    | Installed plugin manifests (M4.7)                  |
| `configuration`      | The active `DeploymentProfile`                     |
| `audit_logs`         | Security/audit event history                       |

A backup is only as complete as the fetchers supplied to it — a scope
with no fetcher wired is recorded (not silently dropped) in
`BackupRecord.notes` as "no fetcher supplied," and the backup's
`status` becomes `partial` if it was the only scope requested.

## Taking a backup

```bash
python -m services.integration.production.production_cli production backup \
  --scopes database,configuration,workflow_layouts \
  --type full \
  --dest /var/lib/aasc/backups
```

Or via the API: `POST /production/backup`.

### Full vs. incremental

`--type incremental` requires a `baseline` `BackupRecord` (passed
programmatically via `backup_manager.create_backup(..., baseline=...)`
— the CLI/API always take `full` backups today; wiring a baseline
picker into the CLI is deferred work, see the M4.9 handover doc).
Incremental backups skip re-fetching any scope the baseline already
covers.

## Verifying a backup

Every `BackupRecord` carries a SHA-256 `checksum` over its JSON
payload. `backup_manager.verify_backup(record)` recomputes it from the
file on disk — run this before trusting any backup as a restore
target, especially one that's been copied between hosts.

## Restoring

**Restores never overwrite data without confirmation.** The flow is
always: dry run → review → confirmed apply.

```bash
python -m services.integration.production.production_cli production restore <backup.json> --mode dry_run
python -m services.integration.production.production_cli production restore <backup.json> --mode full --yes
```

`--mode selective` restores only the scopes you pass via
`restore_manager.plan_restore(..., scopes=[...])` (not yet exposed as
a CLI flag — see the M4.9 handover doc's deferred-work list).

## Retention

This package does not implement automatic retention/pruning of old
backups — that's an operator responsibility (a cron job removing
`BackupRecord.location` files and their DB rows past your retention
window) and is called out as deferred work in the handover doc.

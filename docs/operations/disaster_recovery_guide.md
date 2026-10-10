# Disaster Recovery Guide

Part of M4.9 — Production Readiness & Release Engineering.

## Scope

Covers full platform recovery in the event of database loss, plugin
registry corruption, or a bad deployment that can't be rolled forward.
For routine backup/restore mechanics see the Backup & Restore Guide;
this document covers the end-to-end incident procedure.

## Recovery objectives

Define your own RPO/RTO targets before an incident, not during one.
As a starting point:

- **RPO** (acceptable data loss): the interval between `production
  backup` runs — schedule these accordingly (hourly `incremental`,
  daily `full`, as a starting cadence).
- **RTO** (acceptable downtime): dominated by restore time for the
  `database` scope plus `alembic upgrade head`, not by the other five
  scopes, which are comparatively small.

## Procedure

1. **Stop write traffic** to the affected deployment (drain the
   Service/Ingress, or scale the Deployment to zero).
2. **Identify the most recent good backup**:
   ```bash
   python -m services.integration.production.production_cli production status --environment production
   ```
   or `GET /production/backups` for the full list with checksums.
3. **Dry-run the restore first, always**:
   ```bash
   python -m services.integration.production.production_cli production restore <backup.json> --mode dry_run
   ```
   Review `validation_issues` — a failed checksum or missing scope
   means this backup is not a safe restore target; try the next one
   back.
4. **Apply the restore** only after the dry run is clean:
   ```bash
   python -m services.integration.production.production_cli production restore <backup.json> --mode full --yes
   ```
   `--yes` is required — restores never overwrite data without
   explicit confirmation.
5. **Run migrations** (`alembic upgrade head`) if the restored backup
   predates the current schema.
6. **Re-run environment checks** before resuming traffic:
   ```bash
   python -m services.integration.production.production_cli production check --environment production
   ```
7. **Resume traffic** and monitor the Live Operations Dashboard's
   Production Readiness card closely for the following hour.

## Postmortem

Record: what triggered the incident, which backup was restored, the
actual RPO/RTO achieved, and any gaps discovered in monitoring or
alerting that delayed detection.

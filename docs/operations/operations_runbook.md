# Operations Runbook

Part of M4.9 — Production Readiness & Release Engineering.

## Daily

- Review the Live Operations Dashboard's Production Readiness card —
  confirm the latest environment check is `pass`, not `fail` (a
  `SKIPPED`-heavy report is normal in development; it should not be in
  production — if core checks show `SKIPPED` in production, that means
  connectivity probes aren't wired, which is itself worth fixing).
- Confirm a backup was taken within the last 24h
  (`GET /production/backups`, most recent entry).

## Weekly

- Run `production validate` over `deploy/` after any manifest/Helm
  values change, even one that looks purely cosmetic.
- Review `ConfigValidationResult.issues` for the running environment's
  actual loaded config (not just the profile default) — catch drift
  early via the Configuration Drift dashboard widget.
- Spot-check a recent backup's checksum (`backup_manager.
  verify_backup`) — corruption is best caught before it's needed.

## Per release

Follow the Upgrade Guide's generated `upgrade_checklist` in full,
every release, even ones that feel routine — the checklist's first
four steps (backup, migrate, check, validate) are cheap relative to
the cost of skipping one during an incident.

## Per incident

Follow the Disaster Recovery Guide. Do not skip the dry-run restore
step even under time pressure — a bad restore is strictly worse than
a slow one.

## Ownership

This runbook assumes an on-call rotation with access to:
production database credentials (via the configured secrets backend,
never plaintext), the Kubernetes cluster / Docker hosts, and the
`production_cli.py`/`/production/*` API. Document who holds each of
these in your own team's on-call handoff notes — this repository does
not track that.

## Related documents

Production Deployment Guide · Disaster Recovery Guide · Backup &
Restore Guide · Upgrade Guide · Rollback Guide · Monitoring Guide ·
Security Checklist · Capacity Planning Guide · Troubleshooting Runbook

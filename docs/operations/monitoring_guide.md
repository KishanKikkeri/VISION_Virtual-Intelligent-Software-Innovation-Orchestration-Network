# Monitoring Guide

Part of M4.9 — Production Readiness & Release Engineering.

## What M4.9 adds to monitoring

This milestone does not add a new metrics pipeline — it reuses the M3
Monitoring service's existing `/metrics` endpoint (wired to Prometheus
via `deploy/k8s/servicemonitor.yaml`) and extends the Live Operations
Dashboard (M4.3) with a **Production Readiness** card
(`ProductionSummary` in `dashboard_models.py`), surfacing:

- Environment Status — latest `EnvironmentReport.overall_status`
- Deployment Health — latest `DeploymentValidationResult.valid`
- Backup Status — latest `BackupRecord` timestamp/count
- Release Version — latest `Release.version`
- Migration Status — from the `migrations` environment check
- Production Checklist — `ProductionChecklist.completion_ratio`
- Storage Usage — from the `disk_space` environment check
- Configuration Drift — `ConfigurationDrift.has_drift`

## Wiring it up

The dashboard card is populated by `production_repository.
fetch_production_dashboard_section`, called from
`dashboard_builder.build_production_summary`. If no database is
configured, this returns a stub with all fields `None`/`0` rather than
raising — the rest of the dashboard renders normally either way.

## Alerting recommendations

- Alert on `EnvironmentReport.overall_status == "fail"` for the
  production environment — a `SKIPPED` check is not an alert
  condition, only `FAIL` is.
- Alert if no `BackupRecord` has been created within your RPO window
  (see the Backup & Restore Guide).
- Alert on `ConfigValidationResult.valid == False` for any config
  actually loaded into a running production environment — that
  indicates configuration drift from a validated baseline.

## Dashboards

Import the existing M4.3 Live Operations Dashboard; the Production
Readiness card appears automatically once
`production_repository`-backed data exists — no separate dashboard
deployment is required.

"""
services/integration/production/
=================================
M4.9 — Production Readiness & Release Engineering. See
docs/M4.9_Production_Readiness_Handover.md for the full writeup.

Hardening/deployment/operations tooling layered on top of the now
feature-complete AASC platform. Nothing here is a new runtime concept —
this package configures, validates, backs up, and releases the
*existing* eleven services/ten workflows; it never touches LangGraph
execution, never rewrites a workflow, and degrades gracefully whenever
optional infrastructure (Docker, Kubernetes, NATS, Redis, a live
database) isn't present.

    release_models.py            Pure Pydantic shapes (Environment, DeploymentProfile,
                                 ConfigValidationResult, EnvironmentReport,
                                 DeploymentValidationResult, Release, BackupRecord,
                                 RestoreRecord, ProductionChecklist, ProductionStatus, ...).
    deployment_profiles.py        development/testing/staging/production profiles with
                                 layered base -> environment -> override merging.
    configuration_manager.py      load/merge/validate/export/compare/diff over YAML/JSON/ENV.
    environment_validator.py      Production-readiness checks (db/cache/messaging/fs/disk/
                                 permissions/plugins/workflows/migrations/secrets/TLS),
                                 every check gracefully degrading when unconfigured.
    deployment_validator.py       Structural validation of Docker Compose / Kubernetes
                                 manifests / Helm values / environment config.
    release_manager.py            Semantic versioning, artifact/dependency inventory, SBOM
                                 (reused from M4.6 when available), upgrade/rollback plans.
    backup_manager.py             Full/incremental backup across six named scopes.
    restore_manager.py            dry_run/full/selective restore with a confirm=True gate.
    production_repository.py      Repository-pattern DB persistence (releases /
                                 deployment_profiles / backup_records / restore_records /
                                 environment_checks) + fetch_production_dashboard_section.
    production_export.py          json/markdown/html export for any of this package's reports.
    production_cli.py             python production_cli.py production [check|validate|
                                 release|backup|restore|config|status|checklist]
"""

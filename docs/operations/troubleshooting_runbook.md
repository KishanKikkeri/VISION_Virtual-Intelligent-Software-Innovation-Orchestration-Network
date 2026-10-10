# Troubleshooting Runbook

Part of M4.9 — Production Readiness & Release Engineering.

## `production check` reports FAIL

| Check | Likely cause | First step |
|-------|--------------|-----------|
| `database_connectivity` | DB down, wrong `DATABASE_URL`, network policy blocking egress | verify connectivity from inside the pod/container; check `deploy/k8s/networkpolicy.yaml` egress rules |
| `cache_connectivity` | Redis down or wrong `REDIS_URL` | same as above for port 6379 |
| `messaging_connectivity` | NATS down or wrong `NATS_URL` | same as above for port 4222 |
| `filesystem_writable` / `permissions` | wrong container user, read-only volume mount | check `securityContext`/volume `readOnly` flags |
| `disk_space` (WARN) | approaching capacity | see Capacity Planning Guide |
| `plugin_integrity` | a plugin manifest failed M4.7's validator | inspect the specific plugin named in `detail` |
| `workflow_integrity` | a workflow failed validation | inspect the workflows named in `detail`; do not deploy until fixed or rolled back |
| `migrations` | DB not at Alembic head | run `alembic upgrade head` |
| `secrets` | a required secret is unset | check the Secret/ConfigMap actually mounted, not just the manifest |
| `tls_configuration` | cert/key path misconfigured or files missing | verify the mounted paths in `deploy/k8s/deployment.yaml`'s `volumeMounts` |

## `production validate` reports invalid assets

Read `DeploymentValidationIssue.message` — it names the exact asset
and missing field (`missing metadata.name`, `neither 'image' nor
'build'`, etc.). These are structural checks only; a structurally
valid manifest can still fail at `kubectl apply` time for
cluster-specific reasons (quota, admission webhooks) this validator
doesn't see.

## A backup's `status` is `partial`

Check `BackupRecord.notes` — it names exactly which scope(s) had no
fetcher wired or whose fetch raised. A `partial` backup is not
necessarily unusable, but do not treat it as covering scopes it
explicitly notes it skipped.

## `restore` is refused without `--yes`

This is intentional (§Critical Constraints: never overwrite data
without confirmation) — re-run with `--yes` only after reviewing the
dry-run output and `validation_issues`.

## `PyYAML is not installed` errors

`configuration_manager`/`deployment_validator`'s YAML paths require
PyYAML; install it (`pip install pyyaml`) — this is the only external
dependency this package's core logic assumes beyond Pydantic/
SQLAlchemy, and it degrades to an explicit error rather than crashing
with a bare `ImportError`.

## Dashboard's Production Readiness card is missing

`ProductionSummary` is `None` when `production_repository.
fetch_production_dashboard_section` returns `None` — either no
database is wired, or no releases/backups/checks have been recorded
yet. This is expected on a fresh deployment; it populates once
`production release`/`production backup`/`production check` have run
at least once against a configured database.

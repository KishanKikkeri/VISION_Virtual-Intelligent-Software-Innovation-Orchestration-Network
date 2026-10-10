# Capacity Planning Guide

Part of M4.9 — Production Readiness & Release Engineering.

## Baseline resource requests

From `deploy/helm/aasc/values.yaml` / `deploy/k8s/deployment.yaml`
defaults, per pod:

| Resource | Request | Limit |
|----------|---------|-------|
| CPU      | 500m    | 2     |
| Memory   | 512Mi   | 2Gi   |

`deployment_profiles.py`'s `production` override sets
`workflow.max_concurrent_executions: 100` (vs. `10` at base/
development) — size CPU/memory limits with that concurrency ceiling in
mind, not the base default.

## Scaling signals

The HPA (`deploy/k8s/hpa.yaml` / Helm's
`templates/hpa.yaml`) scales on CPU (70%) and memory (80%) utilization,
`minReplicas: 3` / `maxReplicas: 12`. Treat sustained scaling to
`maxReplicas` as a signal to raise the ceiling and re-evaluate node
pool capacity, not just let it plateau there.

## Storage

The `disk_space` environment check
(`environment_validator.check_disk_space`) warns below 1GB free by
default — treat that as a floor, not a target; plan actual capacity
around backup retention (see Backup & Restore Guide) plus workflow
artifact growth, both of which are unbounded without an operator-owned
retention policy (see that guide's "Retention" section — this is
explicitly deferred work, not automated by this package).

## Database

`deployment_profiles.py`'s `database.pool_size` scales from `2`
(development) to `20` (production) — this is a starting point, not a
sized recommendation; tune it against actual concurrent-connection
telemetry from the Live Operations Dashboard rather than the
environment default alone.

## Cache / messaging

No capacity guidance is generated automatically for Redis/NATS sizing
in this milestone — this is deferred work (see the M4.9 handover
doc); size these against your actual workflow throughput and the
existing M3 Monitoring service's throughput metrics.

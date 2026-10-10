# Production Deployment Guide

Part of M4.9 — Production Readiness & Release Engineering.

## Overview

The AASC platform ships four environment profiles — `development`,
`testing`, `staging`, `production` — defined in
`services/integration/production/deployment_profiles.py` and layered
base → environment → deployer overrides. Before any production
deployment, run the full readiness sweep:

```bash
python -m services.integration.production.production_cli production check --environment production
python -m services.integration.production.production_cli production validate deploy/k8s --environment production
```

Both must exit `0` before proceeding. `production check` degrades
gracefully for infrastructure you haven't wired a probe for (it
reports `SKIPPED`, not `FAIL`) — a `FAIL` always means something
configured is actually broken.

## Deployment paths

### Kubernetes / Helm (recommended for production)

```bash
helm upgrade --install aasc deploy/helm/aasc \
  -f values-production.yaml \
  --set image.tag=$(python -m services.integration.production.production_cli production config --environment production | grep version)
```

Apply plain manifests instead of Helm if your cluster doesn't run Helm:

```bash
kubectl apply -f deploy/k8s/rbac.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml   # created from secret.yaml.example — never commit real values
kubectl apply -f deploy/k8s/pvc.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/ingress.yaml
kubectl apply -f deploy/k8s/hpa.yaml
kubectl apply -f deploy/k8s/networkpolicy.yaml
kubectl apply -f deploy/k8s/servicemonitor.yaml   # requires the Prometheus Operator CRDs
```

### Docker Compose (single-host)

```bash
docker compose -f deploy/docker/docker-compose.prod.yml config   # validates required env vars are set
docker compose -f deploy/docker/docker-compose.prod.yml up -d
```

## Pre-flight checklist

Use `GET /production/checklist` or `production checklist` for the
synthesized view; the underlying gates are:

1. `production check` passes (or only reports `SKIPPED` for
   infrastructure genuinely not in scope for this deployment).
2. `production validate` passes over every asset in `deploy/`.
3. A backup has been taken (`production backup`) within your recovery
   point objective.
4. `alembic upgrade head` has been run against the target database.
5. TLS certificate/key are provisioned and referenced by
   `security.tls_cert_path`/`security.tls_key_path`.

## Rollout strategy

The Compose production file uses `order: start-first` with
`failure_action: rollback`; the Kubernetes Deployment relies on the
default `RollingUpdate` strategy. Neither performs a blue/green cutover
— see the Rollback Guide for the explicit rollback procedure if a
rollout needs to be reverted.

See also: Disaster Recovery Guide, Backup & Restore Guide, Upgrade
Guide, Rollback Guide.

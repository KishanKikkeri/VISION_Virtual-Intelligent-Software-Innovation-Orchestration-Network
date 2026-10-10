# PRODUCTION_READINESS.md

## 1. `:latest` image tag — Medium, confirmed at both the raw manifest and Helm chart level

- `deploy/k8s/deployment.yaml:29` — `image: aasc-platform:latest`
- `deploy/helm/aasc/values.yaml:12` — `tag: "latest"` (the Helm
  chart's own default, which the k8s manifest's hardcoded value above
  matches)

Using a mutable `:latest` tag in a production deployment manifest is a
well-known anti-pattern: a `kubectl rollout restart` (or any pod
reschedule) can silently pull a different image than what was
originally deployed, breaking reproducibility and making rollback
(`kubectl rollout undo`) unreliable, since the "previous" ReplicaSet's
pod template still just says `:latest` — there's no way to know which
actual image content it referred to. **Severity: Medium.** **Fix:**
default to a real version tag (e.g. the `release/VERSION` file's
content, per `release_validation`'s own versioning — `1.0.0-rc1` right
now) with `:latest` only as a local-dev override, not the shipped
default.

## 2. No deployment strategy explicitly configured

`deploy/k8s/deployment.yaml` has no `strategy:` block (confirmed:
`grep -n "strategy:" deployment.yaml` → zero matches), so Kubernetes'
implicit default (`RollingUpdate` with 25% max-surge / 25%
max-unavailable) applies. **Not a bug** — the default is a reasonable
choice — but for a "production readiness" bar, an *explicit* strategy
(even if it's the same values) is worth codifying so a future editor
doesn't have to know the implicit default to reason about rollout
behavior. **Severity: Low.**

## 3. No `terminationGracePeriodSeconds` / `preStop` hook

Confirmed absent from `deploy/k8s/deployment.yaml`. The container
relies on Kubernetes' default 30-second grace period and `uvicorn`'s
own SIGTERM handling (which this audit cannot verify further — the
process entrypoint's actual signal handling lives in `main.py`,
outside this slice, per `RUNTIME_ANALYSIS.md`/`DATABASE_REVIEW.md`'s
identical "outside this slice" caveat). **Severity: Low — flagged,
not scored,** since the default may be entirely adequate; there's no
evidence of a slow-shutdown operation (like a long-running request)
in this slice that would need more than 30 seconds.

## 4. Readiness/liveness probes and resource limits — present and reasonable

Re-confirmed from Phase 1: `deployment.yaml:45` (`readinessProbe`),
`:51` (`livenessProbe`), `:39-44` (`resources.requests`:
`cpu: "500m"`, `memory: "512Mi"`; `resources.limits`: `cpu: "2"`,
`memory: "2Gi"`) all present with sane-looking values. **No finding**
— cited as confirmed-good, not re-derived as new.

## 5. Monitoring / alerting — a real gap between "collected" and "acted on"

`deploy/k8s/servicemonitor.yaml` exists and wires a Prometheus
`ServiceMonitor` CR (confirmed: `kind: ServiceMonitor` at line 8) —
metrics are scraped. **But no `PrometheusRule` (or equivalent
alerting-rule resource) exists anywhere in this slice** (confirmed:
`find deploy -iname "*rule*"` → zero results). Metrics are collected
with nothing configured to alert on them — a real gap between
"observable" and "actionable." **Severity: Medium.** **Fix:** add a
`PrometheusRule` covering at minimum: error-rate, the `RUNTIME_
ANALYSIS.md` R1/R2-adjacent memory-growth and event-loop-block
symptoms, and pod restart count.

## 6. CI/CD — build/test/package only, no deploy or rollback automation

Re-confirmed `.github/workflows/ci.yml`'s 4 jobs (`unit-tests`,
`benchmark`, `release-validation`, `docs-generation`, `packaging`) —
`grep -n "deploy\|kubectl\|helm upgrade" .github/workflows/ci.yml` →
**zero matches.** There is no automated deployment step and therefore
no automated rollback step — consistent with (not contradicting)
`docs/M4.10_Final_Release_Handover.md`'s own statement that tagging
and deployment are "an operator action outside this milestone's
scope." **Not a new finding**, but worth stating plainly for a
"production readiness" review specifically: **there is currently no
tested path from a merged commit to a running deployment**, automated
or documented step-by-step. **Severity: Medium** for an "enterprise
release" bar specifically (the audit's own framing) — acceptable for
where this project appears to be in its lifecycle otherwise.

## 7. Backup validation — genuinely real, not just documented

`backup_manager.verify_backup()` (`backup_manager.py:123-130`) was
checked directly, not just cited: it recomputes a checksum from the
on-disk backup payload and compares against the recorded checksum,
returning `False` (not raising) on any read failure or mismatch — a
real, working corruption/tamper check, confirmed by reading the full
function body. **No finding — confirmed-good.** Contrasted with
`RELIABILITY_REVIEW.md` REL4's finding that the equivalent
`verify_restore()` does not exist — backups are verified, restores
are not.

## 8. Configuration management / environment variables

`deploy/docker/Dockerfile`'s own header comment states config is
"entirely environment-variable driven per `deployment_profiles.py`" —
verified against `production/deployment_profiles.py` directly: it
defines per-environment (`development`/`staging`/`production`) profile
dicts keyed by env-var names, no hardcoded values. **No finding** —
matches its own documentation.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| PROD1 | `:latest` image tag as the shipped default, both raw manifest and Helm chart | `deploy/k8s/deployment.yaml:29`, `deploy/helm/aasc/values.yaml:12` | Medium | Default to `release/VERSION`'s value |
| PROD2 | No explicit `strategy:` block | `deploy/k8s/deployment.yaml` | Low | Codify the intended values explicitly |
| PROD3 | No `terminationGracePeriodSeconds`/`preStop` hook | `deploy/k8s/deployment.yaml` | Low — flagged, not scored | Confirm 30s default is adequate; add if not |
| PROD4 | Metrics scraped (`ServiceMonitor` present) but no alerting rules exist | `deploy/k8s/servicemonitor.yaml` present; no `PrometheusRule` anywhere | Medium | Add a `PrometheusRule` |
| PROD5 | No automated deploy or rollback path in CI | `.github/workflows/ci.yml` | Medium | Add a deploy job (or document the manual process explicitly) |

# Security Checklist

Part of M4.9 — Production Readiness & Release Engineering.

This checklist is additive to, and reuses, M4.6's Security Hardening
work — it does not reimplement scanning or secrets management, only
verifies it's actually configured for the environment being deployed.

## Before every production deployment

- [ ] `security.tls_required == True` in the resolved
      `DeploymentProfile` for `production` (the base default is
      `False`; the `production` environment override sets it `True` —
      confirm no caller override has flipped it back).
- [ ] TLS certificate and key files exist at the configured paths
      (`environment_validator.check_tls` — `FAIL` if `tls_required` and
      either file is missing).
- [ ] `security.secrets_backend` is not `env` in production (the base
      default; production's override sets `vault` — verify this wasn't
      overridden back to plaintext env vars).
- [ ] All `required_secret_names` passed to `environment_validator.
      check_secrets` are present and non-empty.
- [ ] `configuration_manager.validate(config, "production")` reports
      `valid=True` — in particular, no `conflicting` issues for
      `security.tls_required` or `database.echo`.
- [ ] The Kubernetes `Secret` in `deploy/k8s/secret.yaml` (created from
      `secret.yaml.example`) has never been committed with real values.
- [ ] `deploy/k8s/networkpolicy.yaml` is applied — default-deny with
      explicit ingress/egress allows only.
- [ ] `deploy/k8s/rbac.yaml`'s `Role` still only grants
      `get`/`list`/`watch` on `configmaps`/`secrets`/`pods` — no
      cluster-admin, no write verbs, unless a specific feature has
      justified adding one (audit any addition here).
- [ ] `release.sbom.available == True` for this release, or the reason
      it's `False` is understood and accepted (SBOM generation not
      wired in this environment vs. SBOM generation actually failing
      are different risk profiles — check `sbom.note`).

## Ongoing

- Re-run `production check --environment production` after any
  configuration change, not just at deploy time.
- Treat any `ConfigValidationIssue` with `kind="deprecated"` as a
  scheduling item, not something to ignore indefinitely.

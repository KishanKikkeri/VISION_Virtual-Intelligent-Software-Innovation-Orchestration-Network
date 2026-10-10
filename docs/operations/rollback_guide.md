# Rollback Guide

Part of M4.9 — Production Readiness & Release Engineering.

## When to roll back

Prefer rolling back over "fixing forward" whenever the new release has
broken environment checks, a failing workflow smoke test, or a
migration that can't be safely left half-applied. Every `Release`
carries a generated `rollback_plan` (`release_manager.
build_rollback_plan`) — read it before you need it, not during the
incident.

## Generated rollback plan

For any release with a recorded `previous_version`:

1. Stop/drain traffic to the upgraded deployment.
2. Restore the pre-upgrade backup taken before the upgrade.
3. Run `alembic downgrade` back toward the previous version.
4. Redeploy the previous version's artifacts.
5. Re-run environment checks and smoke tests.

If a release has no `previous_version` recorded, its rollback plan is
a single `no_prior_release` step — there's nothing to roll back to;
treat that release as your new floor and fix forward instead.

## Kubernetes / Helm rollback

```bash
helm rollback aasc <REVISION>
# or, without Helm history:
kubectl rollout undo deployment/aasc-platform
```

Neither of these reverts a database migration — run `alembic
downgrade` separately per the generated rollback plan, in the correct
order (drain traffic → downgrade migrations → redeploy old artifacts,
not the reverse).

## Docker Compose rollback

The production Compose file sets `update_config.failure_action:
rollback`, so a failed rolling update auto-reverts at the container
level. A rollback that also needs a database downgrade still requires
the manual `alembic downgrade` + restore steps above — Compose alone
cannot undo a schema migration.

## Verification

After any rollback, re-run `production check` and `production
checklist` against the now-restored environment before resuming
traffic — a rollback that "succeeds" at the deployment level but
leaves environment checks failing is not actually complete.

# Upgrade Guide

Part of M4.9 — Production Readiness & Release Engineering.

## Building a release

```bash
python -m services.integration.production.production_cli production release 1.3.0 --previous 1.2.0
```

This assembles a `Release` (`release_manager.build_release`): semantic
version validation, an artifact inventory (every module under
`services/` plus every Alembic migration), a dependency inventory
(parsed from `requirements.txt`), an SBOM reference (populated when
M4.6's SBOM generator is available; otherwise `sbom.available=False`
with an explanatory note — never a hard failure), and a generated
upgrade checklist and rollback plan.

## Upgrade checklist (generated)

Every release's `upgrade_checklist` includes, in order:

1. *(major-version upgrades only)* Review breaking changes in the
   release notes.
2. Take a full backup.
3. Run `alembic upgrade head`.
4. Run `production check` against the target environment.
5. Run `production validate` over deployment assets.
6. Smoke-test the ten production workflows end-to-end.

Treat this as the floor, not the ceiling — add environment-specific
steps (cache warmup, feature-flag flips, etc.) as needed; the
generated checklist doesn't know about those.

## Version bumps

`release_manager.bump_version("1.2.3", part="minor")` → `"1.3.0"`.
Follows standard semver: `major` resets minor/patch to 0, `minor`
resets patch to 0, `patch` increments only the patch component.

## Breaking-change detection

`release_manager.is_breaking_upgrade(previous, next)` is `True` only
when the major version increases — this is a mechanical semver check,
not a static analysis of what actually changed; pair it with a human
review of the release notes for anything semver alone can't catch
(e.g. a config default flip that's compatible at the API level but
changes runtime behavior).

## After the upgrade

Confirm via `GET /production/status` that `latest_release_version`
matches what you just deployed, and via `production checklist` that
environment checks are passing on the new version before declaring the
upgrade complete.

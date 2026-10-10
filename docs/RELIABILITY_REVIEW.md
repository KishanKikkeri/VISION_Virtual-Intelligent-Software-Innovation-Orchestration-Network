# RELIABILITY_REVIEW.md

## 1. Resource leak: zip-bundle temp directories are never cleaned up — Medium-High, verified reachable

`plugin_loader.import_entrypoint()` (`plugin_loader.py:209-234`)
creates a fresh `tempfile.mkdtemp()` directory for every zip-bundle
plugin it imports, and only registers that path for later cleanup if
the caller supplies an `_extracted_dirs` dict argument. `cleanup_extracted()`
(`plugin_loader.py:236-243`) is the function that would remove it — but:

- **`cleanup_extracted` is never called from any application code
  path** — confirmed via `grep -rn "cleanup_extracted" services` →
  only appears in its own definition and in test code
  (`tests/foundation/test_m47_plugin_sdk.py:379,1302`).
- **The one function that repeatedly calls `import_entrypoint` from
  real request/CLI paths, `validate_entrypoint_importable`
  (`plugin_loader.py:247-259`), calls it with no `_extracted_dirs`
  argument at all** (`import_entrypoint(discovered)` at line 256) —
  so even if a caller wanted to track and later clean up the temp
  dir, the path is discarded immediately with no reference kept
  anywhere.
- **This is reachable from two real, unauthenticated (see
  `SECURITY_REVIEW.md` SEC2) entry points**: `plugin_cli.py:165` and,
  more concerning for a long-running server, **`api/plugins.py:221`
  inside `POST /platform/plugins/reload`** (confirmed by reading the
  handler directly, `api/plugins.py:215-224`).

**Concrete impact:** every `POST /platform/plugins/reload` call
against a zip-bundle-sourced plugin permanently leaks one temp
directory on disk. A long-running server subject to repeated reloads
(automated monitoring hitting this endpoint, or a caller retrying
after a transient error) accumulates orphaned directories under the
system temp path indefinitely — a real disk-exhaustion DoS vector on
a long-lived process, not just untidiness.

**Severity: Medium-High** (not Critical: exploiting it for meaningful
impact requires either sustained legitimate traffic or an attacker
with reload access, and each leaked directory is small — a
single-plugin zip's contents — so exhaustion takes volume). **Fix:**
either (a) have `validate_entrypoint_importable` accept and pass
through an `_extracted_dirs` dict and call `cleanup_extracted` in a
`finally` block immediately after the import attempt (validation
doesn't need to keep the extracted files around afterward), or (b)
extract to a directory nested under a single well-known temp root
that's cleared on process startup, so leaked directories don't survive
a restart even if never explicitly cleaned mid-process.

## 2. Retries / backoff / circuit breakers

**None exist at runtime anywhere in this slice** — cross-referenced
from `RUNTIME_ANALYSIS.md` (design-time `RETRY` node type only, no
execution engine to apply it). Every real I/O operation in this
codebase (DB queries, subprocess calls, the zip extraction above) is a
single attempt with, at most, a timeout — no retry-on-transient-failure
logic, no exponential backoff, no circuit breaker anywhere. **Severity:
Medium** — for a "production readiness" bar, this is a real gap:
`scripts/install/verify.py`'s TCP reachability checks
(`_tcp_check`, e.g. checking Postgres/Redis/NATS) will report a false
`WARN` on a single transient connection blip with no retry, and none
of the production repository's DB calls have any retry-on-deadlock or
retry-on-connection-drop wrapper.

## 3. Graceful degradation

**This is a genuine strength**, re-confirmed from Phase 1: every
external-module-not-available path (chaos/security/replay modules
absent, `db_factory` absent, `plugin_validator` absent) degrades to a
`SKIPPED`/in-memory-fallback result rather than crashing — this
pattern is applied consistently across all 4 API routers and the
`release_validation` package. Not re-itemized here since Phase 1
already catalogued it in detail; cited as the one reliability
dimension this codebase handles unusually well.

## 4. Timeout handling

Every `subprocess.run` call has an explicit `timeout=` — re-verified:
`final_qa.py:51` (`timeout=120`), `verify.py:69` (`timeout=5`),
`verify.py:83`'s `_tcp_check` (`timeout=1.0` default), `install.py:43`
(`timeout=120`). **No DB call anywhere has an explicit
query/statement timeout** — every `await db.execute(...)` relies on
whatever default the (not-included-in-this-slice) session/engine
configuration provides, which this audit cannot verify (see
`DATABASE_REVIEW.md` DB4's identical unverifiable-boundary note).

## 5. Cancellation handling

No `asyncio.CancelledError` handling anywhere
(`grep -rn "CancelledError" services` → zero matches). Given there are
no long-running background tasks to cancel (confirmed in
`RUNTIME_ANALYSIS.md`), this is consistent with there being nothing
that needs cancellation handling today — not flagged as a gap, since
there's no operation in this slice long enough to warrant it beyond
what the `subprocess.run` timeouts above already bound.

## 6. Cleanup logic (beyond the temp-dir finding above)

Checked every other resource-acquiring code path for matching cleanup.
DB sessions are all `async with db_factory() as db:`-scoped (context
manager, cleanup guaranteed by `__aexit__`) — confirmed across all 4
routers. File handles (`open(...)`) are all `with`-scoped — confirmed
via `grep -rn "open(" services | grep -v "with "` → zero matches
(every `open()` call in application code is inside a `with` block).
**The zip-temp-dir leak (finding #1) is the only cleanup gap found.**

## 7. Rollback correctness

Not independently verifiable beyond `DATABASE_REVIEW.md` DB4's
finding — since commit/rollback boundaries live in the
not-included `db_factory`, whether a partially-failed multi-step
repository operation (e.g., `save_release` followed by a later step
failing) actually rolls back cannot be confirmed from this slice.
Checked whether any repository method itself calls `db.rollback()` on
a caught exception — none do (every repository method either succeeds
or lets the exception propagate; none catches-and-continues after a
partial write). This is arguably correct (let the caller/session
context manager handle rollback) but, per finding DB4, cannot be
confirmed end-to-end.

## 8. Recovery guarantees / disaster recovery

`production/backup_manager.py` and `restore_manager.py` exist and
`backup_manager.py:123` has a `verify_backup()` function — confirmed
present. Checked `restore_manager.py` for an equivalent
post-restore verification step: **no `verify_restore`-equivalent
function exists** (`grep -n "^def " restore_manager.py` shows
`plan_restore`/`apply_restore`/`dry_run_restore`, no verification
step after execution). **Severity: Low-Medium** — a restore that
completes without error has no automated check that the restored
state is actually correct/complete, unlike backups which do get
verified. **Fix:** add a `verify_restore()` mirroring
`verify_backup()`'s approach.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| REL1 | Zip-bundle temp directories leak on every plugin validate/reload call | `plugin_loader.py:247-259` (esp. line 256), reachable via `api/plugins.py:215-224` and `plugin_cli.py:165` | Medium-High | Pass `_extracted_dirs` through and clean up in a `finally` block |
| REL2 | No retry/backoff/circuit-breaker anywhere at runtime | codebase-wide | Medium | Add retry-with-backoff around DB calls and TCP health checks at minimum |
| REL3 | No DB query/statement timeout configured (unverifiable — depends on external `db_factory`) | all `*_repository.py` `db.execute` calls | Low — flagged, not scored | Verify/set explicit statement timeout in the (external) engine config |
| REL4 | `restore_manager.py` has no post-restore verification step, unlike `backup_manager.verify_backup()` | `production/restore_manager.py` | Low-Medium | Add `verify_restore()` |

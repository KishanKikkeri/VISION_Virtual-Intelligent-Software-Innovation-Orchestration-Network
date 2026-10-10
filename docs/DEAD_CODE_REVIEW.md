# DEAD_CODE_REVIEW.md — Phase 2

## Re-verification of Phase 1's fixes

Re-ran `pyflakes services scripts benchmarks examples plugins` fresh
in this session: **3 findings remain, all previously identified and
deliberately not removed** —

```
production/environment_validator.py:114 — plugin_validator imported but unused
release_validation/final_qa.py:110      — plugin_validator imported but unused
release_validation/documentation_generator.py:158 — f-string missing placeholders
```

The first two are the importability-probe pattern Phase 1 correctly
identified and annotated (`# noqa: F401`) rather than deleted — `pyflakes`
doesn't honor inline `noqa` comments natively (that's a `flake8`
feature), so it still reports them, but they are not dead code; both
are load-bearing (removing either would silently disable the plugin
availability check its surrounding function exists to perform). The
third is cosmetic (an f-string with no `{}` interpolation), explicitly
left alone in Phase 1 as stylistic churn the audit brief prohibits
touching. **No new dead-import findings since Phase 1's fixes** —
confirms the 22 removals didn't miss anything and nothing added since
(the zip-slip fix, `requirements.txt`, or this audit's own reports)
introduced new dead imports.

## Verifying Phase 1's own fix isn't itself orphaned

Checked that `_safe_extractall`/`UnsafeZipMemberError`
(`plugin_loader.py:77-101`, added during Phase 1's security fix) are
actually referenced, not dead code introduced by the fix itself:
`grep -rn "UnsafeZipMemberError\|_safe_extractall" services tests` →
**17 references** across the implementation, its call site
(`plugin_loader.py:226`), and 4 dedicated regression tests. **Not
dead.**

## Unreachable branches / impossible conditions

Re-checked (fresh method from Phase 1, not re-run of the same check):
every `Enum` with a corresponding lookup dict was checked for
exhaustiveness. `CheckStatus` (4 members: `PASS`/`WARN`/`FAIL`/`SKIPPED`)
is consumed by `release_validator._STATUS_CREDIT` (a dict literal) —
confirmed all 4 members have an entry, so no `KeyError`-shaped dead
branch exists. Checked the equivalent for `Severity` (`INFO`/`WARNING`/
`ERROR`/`CRITICAL`) against `DependencyReport.clean`'s check
(`severity in (Severity.ERROR, Severity.CRITICAL)`) — this is a
2-of-4 membership test, not an exhaustive dict, so there's no
"missing key" failure mode to check; confirmed the two omitted values
(`INFO`/`WARNING`) are deliberately excluded (issues at those
severities shouldn't fail the "clean" check) rather than
accidentally-uncovered. **No unreachable branch found.**

## Duplicate implementations

Same finding as Phase 1, re-confirmed, not re-derived: the two
markdown-table-building functions
(`benchmark_summary.summary_markdown()`,
`final_qa.qa_report_markdown()`) remain small, near-duplicate, and
not worth merging for two call sites — unchanged assessment.

## Obsolete compatibility code / abandoned migrations / stale feature flags

- `grep -rn "feature_flag\|FEATURE_FLAG\|FeatureFlag" services` →
  **zero matches.** No feature-flag mechanism exists anywhere in this
  slice, so there is nothing to be stale.
- Checked all 3 Alembic migrations for a later migration that
  contradicts/abandons an earlier one (e.g., a column added then
  immediately dropped in a subsequent migration without ever being
  used) — none found; each migration only adds new tables, no
  migration alters or drops anything from an earlier one.
- No deprecated API version markers (`v1`/`v2`-shaped duplicate
  routers, `@deprecated` decorators) found anywhere.

## Unused classes/functions — re-checked candidates from Phase 1

Phase 1 flagged `dependency_checker.diff_against_lockfile()` as
tested-but-uncalled-by-any-CLI/API. Re-checked in this pass: still
uncalled from `release_cli.py`/`api/release.py`/`readiness_report.py`
(confirmed via `grep -rn "diff_against_lockfile" services` → only its
own definition and its test). **Recommendation unchanged: keep, not
dead** — it's exercised by tests and represents real, intentionally-
deferred functionality per Phase 1's reasoning, not abandoned code.

## Findings summary

**No new dead-code findings in Phase 2.** This category is clean,
confirmed via fresh re-verification rather than assumed from Phase 1.

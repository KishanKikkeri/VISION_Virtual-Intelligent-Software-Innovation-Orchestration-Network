# DEAD_CODE_REPORT.md

Tooling: `pyflakes services scripts benchmarks examples plugins`
(the only fully-reliable dead-import/dead-name detector available
without a running application to trace real call graphs — unused
*public* functions/classes that are part of a package's external API
surface can't be soundly detected by static analysis alone, since
`api/*.py` or an external caller may use them; those are noted
separately below as "candidates," not confirmed dead).

## Confirmed dead: unused imports (22 of 24 pyflakes findings; see correction below for the other 2)

| File | Import | Referenced elsewhere? | Removed |
|---|---|---|---|
| `workflow_designer/graph_builder.py` | `typing.Optional` | No | ✅ |
| `workflow_designer/canvas_state.py` | `copy` | No | ✅ |
| `plugin_sdk/plugin_validator.py` | `typing.Any` | No | ✅ |
| `plugin_sdk/plugin_cli.py` | `PluginSourceType` | No | ✅ |
| `plugin_sdk/plugin_cli.py` | `PluginNotFoundError` | No | ✅ |
| `api/release.py` | `typing.Optional` | No | ✅ |
| `api/release.py` | `ExportFormat` | No | ✅ |
| `api/designer.py` | `typing.List` | No | ✅ |
| `api/designer.py` | `build_graph` | No | ✅ |
| `api/production.py` | `ConfigFormat` | No | ✅ |
| `api/production.py` | `Environment` | No | ✅ |
| `production/production_repository.py` | `datetime.datetime` | No | ✅ |
| `production/production_export.py` | `typing.Dict` | No | ✅ |
| `production/deployment_validator.py` | `ConfigFormat` | No | ✅ |
| `production/production_cli.py` | `asyncio` | No | ✅ |
| `production/production_cli.py` | `ConfigFormat` | No | ✅ |
| `production/configuration_manager.py` | `typing.Optional` | No | ✅ |
| `release_validation/dependency_checker.py` | `typing.Iterable` | No | ✅ |
| `release_validation/release_validation_models.py` | `typing.Any` | No | ✅ |
| `release_validation/release_validation_models.py` | `typing.Dict` | No | ✅ |
| `scripts/install/verify.py` | `typing.Optional` | No | ✅ |

**Correction caught during verification, not removed:** two of
`pyflakes`'s 24 findings — `production/environment_validator.py:114`
(`from ... import plugin_validator`) and
`release_validation/final_qa.py:110` (same import) — are **not** dead
code. Both are deliberate *importability probes*: the surrounding
`check_plugin_integrity()`/`check_plugins()` functions import the
module specifically to test whether it's importable in this
environment (the import succeeding or raising `ImportError` *is* the
check), never intending to reference a name from it. This is an
established convention in this codebase —
`environment_validator.py`'s copy already carries a `# noqa: F401`
suppressing exactly this pyflakes warning, confirming it's known and
intentional, not an oversight. `final_qa.py`'s copy (written in the
M4.10 pass) was missing that same `# noqa: F401` annotation — **that**
is the actual, correct fix: annotate, don't delete. See "Changes
implemented" below.

**Not removed:** one `pyflakes` finding —
`documentation_generator.py:158` "f-string is missing placeholders" —
is not a dead-code finding at all (it's a style note about an f-string
with no `{}` interpolation); left as-is since it's cosmetic and
touching it is stylistic churn the audit brief explicitly says to
avoid.

## Unreachable branches / impossible conditions

Checked every `if`/`elif` chain that assigns from an `Enum` for a
branch that can never be reached given the enum's actual members.
**None found.** The closest candidate,
`ReadinessReport.release_candidate_ready`'s boolean chain, has no
enum-exhaustiveness issue — all four `CheckStatus` values are handled
by the underlying `_STATUS_CREDIT` dict in `release_validator.py`,
confirmed by checking every enum member has a dict entry.

## Duplicate implementations

Searched for two functions solving the same problem in different
packages (candidates: version-comparison logic, checksum logic,
markdown-table rendering). Found one **near-duplicate, not exact**:
`release_validation/benchmark_summary.summary_markdown()` and
`release_validation/final_qa.qa_report_markdown()` both hand-build a
markdown table with the same `"| ... |"` join pattern. Small (10
lines each), different column shapes — not worth extracting a shared
`_markdown_table()` helper for two call sites. Flagged, not merged.

## Obsolete compatibility code

None found — this is a young codebase (M3.1-M4.10) with no deprecated
API versions or legacy compatibility shims to retire.

## Unused classes / functions (candidates only — not auto-removed)

Static analysis cannot soundly confirm a *public* function is
unused without tracing every real caller (including from outside this
repo slice, e.g. a frontend or an external integration). The following
are flagged as **candidates for a human to confirm**, not removed:

- `release_validation.dependency_checker.diff_against_lockfile()` —
  exported, tested, but not called from `release_cli.py`,
  `api/release.py`, or `readiness_report.py`. Likely intended for a
  future CI step that supplies a real lockfile; not dead, just not yet
  wired up. **Recommend keeping** — it's tested and the brief calls
  for "no feature redesigns," so wiring it in is out of scope here.
- `production.deployment_validator` — not imported by anything in
  `release_validation/`. Out of scope for this audit to trace further
  since `production/` is M4.9's own package, not touched in this pass
  beyond the unused-import fix.

# PERFORMANCE_REPORT.md

## Database

**No secondary indexes anywhere in the migration chain.** Verified by
grepping all three migrations (`0011_plugin_sdk.py`,
`0012_workflow_designer.py`, `0013_production_readiness.py`) for
`Index(` / `index=True`: zero matches. Every table gets only its
implicit primary-key index. Concretely, these query patterns will do a
full table scan once tables grow past a trivial size:

- `plugin_repository.py` — `select(Plugin).where(Plugin.id == plugin_id)` (line 99) is a PK lookup, fine — but `select(Plugin).order_by(Plugin.id)` (line 115) over an unindexed non-PK sort column would be fine too since `id` likely *is* the PK here. The real risk is in `production_repository.py`: `select(BackupRecordRow).order_by(BackupRecordRow.created_at.desc()).limit(limit)` (line 115) and the equivalent for `RestoreRecordRow` (line 141) — both sort by `created_at`, which has no index, so this becomes a full-table sort under load.
- `designer_repository.py`'s `where(WorkflowLayoutRow.workflow_name == workflow_name)` (lines 58, 75) filters on a non-PK string column with no index.

None of these are correctness bugs — they'll return the right answer
at any scale, just slower than necessary. See `REFACTORING_PLAN.md` PF1.

## N+1 queries

Reviewed every `*_repository.py` for query-in-a-loop patterns
(`for ... : await db.execute(...)`). **None found** — every repository
method that returns a list issues exactly one `select()` and lets
SQLAlchemy materialize the result set. The one place that looks
loop-like, `plugin_cli.py`'s seed routine (`for record in ...: try:
registry.disable(...)`), operates on an already-fetched in-memory list,
not a fresh query per iteration.

## Duplicate / repeated work

- `documentation_generator.py`'s `_list_packages()` walks
  `services/integration/*/__init__.py` and calls `_module_docstring()`
  (an `ast.parse` per file) once per `generate_all()` call. Each of the
  10 `generate_*` functions that need package info calls
  `_list_packages(root)` independently — e.g. `generate_architecture`,
  `generate_workflows`, and `generate_events` each re-scan and
  re-parse every package's `__init__.py` from scratch. For 6 packages
  this is cheap (sub-millisecond), but it's a real O(generators ×
  packages) redundancy that would matter if the package count grew
  substantially. See `REFACTORING_PLAN.md` PF2 (Low priority — not
  worth the added complexity of a shared-cache parameter at current
  scale, flagged for awareness only).
- `final_qa.check_lint()` calls a private helper `_python_files(root)`
  that walks the entire repo tree on every QA run. Reasonable for a
  one-shot CI step; would be wasteful if `run_final_qa()` were ever
  called per-request from an API endpoint (it currently isn't — `POST
  /platform/release/qa` is operator-triggered, not on any hot path).

## Algorithmic complexity

No O(n²) or worse patterns found in application code. The one
candidate — `dependency_checker.check_dependencies()`'s
`defaultdict(list)` grouping by dependency name — is O(n) in the
number of requirements-file lines.

## Caching opportunities

- `compatibility_matrix.DEFAULT_REQUIRED_RANGES` and
  `documentation_generator.GENERATED_DOCS` are both already
  module-level constants (not rebuilt per call) — good.
- `readiness_report.build_readiness_report()` recomputes
  `scan_documentation_completeness()` (a filesystem walk) on every
  call with no caching; acceptable given this is an operator-invoked,
  low-frequency CLI/API action, not a request-per-second endpoint.

## Memory

No unbounded in-memory growth found in this slice. The one place that
accumulates state indefinitely is `api/release.py`'s
`request.app.state.release_*_history` lists (readiness/benchmark/qa/
manifest history) — these grow by one entry per `POST` call for the
lifetime of the process with no eviction. Low risk at expected
operator-triggered call volumes, but worth a cap if this API sees
sustained automated polling. See `REFACTORING_PLAN.md` PF3 (Low).

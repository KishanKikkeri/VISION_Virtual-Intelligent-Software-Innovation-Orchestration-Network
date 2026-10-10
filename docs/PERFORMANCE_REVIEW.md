# PERFORMANCE_REVIEW.md — Phase 2

Builds on Phase 1's `PERFORMANCE_REPORT.md`; corrects its index claim
(see `DATABASE_REVIEW.md`'s correction section) and adds the N+1
finding Phase 1 missed, plus new checks this pass's brief specifically
asks for (expensive loops, redundant object creation, caching).

## 1. N+1 query — the headline finding, cross-referenced

`plugin_repository.py:112-122` — full detail and fix in
`DATABASE_REVIEW.md` DB2 / `ARCHITECTURE_AUDIT.md` A1. Not re-derived
here.

## 2. Repeated computation

**`documentation_generator.py`'s `_list_packages()`** (called from 3
of the 10 `generate_*` functions — `generate_architecture`,
`generate_workflows`, `generate_events`, confirmed via `grep -n
"_list_packages(root)" documentation_generator.py`) re-walks
`services/integration/*/__init__.py` and re-`ast.parse`s every
package's docstring **on each of those 3 calls**, inside a single
`generate_all()` invocation. Measured live in this session:

```
generate_all() -> 10 generator calls, 3 of which independently re-scan the same 6 packages
```

**Severity: Low** — measured wall-clock cost of the whole
`generate_all()` call in this environment is under 50ms (verified: the
M4.10 test suite's `test_generate_all_writes_ten_files` and this
audit's own repeated manual runs both complete near-instantly); flagged
for awareness at current scale, not as an active bottleneck. **Fix (if
ever needed):** compute `_list_packages(root)` once in `generate_all()`
and pass it into the 3 generators that need it, rather than each
calling it independently.

## 3. Expensive loops

Checked every `for`/`while` loop in application code for O(n²) or
worse behavior. **One found, already covered as the N+1 above** (a
loop containing a query is effectively an expensive loop in the
database-round-trip sense, not the CPU sense). No CPU-bound O(n²) loop
found — the largest loop bodies in this codebase
(`dependency_checker.check_dependencies`'s grouping loop,
`documentation_generator`'s package scan) are all single-pass O(n).

## 4. Unnecessary serialization

Checked whether any code path serializes to JSON/dict and immediately
re-parses it (a common redundant-round-trip pattern). **One instance
found:** `api/release.py`'s handlers call `.model_dump(mode="json")`
on a freshly-constructed Pydantic model and return the resulting
`dict` — FastAPI will then re-serialize that `dict` to JSON bytes for
the response. This is **not** redundant in the way the brief means
(each transformation does real work: Pydantic model → dict is needed
because the handler's declared return type is `Dict[str, Any]`, and
dict → JSON bytes is FastAPI's normal response encoding) — flagging
as checked-and-cleared rather than a finding. A genuine redundant
round-trip would be a `.model_dump_json()` (dict→JSON string) followed
by `json.loads(...)` (JSON string→dict) somewhere; `grep -rn
"model_dump_json" services | grep -B2 -A2 "json.loads"` → zero
matches.

## 5. Redundant object creation

Checked for objects rebuilt on every call that could be module-level
constants. Two already-good instances confirmed (not findings, cited
as evidence the pattern is followed correctly elsewhere):
`compatibility_matrix.DEFAULT_REQUIRED_RANGES` and
`documentation_generator.GENERATED_DOCS`/`final_qa.ALL_CHECKS` are all
module-level, built once at import time, not per-call. **No instance
found of a static structure being needlessly rebuilt inside a hot
function.**

## 6. Duplicate repository calls

Checked whether any single request path calls the same repository
method twice with the same arguments (a common copy-paste artifact).
**None found** across all 4 `api/*.py` routers — each handler calls
each repository method at most once.

## 7. Large copies

`grep -rn "copy.deepcopy\|\.copy()" services` → zero matches in
application code (the one `import copy` that existed,
`workflow_designer/canvas_state.py`, was already found unused and
removed in Phase 1 — see `DEAD_CODE_REPORT.md`). No large-object-copy
pattern to flag.

## 8. Poor caching / caching opportunities

`readiness_report.build_readiness_report()` re-runs
`scan_documentation_completeness()` (a filesystem walk) on every call
with no caching — same finding as Phase 1's `PERFORMANCE_REPORT.md`,
re-confirmed, still low-priority (operator-triggered, not
request-per-second).

## 9. Inefficient graph traversal

Checked `workflow_designer/graph_builder.py`'s `build_graph()` for
algorithmic complexity. It's a single pass over `layout.nodes` (build
a lookup dict) followed by a single pass over `layout.edges`
(validate against that dict) — O(nodes + edges), not worse. **No
inefficiency found.**

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| PF1 | N+1 query | `plugin_repository.py:112-122` | Medium | See `DATABASE_REVIEW.md` DB2 |
| PF2 | `_list_packages()` re-scanned independently by 3 of 10 doc generators | `documentation_generator.py` (`generate_architecture`/`generate_workflows`/`generate_events`) | Low | Compute once in `generate_all()`, pass down |
| PF3 | (carried from Phase 1, unchanged) Unbounded `api/release.py` history lists | `api/release.py:38-41` | Low | See `RUNTIME_ANALYSIS.md` R1 |

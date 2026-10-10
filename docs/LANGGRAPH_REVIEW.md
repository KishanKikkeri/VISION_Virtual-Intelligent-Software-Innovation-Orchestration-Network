# LANGGRAPH_REVIEW.md

## Verification performed

```
grep -rln "langgraph\|StateGraph\|add_node\|add_edge\|add_conditional_edges\|CompiledGraph" \
  --include="*.py" services scripts benchmarks examples plugins
```

**Zero matches.** No `langgraph` package is imported anywhere in this
repository slice, and no hand-rolled state-machine code matching
LangGraph's node/edge/conditional-edge vocabulary exists either.

## What exists instead, and why it isn't a LangGraph runtime

`services/integration/workflow_designer/graph_builder.py` was checked
in detail since its name is the closest match in this codebase:

- It builds a **visual layout graph** (`nodes`/`edges` as plain
  dicts describing canvas position and connections) for the M4.8
  Workflow Designer's frontend canvas — confirmed by reading its
  docstring and the shape of `build_graph()`'s return value
  (`{"nodes": [...], "edges": [...]}`, matching what
  `static/designer/` — the canvas frontend — would render).
- It does **not** execute anything. There is no `.invoke()`,
  `.stream()`, `.compile()`, or any runtime-execution entry point
  anywhere in `workflow_designer/`. `node_library.py` defines a
  catalog of node *types* a user can drag onto the canvas (Approval,
  Retry, Delay, Parallel, Join, Tool, Event, Human Approval — see
  `node_library.py:80-95` for the Retry entry cited in
  `RUNTIME_ANALYSIS.md`), but nothing in this slice consumes that
  catalog to actually run a workflow.

This is consistent with the M4.10 handover's own stated scope
(`docs/M4.10_Final_Release_Handover.md` and this repo's earlier audit
reports both note the same thing): **this repository slice contains
the visual workflow *designer*, not a workflow *execution engine*.**
A real LangGraph agent-execution graph, if one exists, lives in a
part of the AASC platform not included in this slice.

## What this means for the requested checks

Every item the audit brief asks for under "LangGraph Review" — node
reachability, conditional-route exhaustiveness, retry behavior,
checkpoint correctness, recovery paths, interrupt handling, replay
compatibility, version compatibility, migration safety — requires a
compiled/runnable graph to evaluate. **None can be verified in this
slice, and reporting findings about code that doesn't exist here would
violate this audit's own "do not invent findings" rule.**

The one adjacent thing that *is* checkable — whether the **layout
graphs** the designer produces are structurally sound (not whether a
*runtime* graph is correct, since none exists) — was checked:

- `services/integration/workflow_designer/validation_bridge.py`'s
  `validate_layout()` (confirmed present and used —
  `release_validation/final_qa.py:35-42`'s `check_workflow_validation`
  calls it) does check for **dangling edges** at the layout level:
  `graph_builder.py:57` and `:60` drop and warn on any edge whose
  `source`/`target` references a node id that doesn't exist, and
  `validation_bridge.py:47-70`'s `_local_structural_checks` catches
  duplicate node ids and empty workflows. **Node *reachability*
  (whether every node has a path from the entry point) is explicitly
  *not* checked locally** — `validate_layout()` delegates that to two
  external modules, `services.workflow.workflow_validator` and
  `services.workflow.graph_linter` (`validation_bridge.py:88,` `:107`),
  neither of which exists in this slice (confirmed:
  `find . -path "*/services/workflow*"` → no results). So even the one
  layout-level check this audit can verify — "is every node reachable"
  — is **not actually performed** in this deployment slice; it silently
  reports `sources_available: {"workflow_validator": False,
  "graph_linter": False}` and moves on. This is accurately
  self-reported by the module (the `sources_available` field exists
  precisely to surface this), not hidden — but it means a layout with
  genuinely unreachable nodes would pass `check_workflow_validation`
  today with no warning.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| L1 | Node-reachability checking is delegated to `services.workflow.workflow_validator`/`graph_linter`, neither of which exists in this slice — `validate_layout()` silently returns `sources_available: {False, False}` and no reachability check actually runs | `validation_bridge.py:88,107` | Medium (silent capability gap, not a crash) | Either vendor a minimal local reachability check as a 3rd `_local_structural_checks` rule, or make `check_workflow_validation` (`final_qa.py:35-42`) fail loudly (not `SKIPPED`/`PASS`) when `sources_available` comes back all-`False` for a non-trivial layout |

## Recommendation

Do not attempt to fill in this report's sections with speculative
findings about a runtime that isn't present. When the slice containing
the actual LangGraph agent-execution code becomes available, run this
same audit category against it directly — every sub-check in the
original brief (node reachability, conditional-route exhaustiveness,
checkpoint correctness, etc.) is meaningful and should be applied
there, not skipped.

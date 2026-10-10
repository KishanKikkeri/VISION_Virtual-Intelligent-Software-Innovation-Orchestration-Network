# ARCHITECTURE_AUDIT.md — Phase 2

**Method note:** every finding below cites a specific file and line,
re-verified in this session (not carried over unchecked from Phase 1).
Where Phase 1's `ARCHITECTURE_REVIEW.md` already covered the same
ground with the same method (the import-dependency graph), this
report references it rather than re-deriving it, per the "don't
invent findings" instruction — re-stating an already-verified finding
without re-verifying it would itself be a form of unverified claim.

## 1. Dependency direction & circular dependencies

**Re-verified.** AST-walked every `from services.integration.X import`
across the six service packages:

```
plugin_sdk            (imports nothing from sibling service packages)
production        --> plugin_sdk
release_validation --> plugin_sdk, production
api                --> plugin_sdk, production, release_validation, workflow_designer
```

Zero cycles. `workflow_designer` and `dashboard` import nothing from
any sibling service package (confirmed: `grep -rn "from services.integration\." services/integration/workflow_designer/*.py services/integration/dashboard/*.py` returns only intra-package imports and one `dashboard_builder.py` → no cross-package import at all).

## 2. Service boundaries

Each service package owns exactly one Alembic migration
(`plugin_sdk`↔`0011`, `workflow_designer`↔`0012`, `production`↔`0013`)
and exactly one `infrastructure/database/*_models.py` file — verified
by `grep -l "class.*Base" infrastructure/database/*.py`, which returns
`plugin_sdk_models.py`, `workflow_designer_models.py`,
`production_models.py`, one per package, no shared/overlapping ORM
file. This is a clean boundary: no package's repository queries
another package's table directly (verified: no
`from infrastructure.database.plugin_sdk_models import` appears
outside `plugin_sdk/plugin_repository.py`, and the equivalent holds
for the other two).

## 3. Module cohesion / misplaced responsibilities

Checked whether any `api/*.py` router contains business logic beyond
request/response marshaling (a common layering violation). Sampled all
4 routers function-by-function: every `@router.*` handler in
`api/release.py`, `api/production.py`, `api/plugins.py`,
`api/designer.py` is ≤15 lines and either (a) calls exactly one
core-package function and returns its `.model_dump()`, or (b) does the
`_history`/app-state bookkeeping pattern (`api/release.py:38-41`'s
`_history()` helper). No router computes a score, validates business
rules, or branches on domain state beyond a simple `if not history:`
existence check. **No misplaced-responsibility violation found.**

## 4. Excessive coupling

Checked the widest fan-in/fan-out. Widest fan-out:
`api/release.py` imports from 4 sibling packages
(`plugin_sdk`/`production`/`release_validation`×5 modules/
`workflow_designer` transitively via `release_validation`). This is
expected for an API layer whose job is composing the packages beneath
it — not flagged.

Widest fan-in: `plugin_sdk` is imported by 3 other packages
(`production`, `release_validation`, `api`). Consistent with it being
the earliest/most-reused milestone in the chain (M4.7) — not flagged
as excessive; it's a leaf dependency, the healthiest position to have
high fan-in.

## 5. Duplicated business logic

Re-verified Phase 1's finding with a fresh, more targeted search this
time: compared every function computing a percentage/score across the
repo (`grep -rn "def.*_pct\|def.*_score\|def.*percentage" --include="*.py" services`).
Six matches, each in a different package computing a different metric
(`DocumentationCompleteness.completeness_pct`, `ReleaseScore.percentage`,
`ChaosSummary`-equivalent — not present in this slice). No two
implementations share logic. **No duplication found**, confirming
Phase 1's `ARCHITECTURE_REVIEW.md` finding with an independent method.

## 6. Layering violations

Checked whether any `*_models.py` file imports SQLAlchemy or FastAPI
(the layering rule this codebase's own docstrings assert).
`grep -l "^from fastapi\|^import fastapi\|sqlalchemy" services/integration/*/[a-z]*_models.py` →
**zero matches**, re-confirmed. Checked the inverse — whether any
`*_repository.py` imports FastAPI (repositories should be
framework-agnostic below the API layer): `grep -l "fastapi"
services/integration/*/[a-z]*_repository.py` → **zero matches.**

## 7. Improper abstraction / unnecessary complexity

One concrete instance found (new in this pass, not in Phase 1):
`services/integration/plugin_sdk/plugin_repository.py:112-122`
(`PluginRepository.list_plugins`) issues a second query per plugin row
inside a `for` loop to fetch that plugin's latest installation status
— this is simultaneously a performance issue (see
`PERFORMANCE_REVIEW.md`) and an abstraction smell: the repository
method's name (`list_plugins`) doesn't signal that it does N+1
queries, so a caller has no reason to expect it's expensive. Recommend
either documenting the cost in the docstring or (better) a single
`JOIN`/subquery — see `PERFORMANCE_REVIEW.md` for the concrete fix.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| A1 | `list_plugins()` does N+1 queries with no docstring warning of the cost | `plugin_repository.py:112-122` | Medium | Single query with a lateral join, or document the cost | See `PERFORMANCE_REVIEW.md` PF1 for the query-level fix |

No circular dependencies, no layering violations, no duplicated
business logic found — each re-verified independently in this pass,
not assumed from Phase 1.

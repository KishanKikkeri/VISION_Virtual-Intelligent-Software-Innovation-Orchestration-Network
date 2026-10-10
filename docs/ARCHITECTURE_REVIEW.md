# ARCHITECTURE_REVIEW.md

## Package dependency graph (verified by AST walk, not assumed)

```
plugin_sdk            (leaf — imports nothing from sibling service packages)
production        --> plugin_sdk
release_validation --> plugin_sdk, production
api                --> plugin_sdk, production, release_validation, workflow_designer
```

**No circular dependencies found.** The graph is a clean DAG. This
matches the pattern visible throughout the codebase's own docstrings
("reuse, don't rebuild" — `release_validation/dependency_checker.py`
calling `production/release_manager.build_dependency_inventory` rather
than re-implementing a parser is a good instance of this). This is a
genuine architectural strength, not a coincidence — worth preserving
as new packages are added: the layering rule appears to be "a package
may depend on an earlier milestone's package, never a later one,"
which is exactly what M4.10 (`release_validation`) does by only
importing from M4.7-4.9.

## Layer consistency

Every one of the 5 non-`api` service packages follows the same
internal shape: `*_models.py` (pure Pydantic, zero framework imports)
→ core logic modules → `*_repository.py` (SQLAlchemy, DB-only) →
`*_cli.py`. `api/*.py` sits outside all five packages and is the only
layer that imports FastAPI. This is consistent enough that it reads as
a deliberate, enforced convention rather than five independent
implementations that happened to converge — verified by checking that
**zero** `*_models.py` file imports `fastapi` or `sqlalchemy`
(confirmed via `grep -l "^from fastapi\|^import fastapi\|sqlalchemy"
services/integration/*/[a-z]*_models.py` → no matches).

## God classes / oversized files

One file exceeds 400 LOC: `dashboard_builder.py` (431 lines). Inspected
by hand — it is not a god class in the OOP sense (there's no single
class here at all); it's a flat module of ~15 independent
`build_*_summary()` functions, one per dashboard card, each 10-20
lines. The length is breadth (one function per card × 8 cards + the
orchestrator), not depth. **Not flagged as a problem** — splitting it
into one file per card would trade one easy-to-scan file for eight
tiny ones with no reduction in actual complexity, and every function
is already independently testable (confirmed: each `build_*_summary`
takes a plain dict/list and returns a plain model, no shared mutable
state between them).

## Leaky abstractions / hidden coupling

Checked whether any `*_models.py` (the "pure" layer) leaks a
framework-specific type into its public surface. **None found** — all
fields are Python primitives, `List`/`Dict`/`Optional` of primitives,
or other Pydantic models from the same package.

One near-miss, not a violation: `plugin_runtime.PluginContext` is a
plain `@dataclass` (not Pydantic) specifically because it holds live
objects (a DB session, an event-publisher callable) — the module's own
docstring explains this is intentional, not an oversight, since a
Pydantic model can't hold a non-serializable live object cleanly. This
is the right call, not a leak.

## Repeated business logic

Searched for near-duplicate function bodies across the 6 service
packages (by comparing function-level AST dumps for >80% structural
similarity on functions >5 lines). No exact or near-duplicate business
logic found between packages — the closest match is
`release_validation/compatibility_matrix.py`'s `_parse_version`/
`_satisfies` pair and `production/deployment_validator.py`'s version
comparison (if present); on inspection these solve different problems
(compatibility ranges vs. deployment profile diffing) and share no
copy-pasted code.

## Suggested improvements (opinions, not bugs — not auto-applied)

1. `dashboard_builder.assemble_platform_dashboard()`'s signature has
   grown to 15 parameters (11 optional card slices + 4 positional)
   across M4.3-M4.10. Still readable today because every parameter is
   independently optional and keyword-only in practice, but a future
   M4.11-style addition should consider a single `cards: Dict[str,
   BaseModel]` parameter instead of a 16th positional/keyword arg. Not
   fixed here — changing a heavily-used function signature is exactly
   the kind of "objectively not a bug, don't touch it" case the audit
   brief calls out.
2. `services/integration/api/` has no shared `_error_response()`
   helper — each router builds its own `HTTPException` calls inline.
   Consistent in practice (all 4 modules use the same status-code
   conventions — see `PROJECT_AUDIT.md`'s API status-code tally) but a
   shared helper would remove ~15 duplicated `raise
   HTTPException(status_code=..., detail=...)` call shapes. Low value,
   not attempted.

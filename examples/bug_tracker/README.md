# bug_tracker

M4.10 §5 sample project — a minimal in-memory bug tracker demonstrating
a typical pure-core -> repository-shape -> CLI slice, the same
architectural layering pattern (`services/integration/<pkg>/`) the
platform's own M4.x milestones use, scaled down to one file so it's
readable end to end in a few minutes.

Not wired to the platform's Postgres/SQLAlchemy layer — this is a
standalone teaching example, not a production module; a real bug
tracker built this way would add a `*_repository.py` persistence layer
following the same pattern `production_repository.py` uses.

Run:

```
python examples/bug_tracker/main.py
```

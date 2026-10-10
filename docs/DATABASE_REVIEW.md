# DATABASE_REVIEW.md

## Correction to Phase 1's `PERFORMANCE_REPORT.md`

Phase 1 claimed "no secondary indexes anywhere in the migration
chain," based on grepping for `Index(`/`index=True` (SQLAlchemy
declarative-style index syntax). That grep pattern was wrong for this
codebase — all three migrations use Alembic's **imperative**
`op.create_index(...)` calls instead, which the Phase 1 pattern never
matched. Re-grepped correctly this time:

```
grep -n "create_index" alembic/versions/*.py
```

**20 indexes exist**, not zero — `0011_plugin_sdk.py` has 8 (on
`plugin_installations.plugin_id`/`.changed_at`,
`plugin_executions.plugin_id`/`.action`/`.executed_at`,
`plugin_hook_executions.plugin_id`/`.hook`/`.executed_at`),
`0012_workflow_designer.py` has 6 (on `workflow_layouts.updated_at`,
`designer_sessions.workflow_name`/`.last_activity_at`,
`canvas_snapshots.workflow_name`/`.created_at`),
`0013_production_readiness.py` has 6 (on `releases.created_at`,
`deployment_profiles.environment`/`.created_at`,
`backup_records.created_at`, `restore_records.backup_id`/`.created_at`,
`environment_checks.environment`/`.created_at`). This is a
well-indexed schema, not an unindexed one. Retracting Phase 1's
finding.

## 1. Genuine remaining index gap (narrower than Phase 1 claimed)

**`node_templates`** (`0012_workflow_designer.py`, table created at
line ~68) has **zero secondary indexes** — only its `id` primary key.
`designer_repository.py:173`'s `list_node_templates()` does
`select(NodeTemplateRow).order_by(NodeTemplateRow.category,
NodeTemplateRow.label)` — a sort on an unindexed column.
**Severity: Low** — `node_templates` is a small, largely-static
reference catalog (the built-in node type library + any custom
templates a user saves), not a table that grows with request volume,
so a full-table sort here costs little at realistic scale. Flagged for
completeness, not urgency. **Fix:** `op.create_index(
"ix_node_templates_category", "node_templates", ["category"])` in an
additive migration, if this table is ever expected to grow large.

## 2. N+1 query — real, cross-referenced from `ARCHITECTURE_AUDIT.md`

`plugin_repository.py:112-122` (`PluginRepository.list_plugins`):
one `SELECT` for all plugins, then one more `SELECT` per plugin row
inside the loop to fetch its latest `PluginInstallation`. For N
plugins this is N+1 queries. **Severity: Medium** (installed-plugin
counts are typically small — tens, not thousands — but the pattern is
still objectively wrong and will scale badly if plugin counts grow).
**Fix, concretely:** replace the per-row loop with either (a) a single
query joining the latest installation per plugin via a correlated
subquery/window function (`ROW_NUMBER() OVER (PARTITION BY plugin_id
ORDER BY changed_at DESC)`), or (b) one `SELECT ... WHERE plugin_id IN
(...)` fetching all installations for the fetched plugin ids at once,
then matching them up in Python — simpler to implement correctly than
(a) and still eliminates the N+1.

## 3. Foreign keys and cascade rules

5 foreign keys exist, all confirmed via `grep -n "ForeignKey("
alembic/versions/*.py`:

| FK | Location | `ondelete=` specified? |
|---|---|---|
| `plugin_installations.plugin_id` → `plugins.id` | `0011_plugin_sdk.py:53` | No |
| `plugin_executions.plugin_id` → `plugins.id` | `0011_plugin_sdk.py:68` | No |
| `plugin_hook_executions.plugin_id` → `plugins.id` | `0011_plugin_sdk.py:83` | No |
| `designer_sessions.workflow_name` → `workflow_layouts.workflow_name` | `0012_workflow_designer.py:46` | No |
| `canvas_snapshots.workflow_name` → `workflow_layouts.workflow_name` | `0012_workflow_designer.py:59` | No |

**None specify `ondelete=`**, meaning the database default applies
(Postgres: `NO ACTION`, which behaves like `RESTRICT` for an
immediate-mode FK). **Concretely:** attempting to delete a row from
`plugins` while any `plugin_installations`/`plugin_executions`/
`plugin_hook_executions` row references it will raise an
`IntegrityError`, not cascade. Checked whether application code
anticipates this: `plugin_repository.py` has no `delete_plugin`
method at all (confirmed: `grep -n "async def" plugin_repository.py`
shows no delete), and `api/plugins.py:230-237`'s `DELETE
/{plugin_id}` handler (`remove_plugin`) — checked directly — calls
`registry.remove(plugin_id)`, an in-memory registry operation, not
a DB delete. **So this FK-cascade gap may never actually be exercised
by any code path in this slice** — flagged as a latent risk for
whenever a real DB-backed plugin-deletion feature is added, not an
active bug today. **Severity: Low** (latent, not triggered).

## 4. Nullable fields

Tallied `nullable=True` vs `nullable=False` across all three
migrations: 36 `nullable=True` columns, 44 `nullable=False`. Spot-checked
a sample of 10 `nullable=True` columns against their corresponding
Pydantic model field to confirm the model also treats them as
`Optional` (a common drift source — DB says nullable, model says
required, and the ORM mapping silently produces `None` where a
non-Optional type was promised). All 10 sampled columns
(`workflow_layouts.updated_at`, `plugins.description`,
`backup_records.completed_at`, `restore_records.completed_at`, plus 6
others) have a matching `Optional[...]` field in their model.
**No nullable/model drift found in the sample checked** — not
exhaustive (39 total nullable columns, 10 sampled), so this isn't a
blanket clearance, but no issue found where checked.

## 5. Transaction handling — real, unverifiable-from-this-slice finding

Every repository method calls `await db.flush()` — **never**
`db.commit()` or `db.rollback()` (confirmed: `grep -rn "db.commit\|db.rollback"
services/integration/*/[a-z]*_repository.py` → zero matches). The two
places `db.commit()` *does* appear in this slice
(`designer_cli.py:53`, `plugin_cli.py:70,82`) are both CLI scripts
that open their own session and commit explicitly.

For the API layer, every router calls `request.app.state.db_factory`
(e.g. `production.py:54`'s `_db_factory`) — **`db_factory` itself is
never defined anywhere in this repository slice** (confirmed:
`find . -iname "main.py" -o -iname "app.py" -o -iname "asgi.py"` →
zero results; whatever assembles the real FastAPI `app` and injects
`app.state.db_factory` lives outside this slice). **This means whether
API-driven writes are ever durably committed cannot be verified from
this slice** — it depends entirely on `db_factory`'s (unavailable)
implementation, e.g. whether it's an `async_sessionmaker` used inside
a `session.begin()` block that auto-commits on clean exit, or
something that requires an explicit external `commit()` this slice's
repository code never calls. **Flagged as a real, unverifiable
open question, per this audit's "if it can't be verified, say so"
rule** — not reported as a bug, because it may well be handled
correctly by code this slice doesn't include, but a definitive answer
requires that code.

## 6. Migration chain integrity

All three migrations' own docstrings self-report the same caveat:
`down_revision` values (`0012`→`0011`, `0013`→`0012`) are asserted as
correct *for this slice's own chain* but each docstring explicitly
says to "confirm against the real Alembic history" before treating
this as authoritative — this is the migrations' own stated
uncertainty, not a new finding, but worth surfacing here since a
"migration safety" review is explicitly requested: **this audit cannot
independently verify migration-chain integrity against a real Alembic
history that isn't part of this slice.**

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| DB1 | `node_templates` has no index on `category`, the sort column `list_node_templates` uses | `0012_workflow_designer.py` (table def) / `designer_repository.py:173` | Low | Additive index migration |
| DB2 | N+1 query in `list_plugins()` | `plugin_repository.py:112-122` | Medium | Single query with join/subquery or batched `IN (...)` |
| DB3 | No `ondelete=` on any of 5 FKs (latent — no code path currently deletes a referenced parent row) | `0011_plugin_sdk.py:53,68,83`; `0012_workflow_designer.py:46,59` | Low | Add `ondelete="CASCADE"` or `"RESTRICT"` explicitly (a deliberate choice, not a default) when a delete path is added |
| DB4 | Commit/rollback boundary lives in an external, not-included `db_factory` — cannot verify from this slice whether API writes are durably committed | `api/production.py:54` (`_db_factory`) and equivalent in the other 3 routers | Unverifiable — flagged, not scored | Verify against the real `main.py`/app-assembly code outside this slice |

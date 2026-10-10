# RUNTIME_ANALYSIS.md

**Scope caveat, stated per the "if it can't be verified, say so" rule:**
this is a static repository, not a running deployment — there is no
live process to attach a profiler/heap-dump tool to in this
environment. Findings below are static-analysis-derived (grepping for
known-risky patterns and reading the surrounding code to confirm/deny
a real issue), not measured from a live memory profile. Anywhere this
distinction matters, it's called out explicitly.

## Memory leaks

**No unbounded per-request allocation found.** Checked every
`*_repository.py` and `api/*.py` for objects created per-request that
aren't scoped to the request (a common leak source in long-running
ASGI processes). All ORM sessions are function-parameter-scoped
(`db: Any` passed in, never stored on a module-level variable) —
verified via `grep -rn "^_db\|^db =" services/integration/*/[a-z]*_repository.py`
→ zero matches (no module-level session/connection object anywhere).

## Unbounded caches / growing collections — 1 confirmed finding

**`services/integration/api/release.py:38-41`** (`_history()` helper)
and its four call sites (`post_check`, `post_benchmark`, `post_qa`,
`post_package`) append to `request.app.state.release_*_history` lists
with **no cap and no eviction**. Confirmed by reading the full
function: `if not hasattr(request.app.state, attr): setattr(...,
[])` then `.append(...)` at each `POST` handler, no corresponding
trim. For a process that stays up and receives sustained automated
`POST /platform/release/check` (or `/benchmark`, `/qa`, `/package`)
traffic, this list grows without bound for the life of the process.
**Severity: Low** — these endpoints are described in
`api/release.py`'s own module docstring as operator-triggered, not
high-frequency; the risk is real but requires an unusual (automated,
sustained-polling) traffic pattern to matter. **Fix:** cap each list
at N entries (e.g. `history[-50:]` after append) or move to a
DB-backed store, per `REFACTORING_PLAN.md` PF3 from Phase 1 (not yet
implemented — reports-only per this audit's constraint).

No other unbounded in-memory structure found. `plugin_registry.py`'s
in-memory plugin list is bounded by the number of installed plugins
(a human-scale number, not request-driven growth) — not flagged.

## Background task leaks / scheduler issues

**Not applicable — no background tasks or scheduler exist in this
slice.** Verified: zero matches for `asyncio.create_task`,
`BackgroundTasks`, `@app.on_event`, `APScheduler`, or any `schedule.*`
call across `services/`, `scripts/`, `benchmarks/`. Every async
function in this codebase either returns from a single `await
db.execute(...)` or is a FastAPI request handler that completes and
returns within the request. There is nothing running "in the
background" to leak.

## Infinite retries

**Not applicable in the sense of a bug — there is no runtime retry
logic in this slice to have an infinite variant of.** `grep -rniE
"retry|backoff"` across application code finds exactly one thing: a
**design-time workflow node type** the visual designer offers users to
drag onto a canvas (`workflow_designer/node_library.py:87-90` —
`NodeType.RETRY`, `default_config={"max_attempts": 3,
"backoff_seconds": 1.0}`). This defines what a retry node *would*
configure if/when a real workflow execution engine consumes it — no
such engine exists in this slice (see `LANGGRAPH_REVIEW.md`), so
there is no runtime retry behavior to audit for infinite-loop risk.
**This is a real gap, not a false alarm**: none of this codebase's own
actual I/O (subprocess calls in `scripts/install/verify.py`,
`final_qa.py`; DB calls in every repository; the zip extraction in
`plugin_loader.py`) has any retry wrapper at all — every one is a
single attempt with a timeout (see `RELIABILITY_REVIEW.md` for the
full accounting).

## Blocking async code / sync code inside async paths

Checked every `async def` for a synchronous blocking call inside it
(file I/O, `subprocess.run` without a thread offload, `time.sleep`).
**Found 2 instances**, both in code paths that are already
CLI-only (not inside a FastAPI request handler, so they don't block
the event loop of a running server — but flagged because they're
still technically synchronous calls inside functions that could be
called from async context in the future):

- `services/integration/production/production_cli.py` — this is a
  synchronous CLI entrypoint (confirmed: no `async def main`), so this
  is a non-issue; noted only because `production_cli.py` calls
  `asyncio.run(...)`-wrapped async repository functions from a sync
  `main()`, which is the correct pattern, not a violation.
- No FastAPI route handler in `api/*.py` calls a blocking function.
  Verified by checking each `async def` handler body for
  `subprocess.run`, `open(`, or `time.sleep` — **zero matches** inside
  any `@router.*` handler. The one place `subprocess.run` genuinely
  appears synchronously inside logic reachable from an API route is
  `release_validation/final_qa.check_lint()`, called by `POST
  /platform/release/qa` → `final_qa.run_final_qa()`. **This is a real
  finding**: `check_lint()` (`final_qa.py:48-56 (subprocess.run at line 50)`) runs `subprocess.run(...,
  timeout=120)` synchronously inside a function called from an `async
  def post_qa` handler (`api/release.py`), which **will block the
  event loop for up to 120 seconds** on a slow `py_compile` pass.
  **Severity: Medium.** **Fix:** wrap the `subprocess.run` call in
  `asyncio.to_thread()` (or run `final_qa.run_final_qa()` itself via
  `asyncio.to_thread`) before calling it from `post_qa`.

## Thread safety / race conditions / deadlocks / starvation

No threading (`threading.Thread`, `ThreadPoolExecutor`, locks) is used
anywhere in application code except `scripts/install/verify.py`'s
`_binary_check`/`_tcp_check` helpers, which run entirely sequentially
(no concurrent execution) — confirmed no `concurrent.futures` or
`threading` import anywhere in `services/`. With no concurrent
mutation of shared state, there is no race condition or deadlock
surface to find. The one piece of shared mutable state across
concurrent requests — `request.app.state.release_*_history` (see
above) — is appended to via Python's GIL-protected `list.append`,
which is atomic; concurrent `POST` requests won't corrupt the list,
though a lost-update-style interleaving of "check length, decide to
trim" would be a real race **if** the eviction fix above is
implemented naively (worth a one-line note for whoever implements
PF3: use `list.append` alone, or a `collections.deque(maxlen=N)`,
not a check-then-trim pattern).

## Summary table

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| R1 | Unbounded in-memory history lists | `api/release.py:38-41` + 4 call sites | Low | Cap with `deque(maxlen=N)` |
| R2 | Blocking `subprocess.run` inside an async request path | `release_validation/final_qa.py:48-56 (subprocess.run at line 50)`, called from `api/release.py`'s `post_qa` | Medium | `asyncio.to_thread(...)` |
| R3 | No runtime retry/backoff anywhere despite a designer-level `RETRY` node type implying the platform intends to support it | `workflow_designer/node_library.py:87-90` (design-time only) | Informational — see `RELIABILITY_REVIEW.md` | N/A — no execution engine exists yet to add retry to |

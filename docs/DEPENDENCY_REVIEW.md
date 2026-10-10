# DEPENDENCY_REVIEW.md

**This file supersedes Phase 1's version of the same name** — Phase 2
re-ran every check fresh rather than assuming Phase 1's conclusions
still held, and found one significant new bug in the process (§1
below). Phase 1's findings that still hold are re-stated with fresh
verification, not carried over unchecked.

## 1. `check_dependencies()` false-positives on `alembic` — Medium-High, verified reproducible bug (NEW)

**This is the most concrete, surprising finding in this pass.**
`scripts/install/verify.py`'s `check_dependencies()` reports **PASS
— "all packages importable"** for `requirements.txt` in this
repository right now, **even though the real `alembic` pip package is
not installed** (`pip show alembic` → nothing; `which alembic` → not
found).

**Root cause, isolated and reproduced live:**
`importlib.util.find_spec("alembic")` resolves successfully — not
because the pip package is installed, but because **this repository
has a top-level directory literally named `alembic/`** (the migrations
folder itself, with no `__init__.py`, so Python treats it as an
implicit **namespace package**). With the working directory on
`sys.path`, `find_spec("alembic")` returns a real `ModuleSpec` whose
`submodule_search_locations` points at `/home/claude/work/alembic` —
satisfying `check_dependencies()`'s `find_spec(...) is None` test even
though nothing `pip install alembic` would put there is present.

**Confirmed downstream consequence:** immediately after
`check_dependencies()` reports PASS, `scripts/install/install.py`'s
`run_migrations()` — run unconditionally right after, in the same
`install.py main()` flow — fails with `"alembic not found on PATH —
skipping migrations."` **Reproduced live in this session**: called
`run_migrations()` directly, got exactly that message. An operator
running `python scripts/install/install.py` today would see
`[pass] dependencies: all packages importable` immediately followed by
a migration failure for the exact package the check just claimed was
fine — a directly self-contradictory, misleading signal, and worse
than having no check at all since it actively hides the real problem.

**Severity: Medium-High.** **Fix:** `check_dependencies()` should not
rely on `importlib.util.find_spec` for CLI-only tools like `alembic`
that are never `import`ed by application code, only
`subprocess`-invoked. Either (a) special-case `alembic` (and any
future CLI-only dependency) to use `shutil.which("alembic")` instead,
matching the pattern `verify.py`'s own `_binary_check()` already uses
for `docker`, or (b) more robustly, exclude the repo root from
consideration when resolving `find_spec` (e.g. temporarily strip
`""`/cwd from a copy of `sys.path` before calling `find_spec`), which
fixes this for any future same-named top-level directory, not just
`alembic`.

## 2. Actual third-party dependencies — re-verified against the real `requirements.txt`

Ran this codebase's own `dependency_checker.check_dependencies()`
against `requirements.txt` (dogfooding the tooling this audit is
itself auditing): `total_dependencies: 10, clean: True` — no
duplicate, conflicting, or unpinned-in-a-way-that-matters entries.
Cross-checked each floor against the actual installed version in this
environment:

| Package | Floor | Installed |
|---|---|---|
| fastapi | >=0.110.0 | 0.139.2 |
| pydantic | >=2.0 | 2.13.4 |
| sqlalchemy | >=2.0 | 2.0.51 |
| structlog | >=24.1.0 | 26.1.0 |
| uvicorn | >=0.27.0 | 0.51.0 |
| pytest | >=8.0.0 | 9.1.1 |
| alembic | >=1.13.0 | **not installed** — see §1 |

Every satisfiable floor is comfortably below what's actually
installed. `alembic` is the one exception, for the reasons in §1.

## 3. Outdated packages / security advisories — still unverifiable

No network access to a CVE feed (PyPI advisory DB, OSV, etc.) from
this environment, in either audit pass. Not re-claimed as "checked."
**Recommend `pip-audit` in CI** (Phase 1's `REFACTORING_PLAN.md`
SEC-AUDIT item, still not implemented — cross-referenced, not
re-invented as new).

## 4. Duplicate / conflicting dependencies

None found — 10 packages, each named once, confirmed via the
dogfooded `check_dependencies()` run above (which would surface a
`duplicate`/`version_conflict` issue kind if one existed — see
`release_validation/dependency_checker.py`'s issue-kind taxonomy).

## 5. Unnecessary runtime dependencies

Checked whether every one of the 10 listed packages is actually
imported/invoked somewhere reachable at runtime.
`pytest`/`pytest-asyncio`/`httpx`/`aiosqlite` are dev/test-only
(already labeled as such in `requirements.txt`'s own comment). The 6
non-dev entries are each confirmed used: 4 via direct `import` in
application code (`fastapi`/`pydantic`/`sqlalchemy`/`structlog`),
`uvicorn` via `deploy/docker/Dockerfile`'s `CMD`, `alembic` via
`scripts/install/install.py`'s `subprocess.run(["alembic", ...])`
call. **No unnecessary dependency found** — every entry earns its
place, including the one (`alembic`) whose *check* is broken.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| DEP1 | `check_dependencies()` false-positives on `alembic` because the repo's own `alembic/` directory shadows `importlib.util.find_spec` as a namespace package | `scripts/install/verify.py` (`check_dependencies`) | Medium-High | Use `shutil.which()` for CLI-only deps, or strip cwd from `sys.path` before resolving |
| DEP2 | CVE/advisory scanning still not implemented (carried from Phase 1, unresolved) | N/A — process gap, not code | Medium | Add `pip-audit` to CI |

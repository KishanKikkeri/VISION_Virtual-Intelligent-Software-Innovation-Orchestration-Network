# PROJECT_AUDIT.md — AASC Platform Codebase Audit

**Audit date:** 2026-07-19
**Scope caveat (read first):** This audit covers the repository slice
actually present in this environment: `plugin_sdk` (M4.7),
`workflow_designer` (M4.8), `production` (M4.9), `release_validation`
(M4.10), plus `deploy/`, `infrastructure/database/`, and `tests/`. It
does **not** cover — because they are not present in this slice —
Monitoring, Chaos Engineering (M4.5), Security Hardening (M4.6), a live
LangGraph agent runtime, a NATS event bus implementation, or agent
orchestration code. Every score and finding below is scoped to what
exists here; sections 6-8 (LangGraph/Agent/NATS) are marked "not
applicable to this slice" rather than scored, so as not to imply
findings about code that was never audited.

## Executive Summary

| Category | Score /10 | Basis |
|---|---|---|
| Architecture | 8 | Consistent 4-layer convention, no circular imports found, one file over 400 LOC |
| Security | 7 | 0 High/0 Critical findings; 1 Medium (hardcoded tmp path); plugin sandbox gap is self-documented, not hidden |
| Reliability | 8 | Consistent graceful-degradation pattern; all 81 broad excepts are commented/justified |
| Performance | 6 | No DB indexes beyond primary keys in any migration; no N+1 query sites found (few DB call sites exist) |
| Maintainability | 8 | Low duplication, small functions, consistent naming; 24 unused-import lint findings |
| Testing | 7 | 718/718 passing (714 pre-fix + 4 new zip-slip regression tests); coverage concentrated on core/CLI/API layers, thinner on install/CLI edge cases |
| Documentation | 8 | Every module has a rationale-carrying docstring; no requirements.txt to document install |
| Production Readiness | 5 → 7 after this pass's fix | **Was:** Dockerfile referenced a `requirements.txt` that didn't exist — the documented build path was broken. **Now:** reconstructed and verified (see §Changes implemented). Not raised to 8+ because the reconstruction is floor-only, unpinned (`TECHNICAL_DEBT.md` D5), and the plugin-sandbox gap (`SECURITY_REPORT.md`) remains open. |

## Repository Statistics

- Python files: **75** (services/scripts/benchmarks/examples/plugins/tests)
- Total LOC (all `.py`): **15,735**; LOC in application code (excl. tests): **9,123**
- Service packages: **6** (`plugin_sdk`, `workflow_designer`, `production`, `dashboard`, `release_validation`, `api`)
- API route modules: **4** (`designer.py`, `plugins.py`, `production.py`, `release.py`)
- Workflows / LangGraphs: **0 running graphs** — `workflow_designer/graph_builder.py` builds/validates layout graphs for the visual designer; no LangGraph agent execution graph is present in this slice
- Test files: **4**, **575** test functions, **714** total passing (incl. reused fixtures)
- Migrations: **3** (`0011_plugin_sdk`, `0012_workflow_designer`, `0013_production_readiness`)
- DB model / SQLAlchemy files: **14**
- Repositories (`*_repository.py`): **4**
- Dashboards: **1** backend package (`dashboard_builder.py`/`dashboard_models.py`); no frontend SPA present in this slice (`static/designer/` holds designer canvas assets only)
- CLI tools: **4** (`designer_cli.py`, `plugin_cli.py`, `production_cli.py`, `release_cli.py`)
- Plugins: **3** sample plugins (`hello_plugin`, `metrics_plugin`, `notification_plugin`)

## Top 3 findings, ranked

1. **[Critical, Production Readiness] `deploy/docker/Dockerfile` COPYs and installs a `requirements.txt` that does not exist anywhere in this repository.** A `docker build` following this repo's own documented deployment path fails today. See `PROJECT_AUDIT.md` §Production Review and `REFACTORING_PLAN.md` item P1.
2. **[Medium, Security] No secondary database indexes.** All three migrations (`0011`-`0013`) define primary keys only — no `Index(...)`/`index=True` on any foreign key or frequently-filtered column (`plugin_id`, `environment`, `workflow_id`). Not a vulnerability, but a real scaling cliff once tables grow. See `PERFORMANCE_REPORT.md`.
3. **[Low, Maintainability] 24 unused imports** (via `pyflakes`), all safe to remove, none load-bearing. See `DEAD_CODE_REPORT.md` — fixed in this pass, see §"Changes implemented" below.

## What this audit did *not* find

Worth stating explicitly, since a "no issues of type X" is itself
useful information a scan can produce:

- **0** uses of `eval`/`exec`/`pickle`/`yaml.load`/`shell=True` anywhere in application code.
- **0** bare `except:` clauses.
- **0** mutable default arguments (`def f(x=[])`).
- **0** High-severity or Critical-severity `bandit` findings.
- **0** circular imports detected between service packages.
- **0** files over 500 LOC (one file, `dashboard_builder.py` at 431 LOC, is the largest).

## Changes implemented in this pass

See `REFACTORING_PLAN.md` for the full prioritized list and
`TECHNICAL_DEBT.md`/`DEAD_CODE_REPORT.md` for what was and wasn't
touched. Four changes were made, each verified against the full test
suite before and after:

1. **Removed 22 of 24 `pyflakes`-flagged unused imports.** The other 2
   were a false alarm on this audit's own part — caught during
   verification and corrected to a one-line `# noqa: F401` annotation
   instead, since they're deliberate importability probes, not dead
   code (see `DEAD_CODE_REPORT.md`'s correction).
2. **Fixed a real zip-slip vulnerability** in
   `plugin_sdk/plugin_loader.py`'s zip-bundle extraction path. The
   originally-planned fix (pass `filter="data"` to `extractall`) turned
   out to be wrong on inspection — `zipfile` has no such parameter,
   only `tarfile` does — so this pass implemented real per-member path
   validation (`_safe_extractall`) instead, with 4 new tests proving it
   both rejects a crafted malicious zip and still extracts legitimate
   ones correctly.
3. **Reconstructed `requirements.txt`** from every third-party import
   actually found across the codebase, unblocking `deploy/docker/
   Dockerfile`'s documented (previously broken) build path. Verified
   live: ran `scripts/install/verify.py`'s dependency check against it
   in this environment, found `uvicorn` genuinely missing, installed
   it, and confirmed the check then passes.
4. Regenerated every M4.10 on-disk artifact (`docs/generated/`,
   `benchmarks/benchmark.{json,md}`, `release/release_manifest.json`,
   `QA_REPORT.md`) against the post-fix codebase.

**Final verification: 718/718 tests passing** (714 pre-existing + 4 new
zip-slip regression tests), `pyflakes` clean (net of the 2 documented
false positives), `bandit` unchanged (0 High, 1 Medium, 14 Low — the
zip-slip issue was a manual-review finding, not a `bandit` signature,
so its fix doesn't move `bandit`'s own count).

Everything else in this report is **findings only** — per the audit
brief's constraint ("never change behavior unless it is objectively a
bug"), items that are architectural opinions rather than bugs (e.g.
"no secondary indexes") are reported and prioritized, not
unilaterally fixed, since adding indexes is a schema change with real
migration-ordering and rollout implications that deserves a
deliberate decision, not an automated pass.

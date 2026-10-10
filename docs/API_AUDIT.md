# API_AUDIT.md

## Inventory (re-counted directly from source, not assumed)

44 endpoints across 4 routers: `designer.py` (12), `plugins.py` (10),
`production.py` (13), `release.py` (12) — counted via `grep -c
"^@router\." services/integration/api/*.py`.

## 1. Missing authentication/authorization — Critical, applies to all 44 endpoints

`grep -rniE "authenticate|authorization|jwt|oauth|Depends\(.*auth"
services/integration/api/*.py` → **zero matches.** No router in this
slice has any `Depends(...)` auth dependency, no `Authorization`
header is read anywhere, and no permission check gates any handler.
Every endpoint — including destructive ones like `DELETE
/platform/designer/workflows/{workflow_name}`
(`designer.py:152`), `DELETE /platform/plugins/{plugin_id}`
(`plugins.py:230`), and `POST /platform/production/restore`
(`production.py:199`) — is callable by anyone who can reach the
process, with no identity or permission check whatsoever.
**Severity: Critical.** **Fix:** add a shared `Depends(require_auth)`
(or equivalent) to `APIRouter(...)`'s constructor for each router, not
per-endpoint, so it can't be forgotten on a new route.

## 2. Missing pagination — 1 concrete unbounded endpoint found

- **`GET /platform/production/releases`** (`api/production.py:120`)
  → `ProductionRepository.list_releases()`
  (`production_repository.py:51-55`) has **no `limit` parameter at
  all** — every other list method in the same file
  (`list_backup_records`, `list_restore_records`,
  `list_profile_snapshots`, `list_environment_checks`) has a `limit:
  int = 50`-or-`20` default, confirmed by re-reading all 15 repository
  methods. `list_releases` is the one exception. **Severity: Medium.**
  **Fix:** add `limit: int = 50` matching the sibling methods'
  convention.
- Every other list-returning endpoint (`GET /platform/production/backups`,
  `GET /platform/designer/workflows`, `GET /platform/designer/history/
  {workflow_name}`, `GET /platform/plugins`) is backed by a repository
  method that already has a bounded default (`limit=50` or `limit=20`)
  — confirmed for each — but **none of the 44 endpoints exposes that
  limit as a caller-adjustable query parameter**, and none returns an
  `offset`/cursor for paging past the default window. Not flagged as
  a bug (every list is bounded, so nothing is unbounded) but flagged
  as a real usability gap: a caller can never see backup record #51.
  **Severity: Low.**

## 3. Missing rate limiting — applies to all 44 endpoints

`grep -rn "rate.?limit\|slowapi\|RateLimiter"` → zero matches anywhere
in `services/`. No per-IP or per-token request throttling exists.
Combined with finding #1 (no auth), this means any of the 44
endpoints — including `POST /platform/release/qa`, which (per
`RUNTIME_ANALYSIS.md` R2) synchronously blocks the event loop for up
to 120 seconds — can be called with unlimited concurrency by an
unauthenticated caller. **Severity: High** (compounds directly with
R2: a burst of concurrent `POST /platform/release/qa` calls would
serialize on the blocked event loop and could stall the entire
process for other requests).

## 4. HTTP status code consistency

Re-tallied every `status_code=` literal across all 4 routers:
`400`×1, `404`×11, `409`×1, `422`×21, `503`×12. Spot-checked
usage consistency: every `503` is used for "an operation that talks
to the DB/subprocess/filesystem failed" (e.g.
`production.py:129`'s `except Exception ... raise
HTTPException(status_code=503, ...)` around a DB call), and every
`404` is used for "requested resource not found" (e.g.
`designer.py:110`'s workflow-not-found path). **Consistent usage, no
finding.** One inconsistency found: `api/release.py`'s
`get_status`/`get_benchmark`/`get_qa`/`get_manifest` (lines 48, 102,
117, 132) return **HTTP 200** with a body `{"available": false, ...}`
when nothing has been computed yet, while `api/release.py:143-149`'s
`get_export` returns a genuine **404** for the equivalent
"nothing computed yet" case. Two different conventions for the same
situation ("you haven't run X yet") in the same file.
**Severity: Low.** **Fix:** pick one convention (this audit has no
opinion on which — that's a product decision, not a bug) and apply it
to all four `GET .../status`-shaped endpoints in this router.

## 5. Response model consistency

Every handler returns `Dict[str, Any]` (via a raw `dict` literal or
`.model_dump(mode="json")`) rather than a declared FastAPI
`response_model=`. Confirmed: `grep -c "response_model="
services/integration/api/*.py` → **zero matches across all 4 files.**
This means FastAPI's OpenAPI schema generation for every one of these
44 endpoints has no typed response shape — `/openapi.json` (or
`/docs`) would show `{}`/`any` for every response body, not the real
`ReadinessReport`/`QAReport`/etc. shape these handlers actually
return. Not a runtime bug (every handler's actual JSON is correctly
typed by the underlying Pydantic model's own `.model_dump()`), but a
real API-discoverability/documentation gap. **Severity: Low.**
**Fix:** add `response_model=` to each `@router.get`/`@router.post`
decorator using the same Pydantic model the handler already
constructs.

## 6. Exception leakage

Checked whether any handler lets a raw exception's `str(e)` reach the
client in a way that could leak internal detail (file paths, stack
info). Every `except Exception as e` in `api/*.py` wraps `e` in an
`HTTPException(status_code=503, detail=f"... : {e}")` — the message is
descriptive (e.g. `f"could not read releases: {e}"`) but does include
the raw exception string, which for a DB-connectivity error could
include a connection string fragment or file path.
**Severity: Low** (this is a defense-in-depth concern, not an active
leak of secrets — no code path constructs an exception containing a
credential). **Fix:** log `str(e)` server-side, return a generic
`detail` to the client.

## 7. Improper async usage

Covered in depth in `RUNTIME_ANALYSIS.md` R2 (blocking `subprocess.run`
inside `post_qa`). Checked the other 43 handlers for the same pattern
— **none of the other 43 call a synchronous blocking function**; `post_qa`
is the sole instance.

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| API1 | No authentication on any of 44 endpoints | all of `services/integration/api/*.py` | Critical | Router-level `Depends(require_auth)` |
| API2 | No rate limiting on any endpoint | all of `services/integration/api/*.py` | High | Add request throttling middleware |
| API3 | `list_releases()` has no `limit`, unlike its 4 sibling list methods | `production_repository.py:51-55`, called from `api/production.py:120` | Medium | Add `limit: int = 50` |
| API4 | Inconsistent "not yet computed" convention: 200+flag vs. 404 in the same router | `api/release.py:48,102,117,132` vs. `api/release.py:143-149` | Low | Standardize on one convention |
| API5 | No `response_model=` on any of 44 endpoints — OpenAPI schema is untyped | all of `services/integration/api/*.py` | Low | Add `response_model=` per handler |
| API6 | Raw exception string reaches client `detail` field | every `except Exception as e: raise HTTPException(..., detail=f"...{e}")` across all 4 routers | Low | Log server-side, generic client message |
| API7 | Blocking subprocess call inside async handler | see `RUNTIME_ANALYSIS.md` R2 | Medium | Cross-referenced, not duplicated here |

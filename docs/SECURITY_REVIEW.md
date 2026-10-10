# SECURITY_REVIEW.md — Phase 2

Per this audit's "only report verified issues" constraint: every
finding below was reproduced with a live proof-of-concept in this
session where the code permits it (Python is executable here), not
inferred from reading alone.

## 1. Unescaped HTML export — reflected/stored XSS — Critical, NEW finding, verified with a live PoC

**`services/integration/production/production_export.py:59-62`**
(`_to_html`, reused by `release_validation/release_export.py` and thus
reachable via `GET /platform/release/export?fmt=html`,
`api/release.py:140-149`):

```python
def _to_html(report: BaseModel) -> str:
    ...
    body = "".join(f"<p>{line}</p>\n" for line in markdown_lines)
    return f"<html><head><title>{_title_for(report)}</title></head><body>\n{body}</body></html>"
```

No `html.escape()` (or any escaping) call exists anywhere in this
file (`grep -n "html.escape" production_export.py` → zero matches).
**Reproduced live in this session:** constructing a
`DependencyIssue(detail="<script>alert(document.cookie)</script>")`
and passing it through `release_export.export_report(report, "html")`
produces `<p>  - **detail**: <script>alert(document.cookie)</script></p>`
verbatim in the output — the script tag is not escaped or stripped.

**Why this is reachable with attacker-influenced content, not just
theoretical:** `dependency_checker.check_dependencies()`
(`release_validation/dependency_checker.py`) builds `DependencyIssue.detail`
directly from parsed `requirements.txt` content (dependency names and
version strings appear verbatim in the message). Anyone who can
influence that file's content (a malicious PR, a compromised
transitive dependency's name in a lockfile update) controls text that
flows unescaped into this HTML export. `QACheckResult.detail` similarly
carries raw exception message text (`str(e)`) from subprocess/import
failures in some paths.

**Severity: Critical** if this HTML is ever served to or opened by a
browser (the intended use — `fmt=html` exists specifically to produce
browser-viewable output) with any session/cookie context worth
stealing; the `document.cookie` PoC above demonstrates the mechanism
directly. **Fix:** `html.escape(str(value))` every interpolated field
in `_to_html`, or switch to a templating engine with autoescaping
(Jinja2 with `autoescape=True`) instead of manual f-string
concatenation.

## 2. No authentication on any endpoint — Critical, cross-referenced

Fully covered in `API_AUDIT.md` finding API1 — not re-derived here,
cross-referenced per this audit's "don't invent findings" instruction
(re-deriving an already-verified finding a second time doesn't add
evidence). **Combines directly with finding #1**: the unauthenticated
`GET /platform/release/export?fmt=html` endpoint means finding #1's
XSS payload doesn't even require a privileged caller to trigger.

## 3. Plugin execution sandbox — re-verified, unchanged from Phase 1

Re-confirmed `plugin_loader.import_entrypoint`
(`plugin_sdk/plugin_loader.py`) still calls
`importlib.util.module_from_spec` + `spec.loader.exec_module` with no
restricted builtins, no resource limits, no process isolation — same
finding as Phase 1's `SECURITY_REPORT.md`, still open (by design,
Large effort, not attempted in either audit pass). Re-verifying this
here rather than silently dropping it, since Phase 2's brief asks for
"plugin isolation / sandbox boundaries" explicitly.

## 4. Zip-slip — re-verified fixed, with evidence

Phase 1 found and fixed a zip-slip vulnerability in
`plugin_loader.py`'s zip-bundle extraction. Re-verified in this pass
that the fix is still present and correct:
`_safe_extractall`/`UnsafeZipMemberError` at `plugin_loader.py:77-101`,
called at `plugin_loader.py:226`. Re-ran the exact malicious-zip
proof-of-concept from Phase 1 (`../../evil.py` traversal member) —
**still correctly rejected.** No regression.

## 5. SSRF

`grep -rn "requests.get\|httpx.get\|urlopen\|aiohttp" services` →
**zero matches.** No outbound HTTP request is made anywhere in
application code with a URL built from request input (or at all,
except test infrastructure's own `TestClient`, which isn't
network-facing). **No SSRF surface exists in this slice.**

## 6. CSRF

Not applicable in the meaningful sense — CSRF requires a
cookie/session-authenticated state-changing endpoint that a browser
would auto-attach credentials to. Since finding #2 establishes there
is **no authentication of any kind**, there is no session to forge a
request against; every endpoint is equally callable by any caller
already, with or without CSRF. (This is not a mitigating factor for
#2 — it's the same underlying problem restated. Once #2 is fixed with
a session/cookie-based auth scheme, CSRF protection becomes a new,
real requirement that doesn't exist today because nothing is
authenticated yet to protect.)

## 7. Command injection

Re-verified (same method as Phase 1, re-run fresh): every
`subprocess.run` call site (`scripts/install/verify.py:69`,
`scripts/install/install.py:43`, `release_validation/final_qa.py:50`)
passes a hardcoded or closed-set argv list; `shell=True` appears
**zero times** anywhere in the codebase. No command injection surface.

## 8. SQL injection

Re-verified: every `db.execute(...)` call site uses SQLAlchemy
`select()`/ORM constructs; `grep -rn "execute(f\"" services` (an
f-string passed directly to `execute`, the classic injection pattern)
→ zero matches.

## 9. Unsafe deserialization

`grep -rn "pickle\|yaml.load\b" services` → zero matches (`yaml` isn't
imported anywhere in this slice at all). All JSON deserialization goes
through Pydantic model validation (`model_validate`/`model_validate_json`),
which rejects unexpected shapes rather than executing arbitrary code —
no unsafe deserialization surface found.

## 10. Secrets / token handling

Re-verified: `grep -rnE "(api_key|password|secret|token)\s*=\s*[\"'][^\"']+[\"']"`
→ zero matches (only `.example`-suffixed placeholder files in
`deploy/k8s/` contain the string "secret," and those are named to make
clear they're templates). No token-handling code exists to review
(consistent with finding #2 — there's no auth token scheme
implemented yet).

## Findings summary

| # | Finding | File:Line | Severity | Fix |
|---|---|---|---|---|
| SEC1 | Unescaped HTML export — XSS, reproduced with a live PoC | `production/production_export.py:59-62` | Critical | `html.escape()` every interpolated value, or switch to autoescaping templating |
| SEC2 | No authentication on any endpoint | all of `api/*.py` — see `API_AUDIT.md` API1 | Critical | Cross-referenced, not duplicated |
| SEC3 | Plugin entrypoint execution has no sandbox | `plugin_sdk/plugin_loader.py` (`import_entrypoint`) | Critical (by design, Large effort) | Unchanged from Phase 1 — architectural decision, not attempted |
